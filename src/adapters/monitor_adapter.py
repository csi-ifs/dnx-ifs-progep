"""Monitor_de_Consumo adapter (CloudWatch + AgentCore Observability) — Task 12.7.

This adapter performs the *I/O* side of the Monitor_de_Consumo: it emits token
consumption metrics and response records to Amazon CloudWatch (via
``put_metric_data``). Metrics are aggregate (no per-session dimension, see
:data:`METRIC_DIMENSIONS`); the ``session_id`` of each record goes to the log
line, so consumption is still traceable per session.

Responsibilities mapped to acceptance criteria:

* **Req 7.1** — after a successful LLM call, emit input/output token metrics
  associated with the ``session_id``.
* **Req 7.2** — emit a record for the generated response associated with the
  ``session_id`` (a response count/length metric, plus the response content made
  available for observability).
* **Req 7.3** — metrics are published to CloudWatch (and thus the AgentCore
  observability dashboards) through ``put_metric_data``.
* **Req 7.5** — when a record fails to be written, the recording is retried up to
  3 times using the shared bounded retry policy; on exhaustion a structured error
  **identifying the affected session** is surfaced.

The pure decision logic is reused rather than re-implemented:

* :func:`src.domain.retry.decide_retry` / :func:`src.domain.retry.plan_retries`
  with :attr:`~src.domain.retry.RetryOperation.MONITOR_RECORDING` bound the record
  retries to 3 attempts (Req 7.5, Property 10).
* :func:`src.domain.monitor.record_llm_failure` (with :class:`CallRecord` /
  :class:`CallStatus`) tracks a failed LLM call attempt while preserving the
  records already made (Req 7.4, Property 18).

The ``boto3`` import is guarded so this module imports even where ``boto3`` is not
installed (e.g. CI/unit-test runners without AWS SDKs). A CloudWatch client may be
injected for testing; otherwise a real client is built lazily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.domain.config import MAX_RETRIES
from src.domain.monitor import CallRecord, record_llm_failure
from src.domain.privacidade import mascarar_contato
from src.domain.retry import RetryOperation, decide_retry

logger = logging.getLogger(__name__)

#: CloudWatch namespace under which consumption metrics are published (Req 7.3).
DEFAULT_NAMESPACE: str = "ProgepIFS/Assistente/Consumo"

#: As métricas saem **sem dimensão**. Até 02/10/2026 levavam ``SessionId``, e
#: cada conversa criava duas séries novas no CloudWatch (186 em duas semanas) —
#: e série personalizada é cobrada por hora com dado, então o custo crescia com
#: o número de conversas, não com o volume. A ligação com a sessão (Req 7.1,
#: 7.2) fica na linha de log de cada registro, que é onde se procura uma
#: conversa específica; a métrica serve ao agregado.
METRIC_DIMENSIONS: list[dict[str, str]] = []

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3/botocore are absent, so the adapter can
# be unit-tested by injecting a mock CloudWatch client. When present we catch the
# SDK transient error types as retryable record failures (Req 7.5).
try:  # pragma: no cover - exercised indirectly depending on environment
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    boto3 = None  # type: ignore[assignment]

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    class ClientError(Exception):  # type: ignore[no-redef]
        """Fallback when botocore is unavailable."""

    _BOTO3_AVAILABLE = False


#: Exceptions treated as a *failed* (retryable) record attempt against the
#: 3-attempt budget (Req 7.5). Network/SDK transient errors count as failures.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    BotoCoreError,
    ClientError,
    ConnectionError,
    TimeoutError,
)


def boto3_available() -> bool:
    """Return ``True`` when boto3/botocore are importable in this environment."""
    return _BOTO3_AVAILABLE


def _build_cloudwatch_client(region_name: str | None) -> Any:
    """Build a real CloudWatch client.

    Raises:
        RuntimeError: If ``boto3`` is not available in the environment.
    """
    if not _BOTO3_AVAILABLE:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "boto3 is not available; inject a CloudWatch client for testing "
            "or install boto3 to use the real Monitor_de_Consumo adapter."
        )
    return boto3.client("cloudwatch", region_name=region_name)


@dataclass(frozen=True)
class RecordResult:
    """Outcome of a metric-recording operation for a single session.

    Attributes:
        recorded: ``True`` when the metrics were written successfully.
        session_id: The session the record belongs to (always identifies the
            affected session, including on failure — Req 7.5).
        attempts_made: Number of write attempts actually performed
            (``1..MAX_RETRIES``).
        error: A structured, session-identifying error message when
            ``recorded`` is ``False`` after all retries are exhausted (Req 7.5);
            ``None`` on success.
    """

    recorded: bool
    session_id: str
    attempts_made: int
    error: str | None = None


class MonitorAdapter:
    """I/O adapter that emits consumption metrics/responses to CloudWatch.

    The bounded-retry and failure-recording decisions are delegated to the domain
    layer; this class only performs the CloudWatch ``put_metric_data`` calls,
    bounds record retries to 3 attempts and surfaces a session-identifying error
    on exhaustion.

    Args:
        namespace: CloudWatch namespace for the emitted metrics (Req 7.3).
        cloudwatch_client: An optional pre-built CloudWatch client. When provided
            it is used as-is (enables unit testing via mocking); otherwise a real
            client is built lazily.
        region_name: AWS region used only when building a real client.
    """

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        cloudwatch_client: Any | None = None,
        region_name: str | None = None,
    ) -> None:
        if not (namespace and namespace.strip()):
            raise ValueError("namespace não pode ser vazio")
        self.namespace = namespace
        self._region_name = region_name
        self._client = cloudwatch_client
        # Immutable log of failed LLM call attempts (Req 7.4, Property 18).
        self._failures: tuple[CallRecord, ...] = ()

    @property
    def client(self) -> Any:
        """The CloudWatch client, built lazily on first use if not injected."""
        if self._client is None:
            self._client = _build_cloudwatch_client(self._region_name)
        return self._client

    @property
    def failures(self) -> tuple[CallRecord, ...]:
        """The recorded LLM call failures, in chronological order (Req 7.4)."""
        return self._failures

    # -- metric assembly ---------------------------------------------------------

    def _session_dimensions(self, session_id: str) -> list[dict[str, str]]:
        """Dimensões das métricas — nenhuma; ver :data:`METRIC_DIMENSIONS`."""
        return list(METRIC_DIMENSIONS)

    def _put_metric_data(self, metric_data: list[dict[str, Any]]) -> None:
        """Publish metric data to CloudWatch (single call)."""
        self.client.put_metric_data(
            Namespace=self.namespace,
            MetricData=metric_data,
        )

    # -- bounded retry (Req 7.5) -------------------------------------------------

    def _emit_with_retry(
        self,
        session_id: str,
        metric_data: list[dict[str, Any]],
        *,
        kind: str,
    ) -> RecordResult:
        """Emit ``metric_data`` for ``session_id``, retrying up to 3 times (Req 7.5).

        On a transient failure the write is retried while the bounded policy
        allows another attempt; once exhausted a structured error identifying the
        session is returned (Req 7.5).

        Args:
            session_id: The session the record belongs to.
            metric_data: The CloudWatch metric-data entries to publish.
            kind: Human-readable label of what is being recorded (for the error
                message), e.g. ``"tokens"`` or ``"resposta"``.

        Returns:
            A :class:`RecordResult` describing the outcome.
        """
        attempts_made = 0
        last_error: str | None = None
        while True:
            try:
                self._put_metric_data(metric_data)
            except RETRYABLE_EXCEPTIONS as exc:
                attempts_made += 1
                last_error = str(exc)
                decision = decide_retry(
                    RetryOperation.MONITOR_RECORDING,
                    attempts_made,
                    max_attempts=MAX_RETRIES,
                )
                if decision.should_retry:
                    logger.warning(
                        "Falha ao registrar %s da sessão %s (tentativa %d/%d): %s",
                        kind,
                        mascarar_contato(session_id),
                        attempts_made,
                        MAX_RETRIES,
                        exc,
                    )
                    continue
                # Req 7.5: retries exhausted -> surface a session-identifying error.
                error = (
                    f"falha ao registrar {kind} no CloudWatch para a sessão "
                    f"{mascarar_contato(session_id)!r} após {attempts_made} tentativas: "
                    f"{last_error}"
                )
                logger.error(error)
                return RecordResult(
                    recorded=False,
                    session_id=session_id,
                    attempts_made=attempts_made,
                    error=error,
                )

            attempts_made += 1
            return RecordResult(
                recorded=True,
                session_id=session_id,
                attempts_made=attempts_made,
                error=None,
            )

    # -- public recording API ----------------------------------------------------

    def record_token_usage(
        self,
        session_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> RecordResult:
        """Emit input/output token metrics for ``session_id`` (Req 7.1).

        Publishes ``InputTokens`` and ``OutputTokens`` count metrics keyed to the
        session, retrying up to 3 times on transient failure (Req 7.5).

        Args:
            session_id: The session the LLM call belongs to. Must be non-empty.
            input_tokens: Number of input (prompt) tokens consumed. Must be ``>= 0``.
            output_tokens: Number of output (completion) tokens consumed. Must be
                ``>= 0``.

        Returns:
            A :class:`RecordResult` describing the outcome.

        Raises:
            ValueError: If ``session_id`` is empty or a token count is negative.
        """
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be >= 0")

        dimensions = self._session_dimensions(session_id)
        metric_data = [
            {
                "MetricName": "InputTokens",
                "Dimensions": dimensions,
                "Value": float(input_tokens),
                "Unit": "Count",
            },
            {
                "MetricName": "OutputTokens",
                "Dimensions": dimensions,
                "Value": float(output_tokens),
                "Unit": "Count",
            },
        ]
        logger.info(
            "Tokens registrados para a sessão %s (entrada=%d, saída=%d)",
            mascarar_contato(session_id),
            input_tokens,
            output_tokens,
        )
        return self._emit_with_retry(session_id, metric_data, kind="tokens")

    def record_response(self, session_id: str, content: str) -> RecordResult:
        """Emit a response record for ``session_id`` (Req 7.2).

        Publishes a ``Responses`` count metric (value ``1``) and a
        ``ResponseLength`` metric (character count) keyed to the session, and
        makes the response content available for AgentCore observability. Retries
        up to 3 times on transient failure (Req 7.5).

        Args:
            session_id: The session the response belongs to. Must be non-empty.
            content: The response content generated by the Agente_IA.

        Returns:
            A :class:`RecordResult` describing the outcome.

        Raises:
            ValueError: If ``session_id`` is empty.
        """
        if not session_id:
            raise ValueError("session_id must be a non-empty string")

        dimensions = self._session_dimensions(session_id)
        metric_data = [
            {
                "MetricName": "Responses",
                "Dimensions": dimensions,
                "Value": 1.0,
                "Unit": "Count",
            },
            {
                "MetricName": "ResponseLength",
                "Dimensions": dimensions,
                "Value": float(len(content)),
                "Unit": "Count",
            },
        ]
        # Make the response content observable (AgentCore observability, Req 7.2/7.3).
        logger.info(
            "Resposta registrada para a sessão %s (%d caracteres)",
            mascarar_contato(session_id),
            len(content),
        )
        return self._emit_with_retry(session_id, metric_data, kind="resposta")

    def record_llm_call(
        self,
        session_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        content: str,
    ) -> tuple[RecordResult, RecordResult]:
        """Record a successful LLM call: token usage and response (Req 7.1, 7.2).

        Convenience method combining :meth:`record_token_usage` and
        :meth:`record_response`. Both records are attempted independently so a
        failure of one does not skip the other; each is retried up to 3 times
        (Req 7.5).

        Returns:
            A ``(tokens_result, response_result)`` pair of :class:`RecordResult`.
        """
        tokens_result = self.record_token_usage(session_id, input_tokens, output_tokens)
        response_result = self.record_response(session_id, content)
        return tokens_result, response_result

    def record_call_failure(
        self,
        session_id: str,
        *,
        reason: str | None = None,
    ) -> CallRecord:
        """Record a failed LLM call attempt, preserving prior records (Req 7.4).

        Appends a failure :class:`CallRecord` to the adapter's immutable failure
        log via the pure-logic :func:`record_llm_failure`, and emits a
        ``LlmCallFailures`` count metric keyed to the session so failures are
        visible in CloudWatch (best-effort; a metric-emit failure does not undo
        the recorded attempt).

        Args:
            session_id: The session whose LLM call failed. Must be non-empty.
            reason: Optional description of the failure.

        Returns:
            The newly appended failure :class:`CallRecord`.
        """
        self._failures = record_llm_failure(self._failures, session_id, reason=reason)
        new_record = self._failures[-1]

        metric_data = [
            {
                "MetricName": "LlmCallFailures",
                "Dimensions": self._session_dimensions(session_id),
                "Value": 1.0,
                "Unit": "Count",
            }
        ]
        try:
            self._put_metric_data(metric_data)
        except RETRYABLE_EXCEPTIONS as exc:  # pragma: no cover - best-effort path
            logger.warning(
                "Falha ao emitir métrica de falha de LLM da sessão %s: %s",
                mascarar_contato(session_id),
                exc,
            )
        return new_record
