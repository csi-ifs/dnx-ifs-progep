"""Bounded retry policy for retryable operations (pure-logic layer).

Implements the shared retry policy used by three operations that must tolerate
transient failures while staying strictly bounded:

- **Connect escalation** (Req 3.5): the Roteador_de_Atendimento attempts to reach
  Amazon Connect up to 3 times (each attempt has its own connection timeout); on
  exhaustion it signals unavailability of human attendance and preserves the
  session history.
- **WhatsApp delivery** (Req 6.5): the Conector_WhatsApp retries delivery up to 3
  times, honouring a minimum interval of 10 seconds between attempts.
- **Monitor recording** (Req 7.5): the Monitor_de_Consumo retries a metrics/response
  record up to 3 times, exposing an error that identifies the affected session on
  exhaustion.

All three share the same upper bound (:data:`~src.domain.config.MAX_RETRIES`) and
the "signal unavailability on exhaustion" behaviour. The only per-operation
difference is the minimum interval between attempts: WhatsApp delivery requires
at least :data:`~src.domain.config.MIN_WHATSAPP_RETRY_INTERVAL_S` seconds, while the
others have no mandated minimum spacing.

This module is intentionally free of any I/O — it plans attempts and decides
whether to retry, but never performs the operation, waits, or calls a clock. The
actual retry loops, waiting and service calls live in the respective adapters,
which consume the plan/decision produced here. This keeps the policy exercisable
by property-based tests (Property 10).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.domain.config import MAX_RETRIES, MIN_WHATSAPP_RETRY_INTERVAL_S


class RetryOperation(str, Enum):
    """Operations governed by the bounded retry policy (Req 3.5, 6.5, 7.5)."""

    CONNECT_ESCALATION = "connect_escalation"
    WHATSAPP_DELIVERY = "whatsapp_delivery"
    MONITOR_RECORDING = "monitor_recording"


def min_retry_interval_s(operation: RetryOperation) -> int:
    """Return the minimum interval, in seconds, between attempts for ``operation``.

    Only WhatsApp delivery mandates a minimum spacing between attempts
    (:data:`MIN_WHATSAPP_RETRY_INTERVAL_S`, Req 6.5). Connect escalation
    (Req 3.5) and Monitor recording (Req 7.5) have no mandated minimum interval,
    so they return ``0``.
    """
    if operation is RetryOperation.WHATSAPP_DELIVERY:
        return MIN_WHATSAPP_RETRY_INTERVAL_S
    return 0


@dataclass(frozen=True)
class RetryAttempt:
    """A single planned attempt within a :class:`RetryPlan`.

    Attributes:
        attempt: The 1-based attempt number (``1`` is the initial try).
        earliest_offset_s: The earliest time, in seconds relative to the first
            attempt, at which this attempt may start. Consecutive attempts are
            spaced by at least the operation's minimum interval (Req 6.5 for
            WhatsApp).
    """

    attempt: int
    earliest_offset_s: int


@dataclass(frozen=True)
class RetryPlan:
    """A bounded plan of attempts for a retryable operation (Property 10).

    The plan enumerates every attempt that may be performed, never exceeding
    ``max_attempts`` (Req 3.5, 6.5, 7.5), with each consecutive attempt spaced by
    at least ``min_interval_s`` seconds. If every planned attempt fails, the
    operation is deemed unavailable — see :meth:`signals_unavailability`.

    Attributes:
        operation: The operation this plan governs.
        max_attempts: The maximum number of attempts allowed (``<= MAX_RETRIES``).
        min_interval_s: The minimum interval between consecutive attempts.
        attempts: The ordered tuple of planned attempts.
    """

    operation: RetryOperation
    max_attempts: int
    min_interval_s: int
    attempts: tuple[RetryAttempt, ...]

    def signals_unavailability(self) -> bool:
        """Whether exhausting this plan signals unavailability.

        Every governed operation signals unavailability once all planned
        attempts have failed (Req 3.5, 6.5, 7.5), so this is always ``True`` for
        a valid plan with at least one attempt.
        """
        return len(self.attempts) > 0


@dataclass(frozen=True)
class RetryDecision:
    """The decision for the next step after ``attempts_made`` failed attempts.

    Attributes:
        should_retry: ``True`` when another attempt is allowed.
        next_attempt: The 1-based number of the next attempt when
            ``should_retry`` is ``True``; ``None`` when exhausted.
        wait_s: The minimum number of seconds to wait before the next attempt
            (``0`` when no minimum spacing applies); ``None`` when exhausted.
        exhausted: ``True`` when the maximum number of attempts has been reached.
        unavailable: ``True`` when the operation must signal unavailability
            (equivalent to ``exhausted`` for the governed operations).
        reason: A human-readable reason when the operation is unavailable;
            ``None`` otherwise.
    """

    should_retry: bool
    next_attempt: int | None
    wait_s: int | None
    exhausted: bool
    unavailable: bool
    reason: str | None = None


def plan_retries(
    operation: RetryOperation,
    *,
    max_attempts: int = MAX_RETRIES,
) -> RetryPlan:
    """Produce the bounded retry plan for ``operation`` (Req 3.5, 6.5, 7.5).

    The plan contains exactly ``max_attempts`` attempts (never more than
    :data:`MAX_RETRIES`), the first at offset ``0`` and each subsequent attempt
    spaced by the operation's minimum interval (Req 6.5 for WhatsApp delivery).

    Args:
        operation: The operation to plan retries for.
        max_attempts: The maximum number of attempts. Must be between ``1`` and
            :data:`MAX_RETRIES` inclusive.

    Returns:
        A :class:`RetryPlan` enumerating every planned attempt.

    Raises:
        ValueError: If ``max_attempts`` is not within ``1..MAX_RETRIES``.
    """
    if not 1 <= max_attempts <= MAX_RETRIES:
        raise ValueError(
            f"max_attempts must be within 1..{MAX_RETRIES}, got {max_attempts}"
        )
    interval = min_retry_interval_s(operation)
    attempts = tuple(
        RetryAttempt(attempt=i + 1, earliest_offset_s=i * interval)
        for i in range(max_attempts)
    )
    return RetryPlan(
        operation=operation,
        max_attempts=max_attempts,
        min_interval_s=interval,
        attempts=attempts,
    )


def decide_retry(
    operation: RetryOperation,
    attempts_made: int,
    *,
    max_attempts: int = MAX_RETRIES,
) -> RetryDecision:
    """Decide whether to retry ``operation`` after ``attempts_made`` failures.

    A retry is allowed **if and only if** the number of attempts already made is
    strictly below ``max_attempts`` (Req 3.5, 6.5, 7.5). When another attempt is
    allowed, the decision carries the next attempt number and the minimum wait
    before it (``>= 10`` seconds for WhatsApp delivery, Req 6.5). When the
    attempts are exhausted, the decision signals unavailability with a reason
    that identifies the operation.

    Args:
        operation: The operation being retried.
        attempts_made: The number of attempts already performed (all failed).
            Must be ``>= 0``.
        max_attempts: The maximum number of attempts allowed. Must be between
            ``1`` and :data:`MAX_RETRIES` inclusive.

    Returns:
        A :class:`RetryDecision` describing whether to retry and, if so, the next
        attempt number and the minimum wait; otherwise flagged as exhausted and
        unavailable.

    Raises:
        ValueError: If ``attempts_made`` is negative or ``max_attempts`` is not
            within ``1..MAX_RETRIES``.
    """
    if attempts_made < 0:
        raise ValueError(f"attempts_made must be >= 0, got {attempts_made}")
    if not 1 <= max_attempts <= MAX_RETRIES:
        raise ValueError(
            f"max_attempts must be within 1..{MAX_RETRIES}, got {max_attempts}"
        )

    if attempts_made < max_attempts:
        return RetryDecision(
            should_retry=True,
            next_attempt=attempts_made + 1,
            wait_s=min_retry_interval_s(operation),
            exhausted=False,
            unavailable=False,
            reason=None,
        )

    return RetryDecision(
        should_retry=False,
        next_attempt=None,
        wait_s=None,
        exhausted=True,
        unavailable=True,
        reason=(
            f"operation {operation.value!r} unavailable after "
            f"{max_attempts} attempts"
        ),
    )
