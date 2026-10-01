"""Property-based test for the bounded retry policy.

Feature: assistente-progep-ifs, Property 10: Política de retentativa limitada.

Validates: Requirements 3.5, 6.5, 7.5

For any sequência de falhas consecutivas de uma operação com política de
retentativa (escalonamento ao Connect, entrega no WhatsApp, registro no
Monitor_de_Consumo), o número de tentativas nunca excede o máximo configurado
(3) e os intervalos planejados entre tentativas respeitam o mínimo aplicável
(>= 10 s para entrega no WhatsApp); ao esgotar as tentativas, o resultado
sinaliza a indisponibilidade.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.config import MAX_RETRIES, MIN_WHATSAPP_RETRY_INTERVAL_S
from src.domain.retry import (
    RetryOperation,
    decide_retry,
    min_retry_interval_s,
    plan_retries,
)

# Arbitrary operation governed by the bounded retry policy.
_operations = st.sampled_from(list(RetryOperation))
# Valid configured maxima are within 1..MAX_RETRIES.
_max_attempts = st.integers(min_value=1, max_value=MAX_RETRIES)
# Attempts already made: 0..beyond the maximum, to exercise exhaustion.
_attempts_made = st.integers(min_value=0, max_value=MAX_RETRIES + 5)


@settings(max_examples=200)
@given(operation=_operations, max_attempts=_max_attempts)
def test_plan_never_exceeds_max_and_respects_min_interval(
    operation: RetryOperation,
    max_attempts: int,
) -> None:
    """plan_retries stays bounded and honours the minimum inter-attempt spacing."""
    plan = plan_retries(operation, max_attempts=max_attempts)

    # The number of planned attempts never exceeds the configured maximum,
    # which itself never exceeds MAX_RETRIES (Req 3.5, 6.5, 7.5).
    assert len(plan.attempts) == max_attempts
    assert len(plan.attempts) <= MAX_RETRIES

    # Attempts are numbered sequentially from 1.
    assert [a.attempt for a in plan.attempts] == list(range(1, max_attempts + 1))

    # Consecutive attempts respect the applicable minimum interval; for WhatsApp
    # delivery this is >= 10 seconds (Req 6.5).
    min_interval = min_retry_interval_s(operation)
    if operation is RetryOperation.WHATSAPP_DELIVERY:
        assert min_interval >= MIN_WHATSAPP_RETRY_INTERVAL_S
    for prev, curr in zip(plan.attempts, plan.attempts[1:]):
        spacing = curr.earliest_offset_s - prev.earliest_offset_s
        assert spacing >= min_interval

    # Exhausting a non-empty plan signals unavailability (Req 3.5, 6.5, 7.5).
    assert plan.signals_unavailability() is True


@settings(max_examples=200)
@given(
    operation=_operations,
    attempts_made=_attempts_made,
    max_attempts=_max_attempts,
)
def test_decide_retry_allows_retry_iff_below_max_and_signals_unavailable(
    operation: RetryOperation,
    attempts_made: int,
    max_attempts: int,
) -> None:
    """decide_retry retries iff attempts_made < max; on exhaustion it is unavailable."""
    decision = decide_retry(
        operation, attempts_made, max_attempts=max_attempts
    )

    should_retry = attempts_made < max_attempts
    assert decision.should_retry is should_retry

    if should_retry:
        # A further attempt is planned within the bound.
        assert decision.next_attempt == attempts_made + 1
        assert decision.next_attempt <= MAX_RETRIES
        assert decision.exhausted is False
        assert decision.unavailable is False
        # WhatsApp delivery must wait at least 10 seconds before retrying (Req 6.5).
        assert decision.wait_s == min_retry_interval_s(operation)
        if operation is RetryOperation.WHATSAPP_DELIVERY:
            assert decision.wait_s >= MIN_WHATSAPP_RETRY_INTERVAL_S
    else:
        # On exhaustion the result signals unavailability (Req 3.5, 6.5, 7.5).
        assert decision.next_attempt is None
        assert decision.wait_s is None
        assert decision.exhausted is True
        assert decision.unavailable is True
        assert decision.reason is not None
