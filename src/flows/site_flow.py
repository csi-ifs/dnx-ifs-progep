"""Site-integration flow wiring (Task 14.1).

Orchestrates the site channel end-to-end, connecting the pure-logic domain with
the thin I/O adapters in the order defined by the design's "Fluxo principal —
atendimento automático (site)" sequence diagram:

``API_de_Integracao → Gestor_de_Sessao → Agente_IA → Roteador_de_Atendimento → Monitor_de_Consumo``

Steps performed by :meth:`SiteFlow.process`:

1. **Autenticação/validação (Req 5.1, 5.2, 10.3).** The Cognito token facts are
   obtained from an injected verifier and classified by the pure-logic
   :func:`src.domain.api.verify_token`; only a present, valid, non-expired token
   is forwarded. The message is then validated for the ``site`` channel
   (1..4000 characters) via :func:`src.domain.input_validation.validate_message`.
2. **Resolução de sessão (Req 4.4, 4.5, 4.6).** The active session for
   ``(user_key, "site")`` is resolved via the injected session store; when none
   exists a new one is created (:func:`src.domain.session.create_session`). The
   inbound user message is appended (:func:`src.domain.session.append_message`)
   and the session persisted.
3. **Chamada ao agente (Req 2.1).** The request is forwarded to the Agente_IA
   adapter; a failure/exception is turned into a structured unavailability error
   preserving the input (:func:`src.domain.availability.signal_unavailable`,
   Req 5.5).
4. **Decisão de roteamento (Req 3.1, 3.2).** :func:`src.domain.routing.decide_routing`
   decides between delivering the answer or escalating to a human via Amazon
   Connect; a ``not_found`` agent outcome also escalates (Req 2.4).
5. **Registro de consumo (Req 7.1, 7.2).** The generated response is recorded by
   the Monitor_de_Consumo adapter, keyed to the session.

The flow depends only on small structural ``Protocol`` interfaces, so it is fully
unit-testable by injecting mock/fake adapters — no AWS access and no boto3 are
required to exercise it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from src.domain.api import (
    CognitoToken,
    auth_error,
    build_response,
    verify_token,
)
from src.domain.availability import Dependency, signal_unavailable
from src.domain.config import AgentStatus, Channel, MessageRole, RoutingDecision
from src.domain.input_validation import validate_message
from src.domain.models import (
    AgentRequest,
    AgentResponse,
    ApiResponse,
    ErrorDetail,
    Message,
    Session,
)
from src.domain.routing import decide_routing
from src.domain.session import (
    SessionCreationError,
    append_message,
    create_session,
)

# --- Error / status contract ----------------------------------------------------

#: Error code for a malformed request or an out-of-range message (Req 5.3).
INVALID_REQUEST_CODE: str = "invalid_request"
#: Error code for an unavailable / timed-out Agente_IA (Req 5.5).
AGENT_UNAVAILABLE_CODE: str = "agent_unavailable"
#: Error code reported when session creation fails (Req 4.6).
SESSION_CREATION_FAILED_CODE: str = "session_creation_failed"

#: User-facing message shown when the interaction is transferred to a human.
ESCALATION_USER_MESSAGE: str = (
    "Estou transferindo você para um atendente humano. "
    "Aguarde um instante, por favor."
)
#: User-facing message when the agent found no relevant information (Req 2.4).
NOT_FOUND_USER_MESSAGE: str = (
    "Não encontrei essa informação na base de conhecimento. "
    "Vou transferir você para um atendente humano."
)


# --- Injected dependency contracts ----------------------------------------------

class TokenVerifier(Protocol):
    """Verifies a raw Cognito bearer token into decoded :class:`CognitoToken` facts."""

    def verify(self, raw_token: str | None, now: datetime) -> CognitoToken:  # pragma: no cover - protocol
        ...


class SessionStore(Protocol):
    """Session persistence used by the flow (subset of ``adapters.SessionStore``)."""

    def resolve_active_session(
        self,
        user_key: str,
        channel: str,
        now: datetime,
        timeout_min: int | None = None,
    ) -> Session | None:  # pragma: no cover - protocol
        ...

    def put_session(self, session: Session) -> None:  # pragma: no cover - protocol
        ...


class AgentAdapter(Protocol):
    """Agente_IA adapter used by the flow (subset of ``adapters.BedrockAgentAdapter``)."""

    def forward_request(
        self, request: AgentRequest, threshold: float
    ) -> AgentResponse:  # pragma: no cover - protocol
        ...


class RouterAdapter(Protocol):
    """Roteador_de_Atendimento adapter (subset of ``adapters.ConnectRouterAdapter``)."""

    def escalate(self, session: Session) -> Any:  # pragma: no cover - protocol
        ...


class MonitorAdapter(Protocol):
    """Monitor_de_Consumo adapter (subset of ``adapters.MonitorAdapter``)."""

    def record_response(self, session_id: str, content: str) -> Any:  # pragma: no cover - protocol
        ...


# --- Flow result ----------------------------------------------------------------

@dataclass(frozen=True)
class SiteFlowResult:
    """Outcome of running the site flow for a single request.

    Attributes:
        response: The structured API envelope to return to the caller (Req 5.4).
            Always carries a ``status`` and exactly one of ``content``/``error``.
        session: The session the request was associated with, after any updates,
            or ``None`` when the request was rejected before a session existed
            (auth/validation failure, or session-creation failure).
        session_created: ``True`` when a brand-new session was created for this
            request (Req 4.5).
        routing_decision: ``"deliver"`` or ``"escalate"`` when the agent was
            reached, else ``None``.
        agent_response: The raw :class:`AgentResponse` when the agent was
            reached, else ``None``.
        escalation: The router adapter's escalation outcome when the interaction
            was escalated, else ``None``.
        monitor_result: The monitor adapter's recording outcome when a response
            was recorded, else ``None``.
    """

    response: ApiResponse
    session: Session | None = None
    session_created: bool = False
    routing_decision: str | None = None
    agent_response: AgentResponse | None = None
    escalation: Any | None = None
    monitor_result: Any | None = None


class SiteFlow:
    """Orchestrates the site channel: API → Sessão → Agente → Roteador → Monitor.

    All collaborators are injected, so the flow can be unit-tested with fakes and
    reused in the Lambda handler wiring with the real adapters.

    Args:
        verifier: Cognito token verifier (``verify(raw_token, now) -> CognitoToken``).
        session_store: Session persistence exposing ``resolve_active_session`` and
            ``put_session``.
        agent: Agente_IA adapter exposing ``forward_request(request, threshold)``.
        router: Roteador_de_Atendimento adapter exposing ``escalate(session)``.
        monitor: Monitor_de_Consumo adapter exposing ``record_response(session_id, content)``.
        confidence_threshold: Limite_de_Confianca for the routing decision (Req 3.1).
        relevance_threshold: ``limiar_relevancia`` for the Knowledge Base query (Req 2.2, 2.4).
        timeout_min: Session inactivity timeout used to resolve active sessions
            (Req 4.3); ``None`` defers to the session store's own default.
    """

    def __init__(
        self,
        *,
        verifier: TokenVerifier,
        session_store: SessionStore,
        agent: AgentAdapter,
        router: RouterAdapter,
        monitor: MonitorAdapter,
        confidence_threshold: float,
        relevance_threshold: float,
        timeout_min: int | None = None,
    ) -> None:
        self.verifier = verifier
        self.session_store = session_store
        self.agent = agent
        self.router = router
        self.monitor = monitor
        self.confidence_threshold = confidence_threshold
        self.relevance_threshold = relevance_threshold
        self.timeout_min = timeout_min

    def process(
        self,
        *,
        raw_token: str | None,
        user_key: str,
        message: str,
        now: datetime | None = None,
        user_requested_human: bool = False,
    ) -> SiteFlowResult:
        """Run the full site flow for one authenticated message.

        Args:
            raw_token: The raw Cognito bearer token supplied with the request.
            user_key: Stable identifier of the Usuario on the site channel.
            message: The user's text message.
            now: Current instant (defaults to UTC now); used for token expiry and
                session activity.
            user_requested_human: ``True`` when the Usuario explicitly asked for a
                human attendant (Req 3.2).

        Returns:
            A :class:`SiteFlowResult` carrying the structured envelope and the
            flow metadata.
        """
        now = now or datetime.now(timezone.utc)
        channel = Channel.SITE.value

        # 1. Autenticação (Req 5.2, 10.3, 10.5).
        token = self.verifier.verify(raw_token, now)
        auth = verify_token(token, now)
        if not auth.forwarded:
            return SiteFlowResult(
                response=build_response(error=auth.error or auth_error())
            )

        # 1b. Validação de tamanho da mensagem (Req 5.1, 5.3).
        validation = validate_message(message, channel)
        if not validation.accepted:
            return SiteFlowResult(
                response=build_response(
                    error=ErrorDetail(
                        code=INVALID_REQUEST_CODE,
                        message=validation.reason or "Mensagem inválida.",
                    )
                )
            )

        # 2. Resolução/criação de sessão (Req 4.4, 4.5, 4.6).
        session = self.session_store.resolve_active_session(
            user_key, channel, now, self.timeout_min
        )
        session_created = False
        if session is None:
            try:
                session = create_session(user_key, channel, now)
            except SessionCreationError as exc:
                # Req 4.6: reject without persisting partial session data.
                return SiteFlowResult(
                    response=build_response(
                        error=ErrorDetail(
                            code=SESSION_CREATION_FAILED_CODE, message=str(exc)
                        )
                    )
                )
            session_created = True

        # Associate the inbound message with the session and persist (Req 4.2, 4.4).
        session = append_message(
            session, Message(role=MessageRole.USER.value, text=message, at=now)
        )
        self.session_store.put_session(session)

        # 3. Chamada ao Agente_IA (Req 2.1); failures preserve input (Req 5.5).
        request = AgentRequest(
            session_id=session.session_id, channel=channel, text=message
        )
        try:
            agent_response = self.agent.forward_request(
                request, self.relevance_threshold
            )
        except Exception:
            result = signal_unavailable(
                Dependency.AGENT_RESPONSE, request, timed_out=False
            )
            return SiteFlowResult(
                response=build_response(
                    error=ErrorDetail(
                        code=AGENT_UNAVAILABLE_CODE, message=result.user_message
                    )
                ),
                session=session,
                session_created=session_created,
            )

        # 5. Registro de consumo da resposta gerada (Req 7.1, 7.2).
        monitor_result = None
        if agent_response.content:
            monitor_result = self.monitor.record_response(
                session.session_id, agent_response.content
            )

        # 4. Decisão de roteamento (Req 3.1, 3.2) — not_found também escala (Req 2.4).
        decision = decide_routing(
            agent_response, self.confidence_threshold, user_requested_human
        )
        is_not_found = agent_response.status == AgentStatus.NOT_FOUND.value

        if decision == RoutingDecision.ESCALATE.value or is_not_found:
            return self._escalate(
                session=session,
                session_created=session_created,
                agent_response=agent_response,
                is_not_found=is_not_found,
                monitor_result=monitor_result,
            )

        # Deliver: record the assistant turn and return the structured answer.
        session = append_message(
            session,
            Message(
                role=MessageRole.ASSISTANT.value,
                text=agent_response.content,
                at=now,
            ),
        )
        self.session_store.put_session(session)
        return SiteFlowResult(
            response=build_response(content=agent_response.content),
            session=session,
            session_created=session_created,
            routing_decision=RoutingDecision.DELIVER.value,
            agent_response=agent_response,
            monitor_result=monitor_result,
        )

    def _escalate(
        self,
        *,
        session: Session,
        session_created: bool,
        agent_response: AgentResponse,
        is_not_found: bool,
        monitor_result: Any | None,
    ) -> SiteFlowResult:
        """Escalate the interaction to a human via the router adapter (Req 3.1-3.5).

        The full session history is transferred by the router adapter. When the
        router reports unavailability after exhausting its attempts, the user is
        still informed with the router's sanitized message while the history is
        preserved (Req 3.5).
        """
        escalation = self.router.escalate(session)

        # The router adapter (ConnectRouterAdapter) exposes ``unavailable`` and a
        # sanitized ``user_message`` when all connection attempts are exhausted.
        unavailable = bool(getattr(escalation, "unavailable", False))
        if unavailable:
            user_message = (
                getattr(escalation, "user_message", None) or ESCALATION_USER_MESSAGE
            )
        else:
            user_message = (
                NOT_FOUND_USER_MESSAGE if is_not_found else ESCALATION_USER_MESSAGE
            )

        return SiteFlowResult(
            response=build_response(content=user_message),
            session=session,
            session_created=session_created,
            routing_decision=RoutingDecision.ESCALATE.value,
            agent_response=agent_response,
            escalation=escalation,
            monitor_result=monitor_result,
        )
