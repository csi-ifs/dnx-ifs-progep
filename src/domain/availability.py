"""Failure/unavailability handling for dependency calls (pure-logic layer).

Implements the pure-logic that turns a *failure* or *timeout* signal from a
downstream dependency into a **structured unavailability result** while
**preserving the original input unchanged**. This covers three scenarios of the
design's Error Handling table:

* Knowledge Base query failure/timeout (Req 2.5): inform the Usuario that the
  service is temporarily unavailable, preserve the original request and trigger
  the Roteador_de_Atendimento (escalation).
* Agente_IA unavailable / no response within the timeout at the API boundary
  (Req 5.5): return a structured unavailability/timeout error without losing the
  original request.
* Amazon Connect forwarding failure from the WhatsApp connector (Req 6.6):
  inform the Usuario of the temporary unavailability and preserve the received
  message.

This module is intentionally free of any I/O so it can be exercised by
property-based tests (Property 9). The actual retry/escalation/delivery calls to
AWS services live in separate adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.domain.config import AgentStatus

#: User-facing message shown when a dependency is temporarily unavailable.
#: Kept free of internal/technical detail per the sanitization principle (Req 10.4).
UNAVAILABLE_USER_MESSAGE: str = (
    "O serviço está temporariamente indisponível. Por favor, tente novamente "
    "em instantes."
)


class Dependency(str, Enum):
    """The downstream dependency whose call failed or timed out."""

    #: Consulta à Base_de_Conhecimento (Req 2.5).
    KB_QUERY = "kb_query"
    #: Resposta do Agente_IA no limite da API_de_Integracao (Req 5.5).
    AGENT_RESPONSE = "agent_response"
    #: Encaminhamento ao Amazon Connect pelo Conector_WhatsApp (Req 6.6).
    CONNECT_FORWARDING = "connect_forwarding"


class FailureKind(str, Enum):
    """Whether the dependency signalled an error or a timeout expiration."""

    FAILURE = "failure"
    TIMEOUT = "timeout"


#: Default escalation behaviour per dependency.
#:
#: A Knowledge Base failure must trigger the Roteador_de_Atendimento (Req 2.5).
#: An agent-response failure surfaces a structured error at the API boundary
#: (Req 5.5) and a Connect forwarding failure cannot be escalated *through*
#: Connect (it is the failing dependency, Req 6.6); both default to no escalation.
_DEFAULT_ESCALATE: dict[Dependency, bool] = {
    Dependency.KB_QUERY: True,
    Dependency.AGENT_RESPONSE: False,
    Dependency.CONNECT_FORWARDING: False,
}


@dataclass(frozen=True)
class UnavailabilityResult:
    """Structured outcome signalled when a dependency fails or times out.

    Attributes:
        status: Always :attr:`AgentStatus.UNAVAILABLE` value (``"unavailable"``);
            a machine-readable marker of the unavailable condition.
        dependency: The dependency that failed (``kb_query`` | ``agent_response``
            | ``connect_forwarding``).
        failure_kind: Whether the dependency signalled a ``failure`` or a
            ``timeout``.
        user_message: A sanitized, user-facing message about the temporary
            unavailability (no internal/technical detail, Req 10.4).
        escalate: ``True`` when the Roteador_de_Atendimento must be triggered as
            a consequence of the unavailability (Req 2.5).
        original_input: The original request or message, preserved **unchanged**
            so it can be re-processed, escalated or redelivered without loss
            (Req 2.5, 5.5, 6.6).
    """

    status: str
    dependency: str
    failure_kind: str
    user_message: str
    escalate: bool
    original_input: Any


def signal_unavailable(
    dependency: Dependency,
    original_input: Any,
    *,
    timed_out: bool = False,
    escalate: bool | None = None,
    user_message: str = UNAVAILABLE_USER_MESSAGE,
) -> UnavailabilityResult:
    """Build a structured unavailability result preserving the original input.

    Given a failure or timeout signal from a downstream dependency (Knowledge
    Base query, Agente_IA response, or Amazon Connect forwarding), produce a
    structured :class:`UnavailabilityResult` that signals the unavailability and
    carries the ``original_input`` unchanged (Req 2.5, 5.5, 6.6; Property 9).

    Args:
        dependency: The dependency that failed or timed out.
        original_input: The original request or message to preserve unchanged.
        timed_out: ``True`` when the signal is a timeout expiration rather than a
            generic failure; controls :attr:`UnavailabilityResult.failure_kind`.
        escalate: Optional override for the escalation flag. When ``None`` the
            dependency-specific default is used (Knowledge Base failures escalate
            to the Roteador_de_Atendimento per Req 2.5).
        user_message: Sanitized user-facing message. Defaults to
            :data:`UNAVAILABLE_USER_MESSAGE`.

    Returns:
        An :class:`UnavailabilityResult` with ``status`` set to
        :attr:`AgentStatus.UNAVAILABLE` and the original input preserved.

    Raises:
        TypeError: If ``dependency`` is not a :class:`Dependency` member.
    """
    if not isinstance(dependency, Dependency):
        raise TypeError(f"dependency must be a Dependency, got {type(dependency)!r}")

    resolved_escalate = (
        _DEFAULT_ESCALATE[dependency] if escalate is None else escalate
    )
    failure_kind = FailureKind.TIMEOUT if timed_out else FailureKind.FAILURE

    return UnavailabilityResult(
        status=AgentStatus.UNAVAILABLE.value,
        dependency=dependency.value,
        failure_kind=failure_kind.value,
        user_message=user_message,
        escalate=resolved_escalate,
        original_input=original_input,
    )
