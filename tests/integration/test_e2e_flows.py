"""End-to-end integration tests of the site and WhatsApp flows (Task 14.4).

These tests wire the **real** flow classes (:class:`src.flows.site_flow.SiteFlow`
and :class:`src.flows.whatsapp_flow.WhatsAppFlow`) with the **real** I/O adapters
(:class:`~src.adapters.agent.BedrockAgentAdapter`,
:class:`~src.adapters.connect_router.ConnectRouterAdapter`,
:class:`~src.adapters.whatsapp_connector.WhatsAppConnectorAdapter` and
:class:`~src.adapters.monitor_adapter.MonitorAdapter`). Only the outermost AWS
boundary is faked:

* the Bedrock Knowledge Base client (``kb_client``) is a fake returning canned
  ``retrieve`` results with controllable similarity scores;
* the Amazon Connect, End User Messaging Social and CloudWatch boto3 clients are
  ``MagicMock`` doubles;
* the Cognito verifier and the session store are lightweight in-memory fakes
  (the session-store fake still delegates the active/inactive decision to the
  real pure-logic :func:`src.domain.session.resolve_session`).

Together they exercise the flows across service boundaries without touching AWS,
covering both the success/deliver paths and the escalation/unavailable paths.

Covered acceptance criteria:

* **Req 5.1** — site flow: authenticated request → agent answered (high
  confidence) → structured envelope delivered; the Monitor_de_Consumo records
  the response.
* **Req 3.1** — site flow: low confidence / not_found → the
  ConnectRouterAdapter escalates via Amazon Connect and the Usuario is informed.
* **Req 6.1** — WhatsApp flow: inbound text → forwarded via Amazon Connect →
  agent answered → reply delivered back over End User Messaging Social.
* WhatsApp escalation/unavailable path: forwarding to Connect fails → the
  Usuario is informed and the message is preserved (Req 6.6), and the not_found
  notice is delivered when no document is relevant (Req 2.4).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.agent import BedrockAgentAdapter
from src.adapters.connect_router import ConnectRouterAdapter
from src.adapters.monitor_adapter import MonitorAdapter
from src.adapters.whatsapp_connector import (
    FORWARDING_UNAVAILABLE_MESSAGE,
    IncomingWhatsAppMessage,
    WhatsAppConnectorAdapter,
)
from src.domain.api import AUTH_ERROR_CODE, CognitoToken
from src.domain.config import DEFAULT_TIMEOUT_MIN, AgentStatus, Channel, RoutingDecision
from src.domain.models import Session
from src.domain.session import resolve_session
from src.flows.site_flow import (
    ESCALATION_USER_MESSAGE,
    NOT_FOUND_USER_MESSAGE,
    SiteFlow,
)
from src.flows.whatsapp_flow import NOT_FOUND_MESSAGE, WhatsAppFlow

pytestmark = pytest.mark.integration

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Lightweight fakes (only at the AWS boundary) -------------------------------

class FakeVerifier:
    """Cognito verifier fake returning a fixed decoded token."""

    def __init__(self, token: CognitoToken) -> None:
        self._token = token
        self.calls: list[str | None] = []

    def verify(self, raw_token, now):  # noqa: D401 - simple passthrough
        self.calls.append(raw_token)
        return self._token


class InMemorySessionStore:
    """In-memory session store delegating active resolution to the domain layer.

    Mirrors the subset of :class:`src.adapters.session_store.SessionStore` used
    by :class:`SiteFlow` (``resolve_active_session`` + ``put_session``) without
    any DynamoDB/boto3 dependency, while reusing the real
    :func:`src.domain.session.resolve_session` so the active/inactive decision is
    faithful to production behaviour.
    """

    def __init__(self, timeout_min: int = DEFAULT_TIMEOUT_MIN) -> None:
        self._sessions: dict[str, Session] = {}
        self._timeout_min = timeout_min
        self.put_calls: list[Session] = []

    def resolve_active_session(self, user_key, channel, now, timeout_min=None):
        effective = self._timeout_min if timeout_min is None else timeout_min
        return resolve_session(
            list(self._sessions.values()), user_key, channel, now, effective
        )

    def put_session(self, session: Session) -> None:
        self._sessions[session.session_id] = session
        self.put_calls.append(session)


def valid_token(now: datetime = NOW) -> CognitoToken:
    return CognitoToken(
        present=True, signature_valid=True, expires_at=now + timedelta(hours=1)
    )


def fake_kb_client(scores) -> MagicMock:
    """Build a fake Bedrock KB client whose ``retrieve`` returns canned scores."""
    kb = MagicMock()
    kb.retrieve.return_value = {
        "retrievalResults": [
            {
                "score": s,
                "location": {"s3Location": {"uri": f"s3://progep-docs/doc-{i}.pdf"}},
            }
            for i, s in enumerate(scores)
        ]
    }
    return kb


def connect_client_ok(contact_id: str = "contact-1") -> MagicMock:
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": contact_id}
    connect.get_current_metric_data.return_value = {"MetricResults": []}
    return connect


# ================================================================================
# Site flow — success / deliver path (Req 5.1)
# ================================================================================

@pytest.mark.integration
def test_site_flow_success_delivers_envelope_and_records_response():
    """Authenticated request → answered (high confidence) → delivered + monitored."""
    verifier = FakeVerifier(valid_token())
    store = InMemorySessionStore()
    connect = connect_client_ok()
    cloudwatch = MagicMock()

    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.9]))
    router = ConnectRouterAdapter("inst-1", "flow-1", "queue-1", connect_client=connect)
    monitor = MonitorAdapter(cloudwatch_client=cloudwatch)

    flow = SiteFlow(
        verifier=verifier,
        session_store=store,
        agent=agent,
        router=router,
        monitor=monitor,
        confidence_threshold=0.5,
        relevance_threshold=0.4,
    )

    result = flow.process(
        raw_token="Bearer good", user_key="user-1",
        message="Como solicito férias?", now=NOW,
    )

    # Structured OK envelope carrying the grounded content (Req 5.1, 5.4).
    assert result.response.status == "ok"
    assert result.response.error is None
    assert result.response.content  # non-empty grounded answer
    assert result.routing_decision == RoutingDecision.DELIVER.value
    assert result.agent_response.status == AgentStatus.ANSWERED.value

    # A session was created and persisted for the site channel.
    assert result.session_created is True
    assert result.session.channel == Channel.SITE.value
    assert store.put_calls  # persisted at least once

    # The KB was actually queried through the real agent adapter.
    agent._kb_client.retrieve.assert_called_once()

    # No escalation on the deliver path.
    connect.start_chat_contact.assert_not_called()

    # The Monitor_de_Consumo recorded the response to CloudWatch (Req 7.2).
    assert result.monitor_result is not None
    assert result.monitor_result.recorded is True
    cloudwatch.put_metric_data.assert_called()


@pytest.mark.integration
def test_site_flow_blocks_unauthenticated_request_before_agent():
    """An invalid token blocks the request before any agent/KB call (Req 5.2)."""
    verifier = FakeVerifier(
        CognitoToken(present=True, signature_valid=False, expires_at=None)
    )
    store = InMemorySessionStore()
    kb = fake_kb_client([0.9])
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=kb)
    router = ConnectRouterAdapter("inst-1", "flow-1", "queue-1", connect_client=connect_client_ok())
    monitor = MonitorAdapter(cloudwatch_client=MagicMock())

    flow = SiteFlow(
        verifier=verifier, session_store=store, agent=agent, router=router,
        monitor=monitor, confidence_threshold=0.5, relevance_threshold=0.4,
    )

    result = flow.process(raw_token="Bearer bad", user_key="u", message="oi", now=NOW)

    assert result.response.status == "error"
    assert result.response.error.code == AUTH_ERROR_CODE
    kb.retrieve.assert_not_called()
    assert store.put_calls == []


# ================================================================================
# Site flow — escalation paths (Req 3.1)
# ================================================================================

@pytest.mark.integration
def test_site_flow_low_confidence_escalates_via_connect():
    """A relevant-but-low-confidence answer escalates through Amazon Connect."""
    verifier = FakeVerifier(valid_token())
    store = InMemorySessionStore()
    connect = connect_client_ok("contact-esc")
    cloudwatch = MagicMock()

    # score 0.5 is >= relevance (0.3) so answered, but < confidence (0.8) -> escalate.
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.5]))
    router = ConnectRouterAdapter("inst-1", "flow-1", "queue-1", connect_client=connect)
    monitor = MonitorAdapter(cloudwatch_client=cloudwatch)

    flow = SiteFlow(
        verifier=verifier, session_store=store, agent=agent, router=router,
        monitor=monitor, confidence_threshold=0.8, relevance_threshold=0.3,
    )

    result = flow.process(
        raw_token="Bearer good", user_key="user-2", message="dúvida difícil", now=NOW
    )

    assert result.routing_decision == RoutingDecision.ESCALATE.value
    # The real ConnectRouterAdapter enqueued the contact via Amazon Connect (Req 3.1, 3.3).
    connect.start_chat_contact.assert_called_once()
    assert result.escalation is not None
    assert result.escalation.escalated is True
    assert result.escalation.contact_id == "contact-esc"
    # The Usuario is informed with a sanitized structured envelope.
    assert result.response.status == "ok"
    assert result.response.content == ESCALATION_USER_MESSAGE
    # The full session history was transferred to Connect (Req 3.3).
    _, kwargs = connect.start_chat_contact.call_args
    assert "session_history" in kwargs["Attributes"]


@pytest.mark.integration
def test_site_flow_not_found_escalates_via_connect():
    """No relevant document → not_found → escalation and not-found notice (Req 2.4, 3.1)."""
    verifier = FakeVerifier(valid_token())
    store = InMemorySessionStore()
    connect = connect_client_ok("contact-nf")

    # All scores below the relevance threshold -> not_found.
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.1, 0.2]))
    router = ConnectRouterAdapter("inst-1", "flow-1", "queue-1", connect_client=connect)
    monitor = MonitorAdapter(cloudwatch_client=MagicMock())

    flow = SiteFlow(
        verifier=verifier, session_store=store, agent=agent, router=router,
        monitor=monitor, confidence_threshold=0.5, relevance_threshold=0.4,
    )

    result = flow.process(
        raw_token="Bearer good", user_key="user-3", message="algo obscuro", now=NOW
    )

    assert result.agent_response.status == AgentStatus.NOT_FOUND.value
    assert result.routing_decision == RoutingDecision.ESCALATE.value
    connect.start_chat_contact.assert_called_once()
    assert result.response.status == "ok"
    assert result.response.content == NOT_FOUND_USER_MESSAGE


@pytest.mark.integration
def test_site_flow_escalation_reports_unavailability_when_connect_fails():
    """When Connect is unreachable after all attempts the Usuario is still informed (Req 3.5)."""
    verifier = FakeVerifier(valid_token())
    store = InMemorySessionStore()
    connect = MagicMock()
    connect.start_chat_contact.side_effect = ConnectionError("connect down")

    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.1]))
    router = ConnectRouterAdapter("inst-1", "flow-1", "queue-1", connect_client=connect)
    monitor = MonitorAdapter(cloudwatch_client=MagicMock())

    flow = SiteFlow(
        verifier=verifier, session_store=store, agent=agent, router=router,
        monitor=monitor, confidence_threshold=0.5, relevance_threshold=0.4,
    )

    result = flow.process(
        raw_token="Bearer good", user_key="user-4", message="preciso de ajuda", now=NOW
    )

    assert result.routing_decision == RoutingDecision.ESCALATE.value
    assert result.escalation.unavailable is True
    # 3 bounded attempts were made against Amazon Connect (Req 3.5).
    assert connect.start_chat_contact.call_count == 3
    # The user still receives a sanitized, structured message (history preserved).
    assert result.response.status == "ok"
    assert result.response.content == result.escalation.user_message


# ================================================================================
# WhatsApp flow — success path (Req 6.1)
# ================================================================================

def _incoming(text: str = "qual o edital?", parts=("text",)) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        from_number="+5579999999999",
        message_id="wamid-1",
        text=text,
        part_types=tuple(parts),
        phone_number_id="phone-1",
    )


@pytest.mark.integration
def test_whatsapp_flow_success_forwards_via_connect_and_delivers_reply():
    """Inbound text → forwarded via Connect → answered → delivered back (Req 6.1, 6.2)."""
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "contact-wa"}
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-1"}

    connector = WhatsAppConnectorAdapter(
        instance_id="inst-1", contact_flow_id="flow-1",
        origination_phone_number_id="phone-1",
        connect_client=connect, social_client=social,
        sleeper=lambda _s: None,
    )
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.9]))
    flow = WhatsAppFlow(connector, agent, relevance_threshold=0.4)

    result = flow.handle_incoming(_incoming())

    # Req 6.1: forwarded to the agent through Amazon Connect.
    assert result.forwarded is True
    assert result.contact_id == "contact-wa"
    connect.start_chat_contact.assert_called_once()
    # Answered response grounded in the retrieved document.
    assert result.agent_response.status == AgentStatus.ANSWERED.value
    # Req 6.2: reply delivered back to the same WhatsApp number.
    assert result.delivery.delivered is True
    assert result.unavailable is False
    social.send_whatsapp_message.assert_called_once()
    _, kwargs = social.send_whatsapp_message.call_args
    payload = json.loads(kwargs["message"].decode("utf-8"))
    assert payload["to"] == "+5579999999999"
    assert payload["text"]["body"] == result.agent_response.content


@pytest.mark.integration
def test_whatsapp_flow_not_found_delivers_notice():
    """No relevant document → not-found notice delivered to the Usuario (Req 2.4)."""
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "contact-wa2"}
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-2"}

    connector = WhatsAppConnectorAdapter(
        instance_id="inst-1", contact_flow_id="flow-1",
        origination_phone_number_id="phone-1",
        connect_client=connect, social_client=social, sleeper=lambda _s: None,
    )
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=fake_kb_client([0.1]))
    flow = WhatsAppFlow(connector, agent, relevance_threshold=0.4)

    result = flow.handle_incoming(_incoming())

    assert result.agent_response.status == AgentStatus.NOT_FOUND.value
    assert result.delivery.delivered is True
    _, kwargs = social.send_whatsapp_message.call_args
    payload = json.loads(kwargs["message"].decode("utf-8"))
    assert payload["text"]["body"] == NOT_FOUND_MESSAGE


# ================================================================================
# WhatsApp flow — escalation / unavailable path (Req 6.6)
# ================================================================================

@pytest.mark.integration
def test_whatsapp_flow_forwarding_failure_preserves_message_and_skips_agent():
    """Connect forwarding failure → unavailability informed, message preserved (Req 6.6)."""
    connect = MagicMock()
    connect.start_chat_contact.side_effect = ConnectionError("connect down")
    social = MagicMock()

    kb = fake_kb_client([0.9])
    connector = WhatsAppConnectorAdapter(
        instance_id="inst-1", contact_flow_id="flow-1",
        origination_phone_number_id="phone-1",
        connect_client=connect, social_client=social, sleeper=lambda _s: None,
    )
    agent = BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=kb)
    flow = WhatsAppFlow(connector, agent, relevance_threshold=0.4)

    incoming = _incoming("mensagem importante")
    result = flow.handle_incoming(incoming)

    assert result.forwarded is False
    assert result.unavailable is True
    assert result.agent_response is None
    # Original message preserved unchanged (Req 6.6).
    assert result.receive.preserved_message == incoming
    assert result.receive.user_message == FORWARDING_UNAVAILABLE_MESSAGE
    # The agent/KB was never reached because forwarding failed.
    kb.retrieve.assert_not_called()
