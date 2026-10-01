"""Testes do adapter do Meta WhatsApp Cloud API (caminho interino de teste)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from src.adapters import whatsapp_cloud
from src.adapters.whatsapp_cloud import (
    WhatsAppCloudClient,
    parse_inbound_messages,
    verify_signature,
    verify_subscription,
)

pytestmark = pytest.mark.unit


# --- verify_subscription -------------------------------------------------------

def test_verify_subscription_returns_challenge_when_token_matches():
    params = {"hub.mode": "subscribe", "hub.verify_token": "segredo", "hub.challenge": "12345"}
    assert verify_subscription(params, "segredo") == "12345"


def test_verify_subscription_rejects_wrong_token():
    params = {"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "12345"}
    assert verify_subscription(params, "segredo") is None


def test_verify_subscription_rejects_wrong_mode_and_empty():
    assert verify_subscription({"hub.mode": "x", "hub.verify_token": "s", "hub.challenge": "c"}, "s") is None
    assert verify_subscription(None, "s") is None
    assert verify_subscription({"hub.mode": "subscribe"}, "") is None


# --- verify_signature ----------------------------------------------------------

def test_verify_signature_accepts_valid_hmac():
    body = b'{"a":1}'
    secret = "app-secret"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret) is True


def test_verify_signature_rejects_tampered_body():
    secret = "app-secret"
    sig = "sha256=" + hmac.new(secret.encode(), b'{"a":1}', hashlib.sha256).hexdigest()
    assert verify_signature(b'{"a":2}', sig, secret) is False


def test_verify_signature_disabled_without_app_secret():
    # Sem app_secret a checagem é desabilitada (verify token protege o endpoint).
    assert verify_signature(b"qualquer", None, "") is True


def test_verify_signature_rejects_missing_header_when_secret_present():
    assert verify_signature(b"x", None, "app-secret") is False


# --- parse_inbound_messages ----------------------------------------------------

def _cloud_event(*, msg_type="text", text="oi", from_number="5579999", msg_id="wamid.1"):
    inner = {"from": from_number, "id": msg_id, "type": msg_type}
    if msg_type == "text":
        inner["text"] = {"body": text}
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "PNID-1"},
                            "messages": [inner],
                        }
                    }
                ]
            }
        ]
    }


def test_parse_inbound_extracts_text_message():
    msgs = parse_inbound_messages(_cloud_event(text="Como peço férias?"))
    assert len(msgs) == 1
    m = msgs[0]
    assert m.text == "Como peço férias?"
    assert m.from_number == "5579999"
    assert m.message_id == "wamid.1"
    assert m.part_types == ("text",)
    assert m.phone_number_id == "PNID-1"


def test_parse_inbound_media_message_has_empty_text_and_media_part_type():
    msgs = parse_inbound_messages(_cloud_event(msg_type="image"))
    assert msgs[0].text == ""
    assert msgs[0].part_types == ("image",)


def test_parse_inbound_empty_payload_returns_empty():
    assert parse_inbound_messages({}) == []
    assert parse_inbound_messages({"entry": [{"changes": [{"value": {}}]}]}) == []


# --- WhatsAppCloudClient.send_text --------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_text_posts_to_graph_and_returns_message_id():
    captured = {}

    def fake_opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["method"] = req.get_method()
        return _FakeResp({"messages": [{"id": "wamid.out.1"}]})

    client = WhatsAppCloudClient("TOKEN-123", opener=fake_opener)
    mid = client.send_text("PNID-1", "5579999", "Olá!")

    assert mid == "wamid.out.1"
    assert captured["method"] == "POST"
    assert "/PNID-1/messages" in captured["url"]
    # Authorization header capitalizado por urllib -> "Authorization".
    assert captured["headers"]["Authorization"] == "Bearer TOKEN-123"
    assert captured["body"]["to"] == "5579999"
    assert captured["body"]["text"]["body"] == "Olá!"


def test_send_text_without_messages_returns_empty_id():
    client = WhatsAppCloudClient("T", opener=lambda req, timeout=None: _FakeResp({}))
    assert client.send_text("PNID", "5579", "x") == ""


# --- Erros do Graph API preservam o motivo real -------------------------------

def _http_error(status, payload):
    import urllib.error, io
    body = json.dumps(payload).encode("utf-8")
    return urllib.error.HTTPError(
        "https://graph.facebook.com/x", status, "err", {}, io.BytesIO(body)
    )


def test_send_text_expired_token_surfaces_meta_error():
    """Token expirado (code 190) deve aparecer no erro, não só 'HTTP Error 401'."""
    def opener(req, timeout=None):
        raise _http_error(401, {"error": {"message": "Session has expired", "code": 190}})

    client = WhatsAppCloudClient("TOKEN-EXPIRADO", opener=opener)
    with pytest.raises(whatsapp_cloud.WhatsAppSendError) as exc:
        client.send_text("PNID", "5549", "oi")
    assert exc.value.status == 401
    assert exc.value.code == 190
    assert "expired" in exc.value.detail.lower()
    assert "190" in str(exc.value)


def test_send_text_recipient_not_allowed_surfaces_code_131030():
    def opener(req, timeout=None):
        raise _http_error(400, {"error": {
            "message": "(#131030) Recipient phone number not in allowed list", "code": 131030}})

    client = WhatsAppCloudClient("T", opener=opener)
    with pytest.raises(whatsapp_cloud.WhatsAppSendError) as exc:
        client.send_text("PNID", "5549", "oi")
    assert exc.value.code == 131030


def test_send_text_non_json_error_body_still_reported():
    def opener(req, timeout=None):
        import urllib.error, io
        return (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"<html>boom</html>"))
        )

    client = WhatsAppCloudClient("T", opener=opener)
    with pytest.raises(whatsapp_cloud.WhatsAppSendError) as exc:
        client.send_text("PNID", "5549", "oi")
    assert exc.value.status == 500
    assert "boom" in exc.value.detail


# --- Indicador de leitura/digitação -------------------------------------------

def test_mark_read_and_typing_posts_read_status_and_indicator():
    captured = {}

    def fake_opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"success": True})

    client = WhatsAppCloudClient("TOKEN-123", opener=fake_opener)

    assert client.mark_read_and_typing("PNID-1", "wamid.in.1") is True
    assert "/PNID-1/messages" in captured["url"]
    assert captured["body"]["status"] == "read"
    assert captured["body"]["message_id"] == "wamid.in.1"
    assert captured["body"]["typing_indicator"] == {"type": "text"}


def test_mark_read_and_typing_never_breaks_the_flow():
    """É cortesia: falhar aqui não pode impedir a resposta ao usuário."""
    def opener(req, timeout=None):
        raise _http_error(400, {"error": {"message": "boom", "code": 100}})

    client = WhatsAppCloudClient("TOKEN", opener=opener)
    assert client.mark_read_and_typing("PNID", "wamid.in.2") is False


def test_mark_read_and_typing_skips_without_message_id():
    def opener(req, timeout=None):  # pragma: no cover - não deve ser chamado
        raise AssertionError("não deveria chamar a Graph API sem message_id")

    client = WhatsAppCloudClient("TOKEN", opener=opener)
    assert client.mark_read_and_typing("PNID", "") is False
