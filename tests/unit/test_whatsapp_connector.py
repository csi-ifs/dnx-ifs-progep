"""Unit tests for the Conector_WhatsApp adapter (Task 12.6).

Covers Req 6.1 (forward text to the agent via Amazon Connect), 6.2 (deliver the
response to the same WhatsApp number), 6.3 (discard non-text media with an
"apenas texto" warning, no forwarding), 6.4 (reject messages > 4096 chars),
6.5 (deliver retry <= 3 with >= 10 s between attempts) and 6.6 (preserve the
received message and signal unavailability on forwarding failure). The Amazon
Connect and End User Messaging Social clients are mocked so no AWS access is
needed, and the inter-attempt sleep is a no-op recorder so tests do not wait.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.adapters.whatsapp_connector import (
    FORWARDING_UNAVAILABLE_MESSAGE,
    IncomingWhatsAppMessage,
    WhatsAppConnectorAdapter,
)
from src.domain.config import MAX_CHARS_WHATSAPP, MAX_RETRIES, MIN_WHATSAPP_RETRY_INTERVAL_S
from src.domain.input_validation import TEXT_ONLY_WARNING


def _adapter(
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


def _incoming(text: str = "ola", parts=("text",)) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        from_number="+5579999999999",
        message_id="wamid-1",
        text=text,
        part_types=tuple(parts),
        phone_number_id="phone-1",
    )


# --- Req 6.1: forward text via Connect ------------------------------------------

@pytest.mark.unit
def test_text_message_forwarded_to_agent_via_connect():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "contact-1"}
    social = MagicMock()

    result = _adapter(connect, social).receive_message(_incoming("qual o edital?"))

    assert result.forwarded is True
    assert result.contact_id == "contact-1"
    assert result.unavailable is False
    assert result.warning is None
    # Forwarded through Amazon Connect carrying the WhatsApp origin (Req 6.1).
    kwargs = connect.start_chat_contact.call_args.kwargs
    assert kwargs["Attributes"]["channel"] == "whatsapp"
    assert kwargs["Attributes"]["whatsapp_from"] == "+5579999999999"
    assert kwargs["InitialMessage"]["Content"] == "qual o edital?"


# --- Req 6.3: discard non-text media --------------------------------------------

@pytest.mark.unit
def test_media_message_discarded_and_not_forwarded():
    connect = MagicMock()
    social = MagicMock()

    result = _adapter(connect, social).receive_message(
        _incoming("veja a foto", parts=("text", "image"))
    )

    # Req 6.3: media discarded, user warned, nothing forwarded.
    assert result.forwarded is False
    assert result.warning == TEXT_ONLY_WARNING
    assert result.discarded_media == ("image",)
    connect.start_chat_contact.assert_not_called()
    # The "apenas texto" warning was sent back to the user.
    social.send_whatsapp_message.assert_called_once()


@pytest.mark.unit
def test_audio_and_document_media_are_discarded():
    connect = MagicMock()
    social = MagicMock()

    result = _adapter(connect, social).receive_message(
        _incoming("", parts=("audio", "document"))
    )

    assert result.forwarded is False
    assert set(result.discarded_media) == {"audio", "document"}
    connect.start_chat_contact.assert_not_called()


# --- Req 6.4: 4096 char limit ---------------------------------------------------

@pytest.mark.unit
def test_message_exceeding_4096_chars_is_rejected():
    connect = MagicMock()
    social = MagicMock()

    over = "a" * (MAX_CHARS_WHATSAPP + 1)
    result = _adapter(connect, social).receive_message(_incoming(over))

    # Req 6.4: rejected with a limit warning, not forwarded.
    assert result.forwarded is False
    assert result.warning is not None
    assert str(MAX_CHARS_WHATSAPP) in result.warning
    connect.start_chat_contact.assert_not_called()


@pytest.mark.unit
def test_message_at_4096_chars_is_forwarded():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {"ContactId": "c-ok"}
    social = MagicMock()

    exact = "a" * MAX_CHARS_WHATSAPP
    result = _adapter(connect, social).receive_message(_incoming(exact))

    assert result.forwarded is True


# --- Req 6.6: forwarding failure preserves message ------------------------------

@pytest.mark.unit
def test_forwarding_failure_signals_unavailable_and_preserves_message():
    connect = MagicMock()
    connect.start_chat_contact.side_effect = ConnectionError("connect down")
    social = MagicMock()

    incoming = _incoming("mensagem importante")
    result = _adapter(connect, social).receive_message(incoming)

    # Req 6.6: unavailability signalled, message preserved unchanged.
    assert result.forwarded is False
    assert result.unavailable is True
    assert result.user_message == FORWARDING_UNAVAILABLE_MESSAGE
    assert result.preserved_message == incoming


# --- Req 6.2 / 6.5: delivery and retry ------------------------------------------

@pytest.mark.unit
def test_response_delivered_to_same_number():
    connect = MagicMock()
    social = MagicMock()
    social.send_whatsapp_message.return_value = {"messageId": "out-1"}

    result = _adapter(connect, social).deliver_response("+5579999999999", "resposta")

    # Req 6.2: delivered to the same WhatsApp number.
    assert result.delivered is True
    assert result.message_id == "out-1"
    assert result.attempts_made == 1
    kwargs = social.send_whatsapp_message.call_args.kwargs
    payload = json.loads(kwargs["message"].decode("utf-8"))
    assert payload["to"] == "+5579999999999"
    assert payload["text"]["body"] == "resposta"


@pytest.mark.unit
def test_delivery_retries_up_to_three_times_then_succeeds():
    connect = MagicMock()
    social = MagicMock()
    social.send_whatsapp_message.side_effect = [
        ConnectionError("fail 1"),
        ConnectionError("fail 2"),
        {"messageId": "out-late"},
    ]
    sleeps: list = []

    result = _adapter(connect, social, sleeps).deliver_response("+55", "r")

    # Req 6.5: at most 3 attempts, delivered on the third.
    assert result.delivered is True
    assert result.attempts_made == MAX_RETRIES
    assert social.send_whatsapp_message.call_count == MAX_RETRIES
    # Two inter-attempt waits, each >= 10 s (Req 6.5).
    assert sleeps == [MIN_WHATSAPP_RETRY_INTERVAL_S, MIN_WHATSAPP_RETRY_INTERVAL_S]
    assert all(w >= MIN_WHATSAPP_RETRY_INTERVAL_S for w in result.waits_s)


@pytest.mark.unit
def test_delivery_exhausts_attempts_and_signals_unavailable():
    connect = MagicMock()
    social = MagicMock()
    social.send_whatsapp_message.side_effect = ConnectionError("down")
    sleeps: list = []

    result = _adapter(connect, social, sleeps).deliver_response("+55", "r")

    # Req 6.5: never exceeds 3 attempts; signals unavailability on exhaustion.
    assert result.delivered is False
    assert result.unavailable is True
    assert result.attempts_made == MAX_RETRIES
    assert social.send_whatsapp_message.call_count == MAX_RETRIES
    assert result.user_message == FORWARDING_UNAVAILABLE_MESSAGE
    # Waits happen only between attempts (2 waits for 3 attempts), each >= 10 s.
    assert sleeps == [MIN_WHATSAPP_RETRY_INTERVAL_S, MIN_WHATSAPP_RETRY_INTERVAL_S]


@pytest.mark.unit
def test_min_delivery_interval_is_ten_seconds():
    # Req 6.5: the minimum interval between delivery attempts is 10 s.
    assert MIN_WHATSAPP_RETRY_INTERVAL_S == 10


@pytest.mark.unit
def test_module_imports_without_boto3():
    # The module must import even if boto3 is absent (guarded import).
    import src.adapters.whatsapp_connector as mod

    assert hasattr(mod, "WhatsAppConnectorAdapter")
