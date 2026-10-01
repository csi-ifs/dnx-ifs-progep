"""Unit tests for the site-integration flow wiring (Task 14.1).

Exercises the orchestration in :class:`src.flows.site_flow.SiteFlow` with fake
adapters (no AWS / no boto3), covering the deliver path, escalation paths (low
confidence, explicit human request, not_found), authentication blocking,
payload validation, session creation failure and agent unavailability. The wiring
order API → Sessão → Agente → Roteador → Monitor is asserted along the way.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.api import AUTH_ERROR_CODE, CognitoToken
from src.domain.config import AgentStatus, Channel, RoutingDecision
from src.domain.models import AgentRequest, AgentResponse, Session, SourceRef
from src.domain.session import create_session
from src.flows.site_flow import (
    AGENT_UNAVAILABLE_CODE,
    INVALID_REQUEST_CODE,
    SESSION_CREATION_FAILED_CODE,
    SiteFlow,
)

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Fakes ----------------------------------------------------------------------

class FakeVerifier:
    def __init__(self, token: CognitoToken):
        self._token = token
        self.calls: list[str | None] = []

    def verify(self, raw_token, now):
        self.calls.append(raw_token)
        return self._token


class FakeSessionStore:
    """In-memory session store recording resolve/put calls."""

    def __init__(self, existing: Session | None = None):
        self._existing = existing
        self.put_calls: list[Session] = []
        self.resolve_calls: list[tuple[str, str]] = []

    def resolve_active_session(self, user_key, channel, now, timeout_min=None):
        self.resolve_calls.append((user_key, channel))
        return self._existing

    def put_session(self, session):
        self.put_calls.append(session)


class FakeAgent:
    def __init__(self, response: AgentResponse | None = None, *, exc: Exception | None = None):
        self._response = response
        self._exc = exc
        self.calls: list[AgentRequest] = []

    def forward_request(self, request, threshold):
        self.calls.append(request)
        if self._exc is not None:
            raise self._exc
        return self._response


class FakeEscalation:
    def __init__(self, *, unavailable=False, user_message=None):
        self.unavailable = unavailable
        self.user_message = user_message


class FakeRouter:
    def __init__(self, result: FakeEscalation | None = None):
        self._result = result or FakeEscalation()
        self.calls: list[Session] = []

    def escalate(self, session):
        self.calls.append(session)
        return self._result


class FakeMonitor:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def record_response(self, session_id, content):
        self.calls.append((session_id, content))
        return {"recorded": True, "session_id": session_id}


def valid_token(now=NOW) -> CognitoToken:
    return CognitoToken(present=True, signature_valid=True, expires_at=now.replace(year=now.year + 1))


def answered(content="Resposta fundamentada", confidence=0.9) -> AgentResponse:
    return AgentResponse(
        status=AgentStatus.ANSWERED.value,
        content=content,
        sources=(SourceRef(doc_id="doc-1", score=0.95),),
        confidence=confidence,
    )


def make_flow(*, verifier, session_store, agent, router, monitor,
              confidence_threshold=0.5, relevance_threshold=0.4):
    return SiteFlow(
        verifier=verifier,
        session_store=session_store,
        agent=agent,
        router=router,
        monitor=monitor,
        confidence_threshold=confidence_threshold,
        relevance_threshold=relevance_threshold,
    )


# --- Deliver path (Req 5.1, 4.5, 2.1, 7.2) --------------------------------------

@pytest.mark.unit
def test_deliver_path_wires_all_components_and_creates_session():
    verifier = FakeVerifier(valid_token())
    store = FakeSessionStore(existing=None)
    agent = FakeAgent(answered("Aqui está a resposta"))
    router = FakeRouter()
    monitor = FakeMonitor()
    flow = make_flow(verifier=verifier, session_store=store, agent=agent,
                     router=router, monitor=monitor)

    result = flow.process(raw_token="Bearer good", user_key="user-1",
                          message="Como solicito férias?", now=NOW)

    # Structured OK envelope with the agent content (Req 5.4).
    assert result.response.status == "ok"
    assert result.response.content == "Aqui está a resposta"
    assert result.routing_decision == RoutingDecision.DELIVER.value

    # Session was created (no active session) and associated to site channel.
    assert result.session_created is True
    assert result.session is not None
    assert result.session.channel == Channel.SITE.value
    assert store.resolve_calls == [("user-1", Channel.SITE.value)]

    # Agent was forwarded the request on the site channel.
    assert len(agent.calls) == 1
    assert agent.calls[0].channel == Channel.SITE.value
    assert agent.calls[0].text == "Como solicito férias?"

    # Monitor recorded the response for the session (Req 7.2).
    assert monitor.calls == [(result.session.session_id, "Aqui está a resposta")]

    # No escalation on the deliver path.
    assert router.calls == []

    # Session persisted twice: after inbound user msg and after assistant reply.
    assert len(store.put_calls) == 2
    roles = [m.role for m in store.put_calls[-1].messages]
    assert roles == ["user", "assistant"]


@pytest.mark.unit
def test_deliver_path_reuses_existing_active_session():
    existing = create_session("user-1", Channel.SITE.value, NOW)
    verifier = FakeVerifier(valid_token())
    store = FakeSessionStore(existing=existing)
    flow = make_flow(verifier=verifier, session_store=store,
                     agent=FakeAgent(answered()), router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="user-1",
                          message="Olá", now=NOW)

    assert result.session_created is False
    assert result.session.session_id == existing.session_id


# --- Escalation paths (Req 3.1, 3.2, 2.4) ---------------------------------------

@pytest.mark.unit
def test_low_confidence_escalates_to_router():
    verifier = FakeVerifier(valid_token())
    store = FakeSessionStore()
    agent = FakeAgent(answered(confidence=0.1))  # below threshold 0.5
    router = FakeRouter()
    monitor = FakeMonitor()
    flow = make_flow(verifier=verifier, session_store=store, agent=agent,
                     router=router, monitor=monitor, confidence_threshold=0.5)

    result = flow.process(raw_token="Bearer good", user_key="u", message="oi", now=NOW)

    assert result.routing_decision == RoutingDecision.ESCALATE.value
    assert len(router.calls) == 1
    assert result.response.status == "ok"  # user informed, structured envelope


@pytest.mark.unit
def test_explicit_human_request_escalates_even_with_high_confidence():
    verifier = FakeVerifier(valid_token())
    router = FakeRouter()
    flow = make_flow(verifier=verifier, session_store=FakeSessionStore(),
                     agent=FakeAgent(answered(confidence=0.99)), router=router,
                     monitor=FakeMonitor(), confidence_threshold=0.5)

    result = flow.process(raw_token="Bearer good", user_key="u", message="quero um humano",
                          now=NOW, user_requested_human=True)

    assert result.routing_decision == RoutingDecision.ESCALATE.value
    assert len(router.calls) == 1


@pytest.mark.unit
def test_not_found_response_escalates():
    verifier = FakeVerifier(valid_token())
    router = FakeRouter()
    not_found = AgentResponse(status=AgentStatus.NOT_FOUND.value, content="", sources=(), confidence=0.0)
    flow = make_flow(verifier=verifier, session_store=FakeSessionStore(),
                     agent=FakeAgent(not_found), router=router, monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="u", message="algo obscuro", now=NOW)

    assert result.routing_decision == RoutingDecision.ESCALATE.value
    assert len(router.calls) == 1


@pytest.mark.unit
def test_escalation_unavailable_reports_router_message():
    verifier = FakeVerifier(valid_token())
    router = FakeRouter(FakeEscalation(unavailable=True, user_message="Sem atendentes agora."))
    flow = make_flow(verifier=verifier, session_store=FakeSessionStore(),
                     agent=FakeAgent(answered(confidence=0.1)), router=router,
                     monitor=FakeMonitor(), confidence_threshold=0.5)

    result = flow.process(raw_token="Bearer good", user_key="u", message="oi", now=NOW)

    assert result.response.status == "ok"
    assert result.response.content == "Sem atendentes agora."


# --- Authentication blocking (Req 5.2, 10.3) ------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("token", [
    CognitoToken(present=False, signature_valid=False, expires_at=None),
    CognitoToken(present=True, signature_valid=False, expires_at=None),
    CognitoToken(present=True, signature_valid=True, expires_at=NOW),  # expired
])
def test_invalid_auth_is_blocked_before_agent(token):
    agent = FakeAgent(answered())
    store = FakeSessionStore()
    flow = make_flow(verifier=FakeVerifier(token), session_store=store,
                     agent=agent, router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer bad", user_key="u", message="oi", now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == AUTH_ERROR_CODE
    # Never reached session/agent.
    assert store.resolve_calls == []
    assert agent.calls == []


# --- Payload validation (Req 5.3) ------------------------------------------------

@pytest.mark.unit
def test_empty_message_rejected_without_agent():
    agent = FakeAgent(answered())
    flow = make_flow(verifier=FakeVerifier(valid_token()), session_store=FakeSessionStore(),
                     agent=agent, router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="u", message="", now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == INVALID_REQUEST_CODE
    assert agent.calls == []


@pytest.mark.unit
def test_message_over_limit_rejected_without_agent():
    agent = FakeAgent(answered())
    flow = make_flow(verifier=FakeVerifier(valid_token()), session_store=FakeSessionStore(),
                     agent=agent, router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="u", message="a" * 4001, now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == INVALID_REQUEST_CODE
    assert agent.calls == []


# --- Session creation failure (Req 4.6) -----------------------------------------

@pytest.mark.unit
def test_session_creation_failure_returns_structured_error():
    store = FakeSessionStore(existing=None)
    agent = FakeAgent(answered())
    # Blank user_key causes create_session to fail.
    flow = make_flow(verifier=FakeVerifier(valid_token()), session_store=store,
                     agent=agent, router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="   ", message="oi", now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == SESSION_CREATION_FAILED_CODE
    # No partial persistence and agent never reached (Req 4.6).
    assert store.put_calls == []
    assert agent.calls == []


# --- Agent unavailability (Req 5.5) ---------------------------------------------

@pytest.mark.unit
def test_agent_exception_returns_unavailability_error_preserving_session():
    store = FakeSessionStore(existing=None)
    agent = FakeAgent(exc=RuntimeError("bedrock down"))
    flow = make_flow(verifier=FakeVerifier(valid_token()), session_store=store,
                     agent=agent, router=FakeRouter(), monitor=FakeMonitor())

    result = flow.process(raw_token="Bearer good", user_key="u", message="oi", now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == AGENT_UNAVAILABLE_CODE
    # The inbound message was still persisted (input preserved) before the call.
    assert len(store.put_calls) == 1
