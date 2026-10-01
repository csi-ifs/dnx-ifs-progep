"""Unit tests for the WhatsApp channel orchestration (Task 14.2).

Covers the end-to-end wiring ``End User Messaging -> Amazon Connect -> Agente_IA
-> entrega``: receipt/forwarding via Connect (Req 6.1), obtaining the agent
response, and delivering the reply back to the same number with a bounded retry
(Req 6.2, 6.5). The Amazon Connect and End User Messaging Social clients are
mocked and the inter-attempt sleep is a no-op recorder so tests do not wait or
touch AWS. The agent adapter uses an injected fake Knowledge Base client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.adapters.agent import BedrockAgentAdapter
from src.adapters.whatsapp_connector import (
    FORWARDING_UNAVAILABLE_MESSAGE,
    IncomingWhatsAppMessage,
    WhatsAppConnectorAdapter,
)
from src.domain.config import (
    MAX_CHARS_WHATSAPP,
    MAX_RETRIES,
    MIN_WHATSAPP_RETRY_INTERVAL_S,
    AgentStatus,
)
from src.flows.whatsapp_flow import (
    AGENT_UNAVAILABLE_MESSAGE,
    NOT_FOUND_MESSAGE,
    WhatsAppFlow,
)

THRESHOLD = 0.5


def _connector(
    connect_client: MagicMock,
    social_client: MagicMock,
    sleeps: list | None = None,
) -> WhatsAppConnectorAdapter:
    return WhatsAppConnectorAdapter(
        instance_id="inst-1",
        contact_flow_id="flow-1",
        origination_phone_number_id="phone-1",
        connect_client=connect_client,
        social_client=social_client,
        sleeper=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def _agent(kb_client: MagicMock) -> BedrockAgentAdapter:
    return BedrockAgentAdapter(knowledge_base_id="kb-1", kb_client=kb_client)


def _kb_client_with_scores(scores) -> MagicMock:
    kb = MagicMock()
    kb.retrieve.return_value = {
        "retrievalResults": [
            {
                "score": s,
                "location": {"s3Location": {"uri": f"s3://docs/doc-{i}.pdf"}},
            }
            for i, s in enumerate(scores)
        ]
    }
    return kb


def _incoming(text: str = "qual o edital?", parts=("text",)) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        from_number="+5579999999999",
        message_id="wamid-1",
        text=text,
        part_types=tuple(parts),
        phone_number_id="phone-1",
    )


# --- Happy path: receive -> forward -> answer -> deliver (Req 6.1, 6.2) ---------

@pytest.mark.unit
def test_answered_message_is_forwarded_and_reply_delivered():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "contact-1"}
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-1"}
    kb = _kb_client_with_scores([0.9])

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming())

    # Req 6.1: forwarded to the agent via Amazon Connect.
    assert result.forwarded is True
    assert result.contact_id == "contact-1"
    connect.start_chat_contact.assert_called_once()
    # Agent produced an answered response grounded in the retrieved doc.
    assert result.agent_response is not None
    assert result.agent_response.status == AgentStatus.ANSWERED.value
    # Req 6.2: reply delivered back to the same WhatsApp number.
    assert result.delivery is not None
    assert result.delivery.delivered is True
    assert result.delivery.attempts_made == 1
    assert result.unavailable is False


# --- not_found reply text is delivered ------------------------------------------

@pytest.mark.unit
def test_not_found_delivers_notice_to_user():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "contact-2"}
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-2"}
    kb = _kb_client_with_scores([0.1])  # below threshold -> not_found

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming())

    assert result.agent_response.status == AgentStatus.NOT_FOUND.value
    assert result.delivery.delivered is True
    # The delivered body is the not-found notice.
    _, kwargs = social.send_whatsapp_message.call_args
    import json

    payload = json.loads(kwargs["message"].decode("utf-8"))
    assert payload["text"]["body"] == NOT_FOUND_MESSAGE


# --- Req 6.3: media is discarded, agent never called ----------------------------

@pytest.mark.unit
def test_media_message_short_circuits_without_calling_agent():
    connect = MagicMock()
    social = MagicMock()
    kb = MagicMock()

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming("veja a foto", parts=("text", "image")))

    assert result.forwarded is False
    assert result.agent_response is None
    assert result.delivery is None
    connect.start_chat_contact.assert_not_called()
    kb.retrieve.assert_not_called()


# --- Req 6.4: over-length message rejected before agent -------------------------

@pytest.mark.unit
def test_over_limit_message_not_forwarded():
    connect = MagicMock()
    social = MagicMock()
    kb = MagicMock()

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming("a" * (MAX_CHARS_WHATSAPP + 1)))

    assert result.forwarded is False
    assert result.agent_response is None
    kb.retrieve.assert_not_called()


# --- Req 6.6: forwarding to Connect fails -> preserve, no agent call ------------

@pytest.mark.unit
def test_forwarding_failure_preserves_message_and_skips_agent():
    connect = MagicMock()
    connect.start_chat_contact.side_effect = ConnectionError("connect down")
    social = MagicMock()
    kb = MagicMock()

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    incoming = _incoming("mensagem importante")
    result = flow.handle_incoming(incoming)

    assert result.forwarded is False
    assert result.unavailable is True
    assert result.receive.preserved_message == incoming
    assert result.receive.user_message == FORWARDING_UNAVAILABLE_MESSAGE
    kb.retrieve.assert_not_called()


# --- Req 6.5: delivery retries up to 3 times, >= 10 s between --------------------

@pytest.mark.unit
def test_reply_delivery_retries_then_succeeds():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "c"}
    social = MagicMock()
    social.send_whatsapp_message.side_effect = [
        ConnectionError("fail 1"),
        ConnectionError("fail 2"),
        {"messageId": "out-late"},
    ]
    kb = _kb_client_with_scores([0.8])
    sleeps: list = []

    flow = WhatsAppFlow(
        _connector(connect, social, sleeps), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming())

    assert result.delivery.delivered is True
    assert result.delivery.attempts_made == MAX_RETRIES
    assert sleeps == [MIN_WHATSAPP_RETRY_INTERVAL_S, MIN_WHATSAPP_RETRY_INTERVAL_S]


@pytest.mark.unit
def test_reply_delivery_exhausts_and_signals_unavailable():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "c"}
    social = MagicMock()
    social.send_whatsapp_message.side_effect = ConnectionError("down")
    kb = _kb_client_with_scores([0.8])

    flow = WhatsAppFlow(
        _connector(connect, social), _agent(kb), relevance_threshold=THRESHOLD
    )
    result = flow.handle_incoming(_incoming())

    assert result.delivery.delivered is False
    assert result.delivery.unavailable is True
    assert result.unavailable is True
    assert result.delivery.attempts_made == MAX_RETRIES


# --- Agent billing guard failure -> unavailable notice delivered ----------------

@pytest.mark.unit
def test_agent_billing_block_delivers_unavailable_notice():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "c"}
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-unavail"}
    kb = MagicMock()
    # Agent with a missing/incorrect billing annotation blocks the Bedrock call.
    agent = BedrockAgentAdapter(
        knowledge_base_id="kb-1", kb_client=kb, user_agent_extra=None
    )

    flow = WhatsAppFlow(_connector(connect, social), agent, relevance_threshold=THRESHOLD)
    result = flow.handle_incoming(_incoming())

    assert result.agent_response is None
    assert result.unavailable is True
    kb.retrieve.assert_not_called()  # blocked before the KB call
    import json

    _, kwargs = social.send_whatsapp_message.call_args
    payload = json.loads(kwargs["message"].decode("utf-8"))
    assert payload["text"]["body"] == AGENT_UNAVAILABLE_MESSAGE
