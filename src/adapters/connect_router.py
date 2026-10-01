"""Amazon Connect adapter for the Roteador_de_Atendimento (Task 12.4).

This adapter performs the *I/O* side of escalating an interaction to an
Atendente_Humano via Amazon Connect. All pure decision logic lives in the
domain layer and is reused here rather than re-implemented:

* :func:`src.domain.escalation.build_escalation_payload` assembles the full
  session history to transfer (Req 3.3, Property 8).
* :func:`src.domain.retry.plan_retries` / :func:`src.domain.retry.decide_retry`
  bound the connection attempts to 3 tries (Req 3.5, Property 10). Each attempt
  is given its own connection timeout of
  :data:`src.domain.config.CONNECT_ATTEMPT_TIMEOUT_S` seconds (5 s).
* :func:`src.domain.availability.signal_unavailable` produces the structured
  unavailability result while preserving the original input, used when all
  attempts are exhausted (Req 3.5).

Responsibilities mapped to acceptance criteria:

* **Req 3.1 / 3.2** — the caller decides *whether* to escalate (via
  :func:`src.domain.routing.decide_routing`); this adapter *starts* the
  escalation to Amazon Connect.
* **Req 3.3** — the complete current session history is transferred to Connect
  as the chat contact's initial context/attributes.
* **Req 3.4** — the request is enqueued (``start_chat_contact`` places the
  contact into the configured queue) **independently** of whether an
  Atendente_Humano is currently available.
* **Req 3.5** — connection is attempted up to 3 times, each with a 5 s timeout;
  on exhaustion the Usuario is informed of the unavailability and the session
  history is preserved unchanged.
* **Req 3.6** — while the interaction waits in the queue, the adapter reports the
  queue position and/or the estimated wait time.

The ``boto3`` import is guarded so this module imports even in environments where
``boto3`` is not installed (e.g. unit-test/CI runners without AWS SDKs). A Connect
client may be injected for testing; otherwise a real client is lazily built.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.domain.availability import Dependency, signal_unavailable
from src.domain.config import CONNECT_ATTEMPT_TIMEOUT_S, MAX_RETRIES
from src.domain.escalation import EscalationPayload, build_escalation_payload
from src.domain.models import Message, Session
from src.domain.retry import RetryOperation, decide_retry, plan_retries

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3/botocore are absent, so adapters can be
# unit-tested via mocking. When present, we configure per-attempt timeouts and
# catch the SDK's transient error types.
try:  # pragma: no cover - exercised indirectly depending on environment
    import boto3
    from botocore.config import Config as _BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    boto3 = None  # type: ignore[assignment]
    _BotoConfig = None  # type: ignore[assignment]

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    _BOTO3_AVAILABLE = False


#: Exceptions treated as a *failed* (retryable) connection attempt. Network and
#: SDK transient errors count as failures against the 3-attempt budget (Req 3.5).
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BotoCoreError,
    ClientError,
    ConnectionError,
    TimeoutError,
)

#: Sanitized, user-facing message when human attendance cannot be reached after
#: all attempts (Req 3.5). Free of internal/technical detail (Req 10.4).
CONNECT_UNAVAILABLE_MESSAGE: str = (
    "No momento não foi possível conectar você a um atendente humano. "
    "Sua conversa foi preservada e você pode tentar novamente em instantes."
)


@dataclass(frozen=True)
class QueueStatus:
    """Queue position / estimated wait reported while awaiting an attendant (Req 3.6).

    Attributes:
        position: The interaction's approximate position in the queue (number of
            contacts in queue including this one), or ``None`` when the metric is
            unavailable.
        estimated_wait_seconds: The estimated wait time in seconds, or ``None``
            when it cannot be determined.
    """

    position: int | None = None
    estimated_wait_seconds: int | None = None


@dataclass(frozen=True)
class EscalationResult:
    """Outcome of an escalation attempt to Amazon Connect.

    Attributes:
        escalated: ``True`` when the contact was successfully enqueued in Connect.
        contact_id: The Amazon Connect contact identifier when escalated, else
            ``None``.
        queue_status: Queue position / estimated wait to report to the Usuario
            (Req 3.6); ``None`` when not escalated.
        attempts_made: Number of connection attempts actually performed
            (``1..MAX_RETRIES``).
        unavailable: ``True`` when all attempts were exhausted without connecting
            (Req 3.5).
        user_message: Sanitized message to show the Usuario on unavailability
            (Req 3.5), else ``None``.
        preserved_history: The full session history, preserved unchanged so it is
            never lost on failure (Req 3.5).
        payload: The escalation payload built for transfer (Req 3.3).
    """

    escalated: bool
    contact_id: str | None
    queue_status: QueueStatus | None
    attempts_made: int
    unavailable: bool
    user_message: str | None
    preserved_history: tuple[Message, ...]
    payload: EscalationPayload


def _build_connect_client(region_name: str | None) -> Any:
    """Build a real Amazon Connect client with a per-attempt 5 s timeout (Req 3.5).

    Raises:
        RuntimeError: If ``boto3`` is not available in the environment.
    """
    if not _BOTO3_AVAILABLE:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "boto3 is not available; inject a Connect client for testing "
            "or install boto3 to use the real Amazon Connect adapter."
        )
    config = _BotoConfig(
        connect_timeout=CONNECT_ATTEMPT_TIMEOUT_S,
        read_timeout=CONNECT_ATTEMPT_TIMEOUT_S,
        retries={"max_attempts": 0, "mode": "standard"},
    )
    return boto3.client("connect", region_name=region_name, config=config)


class ConnectRouterAdapter:
    """I/O adapter that escalates interactions to Amazon Connect.

    The pure escalation/retry/unavailability decisions are delegated to the
    domain layer; this class only performs the Amazon Connect calls, bounds them
    to 3 attempts of 5 s each, and reports queue status.

    Args:
        instance_id: The Amazon Connect instance identifier.
        contact_flow_id: The contact flow that routes the chat to the human queue.
        queue_id: The queue the escalated contact is placed into (used to read
            position / estimated wait metrics, Req 3.6).
        connect_client: An optional pre-built Connect client. When provided it is
            used as-is (enables unit testing via mocking); otherwise a real client
            is built lazily with a 5 s per-attempt timeout.
        region_name: AWS region used only when building a real client.
    """

    def __init__(
        self,
        instance_id: str,
        contact_flow_id: str,
        queue_id: str,
        *,
        connect_client: Any | None = None,
        region_name: str | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.contact_flow_id = contact_flow_id
        self.queue_id = queue_id
        self._region_name = region_name
        self._client = connect_client

    @property
    def client(self) -> Any:
        """The Connect client, built lazily on first use if not injected."""
        if self._client is None:
            self._client = _build_connect_client(self._region_name)
        return self._client

    # -- history transfer --------------------------------------------------------

    @staticmethod
    def _serialize_history(payload: EscalationPayload) -> str:
        """Serialize the full session history for transfer to Connect (Req 3.3).

        Preserves message order exactly as recorded on the session (Property 8).
        """
        return json.dumps(
            [
                {
                    "role": m.role,
                    "text": m.text,
                    "at": m.at.isoformat() if m.at is not None else None,
                }
                for m in payload.messages
            ],
            ensure_ascii=False,
        )

    # -- queue status (Req 3.6) --------------------------------------------------

    def _query_queue_status(self) -> QueueStatus:
        """Read queue position and estimated wait for the configured queue (Req 3.6).

        Uses Amazon Connect current-metric data. Failures to read metrics are
        non-fatal: the contact remains enqueued and a best-effort status is
        returned.
        """
        try:
            resp = self.client.get_current_metric_data(
                InstanceId=self.instance_id,
                Filters={"Queues": [self.queue_id]},
                CurrentMetrics=[
                    {"Name": "CONTACTS_IN_QUEUE", "Unit": "COUNT"},
                    {"Name": "OLDEST_CONTACT_AGE", "Unit": "SECONDS"},
                ],
            )
        except RETRYABLE_EXCEPTIONS:
            return QueueStatus()

        position: int | None = None
        estimated_wait: int | None = None
        for result in resp.get("MetricResults", []):
            for collection in result.get("Collections", []):
                name = collection.get("Metric", {}).get("Name")
                value = collection.get("Value")
                if value is None:
                    continue
                if name == "CONTACTS_IN_QUEUE":
                    position = int(value)
                elif name == "OLDEST_CONTACT_AGE":
                    estimated_wait = int(value)
        return QueueStatus(position=position, estimated_wait_seconds=estimated_wait)

    # -- escalation (Req 3.3, 3.4, 3.5, 3.6) -------------------------------------

    def _start_chat_contact(self, payload: EscalationPayload) -> str:
        """Enqueue the escalation as a Connect chat contact (Req 3.3, 3.4).

        The contact is placed into the queue via the configured contact flow,
        independently of attendant availability (Req 3.4). The full session
        history is transferred as contact attributes and an initial system
        message (Req 3.3).

        Returns:
            The Amazon Connect contact identifier.
        """
        history_json = self._serialize_history(payload)
        resp = self.client.start_chat_contact(
            InstanceId=self.instance_id,
            ContactFlowId=self.contact_flow_id,
            Attributes={
                "session_id": payload.session_id,
                "user_key": payload.user_key,
                "channel": payload.channel,
                "session_history": history_json,
            },
            ParticipantDetails={"DisplayName": payload.user_key},
            InitialMessage={
                "ContentType": "text/plain",
                "Content": history_json,
            },
        )
        return resp["ContactId"]

    def escalate(self, session: Session) -> EscalationResult:
        """Escalate ``session`` to an Atendente_Humano via Amazon Connect.

        Builds the transfer payload with the full session history (Req 3.3),
        enqueues the contact independently of attendant availability (Req 3.4),
        retrying the connection up to 3 times with a 5 s timeout each (Req 3.5).
        On success it reports the queue position / estimated wait (Req 3.6). On
        exhaustion it returns a structured unavailability result while preserving
        the session history unchanged (Req 3.5).

        Args:
            session: The current session to escalate.

        Returns:
            An :class:`EscalationResult` describing the outcome.
        """
        payload = build_escalation_payload(session)
        # Bounded plan of 3 attempts, each with its own 5 s connection timeout.
        plan = plan_retries(RetryOperation.CONNECT_ESCALATION, max_attempts=MAX_RETRIES)
        max_attempts = plan.max_attempts

        attempts_made = 0
        while True:
            try:
                contact_id = self._start_chat_contact(payload)
            except RETRYABLE_EXCEPTIONS:
                attempts_made += 1
                decision = decide_retry(
                    RetryOperation.CONNECT_ESCALATION,
                    attempts_made,
                    max_attempts=max_attempts,
                )
                if decision.should_retry:
                    continue
                # Req 3.5: all attempts exhausted -> unavailable, preserve history.
                unavailable = signal_unavailable(
                    Dependency.CONNECT_FORWARDING,
                    payload.messages,
                    escalate=False,
                    user_message=CONNECT_UNAVAILABLE_MESSAGE,
                )
                return EscalationResult(
                    escalated=False,
                    contact_id=None,
                    queue_status=None,
                    attempts_made=attempts_made,
                    unavailable=True,
                    user_message=unavailable.user_message,
                    preserved_history=payload.messages,
                    payload=payload,
                )

            # Enqueued successfully (Req 3.4). Report queue status (Req 3.6).
            attempts_made += 1
            queue_status = self._query_queue_status()
            return EscalationResult(
                escalated=True,
                contact_id=contact_id,
                queue_status=queue_status,
                attempts_made=attempts_made,
                unavailable=False,
                user_message=None,
                preserved_history=payload.messages,
                payload=payload,
            )
