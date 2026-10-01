"""Unit tests for the API_de_Integracao adapter (Task 12.5).

Covers authentication via Cognito (Req 5.2, 10.3), payload validation (Req 5.3),
forwarding to the Agente_IA and the structured envelope (Req 5.1, 5.4), and the
unavailability/timeout behavior (Req 5.5). The Cognito verifier and the agent
invoker are mocked so no AWS access is required.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from src.adapters import api_handler
from src.adapters.api_handler import (
    AGENT_UNAVAILABLE_CODE,
    INVALID_REQUEST_CODE,
    CognitoTokenVerifier,
    handler,
)
from src.domain.api import AUTH_ERROR_CODE, GENERIC_AUTH_ERROR_MESSAGE, CognitoToken
from src.domain.config import AgentStatus
from src.domain.models import AgentRequest, AgentResponse, SourceRef

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- Test doubles ---------------------------------------------------------------

class FakeVerifier:
    """A TokenVerifier stub returning a preconfigured CognitoToken."""

    def __init__(self, token: CognitoToken):
        self._token = token
        self.calls: list[str | None] = []

    def verify(self, raw_token, now):
        self.calls.append(raw_token)
        return self._token


def valid_token(now=NOW) -> CognitoToken:
    return CognitoToken(
        present=True,
        signature_valid=True,
        expires_at=now.replace(year=now.year + 1),
    )


def make_event(*, auth="Bearer good-token", message="Olá, preciso de ajuda"):
    headers = {}
    if auth is not None:
        headers["Authorization"] = auth
    body = None if message is None else json.dumps({"message": message})
    return {"headers": headers, "body": body}


def answering_invoker(text="Resposta fundamentada"):
    def _invoke(request: AgentRequest) -> AgentResponse:
        assert isinstance(request, AgentRequest)
        return AgentResponse(
            status=AgentStatus.ANSWERED.value,
            content=text,
            sources=(SourceRef(doc_id="doc-1", score=0.9),),
            confidence=0.9,
        )

    return _invoke


# --- Success path (Req 5.1, 5.4) -----------------------------------------------

@pytest.mark.unit
def test_authenticated_valid_request_returns_structured_ok_envelope():
    verifier = FakeVerifier(valid_token())
    invoker_calls: list[AgentRequest] = []

    def invoker(request):
        invoker_calls.append(request)
        return answering_invoker("Aqui esta a resposta")(request)

    resp = handler(make_event(), verifier=verifier, invoker=invoker, now=NOW)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "ok"
    assert body["content"] == "Aqui esta a resposta"
    assert "error" not in body
    # The request was actually forwarded to the agent on the site channel.
    assert len(invoker_calls) == 1
    assert invoker_calls[0].channel == "site"
    assert invoker_calls[0].text == "Olá, preciso de ajuda"


# --- Authentication (Req 5.2, 10.3, 10.4, Property 15) --------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "token",
    [
        CognitoToken(present=False, signature_valid=False, expires_at=None),  # absent
        CognitoToken(present=True, signature_valid=False, expires_at=None),   # invalid
        CognitoToken(present=True, signature_valid=True, expires_at=NOW),     # expired
    ],
)
def test_unauthenticated_request_is_blocked_and_not_forwarded(token):
    verifier = FakeVerifier(token)
    forwarded = []

    def invoker(request):
        forwarded.append(request)
        return answering_invoker()(request)

    resp = handler(make_event(), verifier=verifier, invoker=invoker, now=NOW)

    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["status"] == "error"
    assert body["error"]["code"] == AUTH_ERROR_CODE
    # Sanitized message with no internal detail (Req 10.4).
    assert body["error"]["message"] == GENERIC_AUTH_ERROR_MESSAGE
    # Never forwarded to the agent.
    assert forwarded == []


# --- Payload validation (Req 5.3) ----------------------------------------------

@pytest.mark.unit
def test_missing_body_is_invalid_request():
    verifier = FakeVerifier(valid_token())
    resp = handler(
        make_event(message=None), verifier=verifier, invoker=answering_invoker(), now=NOW
    )
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"]["code"] == INVALID_REQUEST_CODE


@pytest.mark.unit
def test_malformed_json_body_is_invalid_request():
    verifier = FakeVerifier(valid_token())
    event = {"headers": {"Authorization": "Bearer good"}, "body": "{not-json"}
    resp = handler(event, verifier=verifier, invoker=answering_invoker(), now=NOW)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == INVALID_REQUEST_CODE


@pytest.mark.unit
def test_empty_message_is_rejected():
    verifier = FakeVerifier(valid_token())
    resp = handler(
        make_event(message=""), verifier=verifier, invoker=answering_invoker(), now=NOW
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == INVALID_REQUEST_CODE


@pytest.mark.unit
def test_message_over_4000_chars_is_rejected_without_forwarding():
    verifier = FakeVerifier(valid_token())
    forwarded = []

    def invoker(request):
        forwarded.append(request)
        return answering_invoker()(request)

    resp = handler(
        make_event(message="a" * 4001), verifier=verifier, invoker=invoker, now=NOW
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"]["code"] == INVALID_REQUEST_CODE
    assert forwarded == []


@pytest.mark.unit
def test_message_at_4000_chars_boundary_is_accepted():
    verifier = FakeVerifier(valid_token())
    resp = handler(
        make_event(message="a" * 4000),
        verifier=verifier,
        invoker=answering_invoker("ok"),
        now=NOW,
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["content"] == "ok"


# --- Unavailability / timeout (Req 5.5) ----------------------------------------

@pytest.mark.unit
def test_agent_timeout_returns_structured_unavailability_error():
    verifier = FakeVerifier(valid_token())

    def slow_invoker(request):
        time.sleep(0.5)
        return answering_invoker()(request)

    resp = handler(
        make_event(),
        verifier=verifier,
        invoker=slow_invoker,
        now=NOW,
        agent_timeout_s=0.05,
    )
    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["status"] == "error"
    assert body["error"]["code"] == AGENT_UNAVAILABLE_CODE


@pytest.mark.unit
def test_agent_exception_returns_structured_unavailability_error():
    verifier = FakeVerifier(valid_token())

    def failing_invoker(request):
        raise RuntimeError("bedrock down")

    resp = handler(make_event(), verifier=verifier, invoker=failing_invoker, now=NOW)
    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["error"]["code"] == AGENT_UNAVAILABLE_CODE


@pytest.mark.unit
def test_missing_invoker_raises_runtime_error():
    verifier = FakeVerifier(valid_token())
    with pytest.raises(RuntimeError):
        handler(make_event(), verifier=verifier, invoker=None, now=NOW)


# --- CognitoTokenVerifier with mocked boto3 client ------------------------------

class _MockClientError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeCognitoClient:
    def __init__(self, *, raise_error=None):
        self._raise_error = raise_error
        self.calls = []

    def get_user(self, *, AccessToken):
        self.calls.append(AccessToken)
        if self._raise_error is not None:
            raise self._raise_error
        return {"Username": "user-123"}


@pytest.mark.unit
def test_cognito_verifier_absent_token_is_absent():
    verifier = CognitoTokenVerifier(client=FakeCognitoClient())
    token = verifier.verify(None, NOW)
    assert token.present is False


@pytest.mark.unit
def test_cognito_verifier_valid_token(monkeypatch):
    monkeypatch.setattr(api_handler, "ClientError", _MockClientError)
    client = FakeCognitoClient()
    verifier = CognitoTokenVerifier(client=client)
    token = verifier.verify("access-token", NOW)
    assert token.present is True
    assert token.signature_valid is True
    assert token.expires_at is not None and token.expires_at > NOW
    assert client.calls == ["access-token"]


@pytest.mark.unit
def test_cognito_verifier_expired_token(monkeypatch):
    monkeypatch.setattr(api_handler, "ClientError", _MockClientError)
    err = _MockClientError("NotAuthorizedException", "Access Token has expired")
    verifier = CognitoTokenVerifier(client=FakeCognitoClient(raise_error=err))
    token = verifier.verify("access-token", NOW)
    # Present + valid signature + expires_at <= now => classified as expired.
    assert token.present is True
    assert token.signature_valid is True
    assert token.expires_at is not None and token.expires_at <= NOW


@pytest.mark.unit
def test_cognito_verifier_invalid_token(monkeypatch):
    monkeypatch.setattr(api_handler, "ClientError", _MockClientError)
    err = _MockClientError("NotAuthorizedException", "Invalid Access Token")
    verifier = CognitoTokenVerifier(client=FakeCognitoClient(raise_error=err))
    token = verifier.verify("access-token", NOW)
    assert token.present is True
    assert token.signature_valid is False


@pytest.mark.unit
def test_end_to_end_with_mocked_cognito_client(monkeypatch):
    """Full handler path using the real CognitoTokenVerifier over a mock client."""
    monkeypatch.setattr(api_handler, "ClientError", _MockClientError)
    verifier = CognitoTokenVerifier(client=FakeCognitoClient())
    resp = handler(
        make_event(auth="Bearer valid"),
        verifier=verifier,
        invoker=answering_invoker("resposta final"),
        now=NOW,
    )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["content"] == "resposta final"


# --- CORS (integração com o site institucional) --------------------------------

@pytest.mark.unit
def test_success_response_carries_cors_headers(monkeypatch):
    """Lambda-proxy não recebe CORS do API Gateway: a função deve emiti-lo."""
    monkeypatch.delenv("CORS_ALLOW_ORIGIN", raising=False)
    from src.adapters.api_handler import _to_proxy_response
    from src.domain.models import ApiResponse

    resp = _to_proxy_response(ApiResponse(status="ok", content="oi", error=None))
    h = resp["headers"]
    assert h["Access-Control-Allow-Origin"] == "*"
    assert "x-access-token" in h["Access-Control-Allow-Headers"]
    assert "POST" in h["Access-Control-Allow-Methods"]


@pytest.mark.unit
def test_error_response_also_carries_cors_headers(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGIN", raising=False)
    from src.adapters.api_handler import _to_proxy_response
    from src.domain.models import ApiResponse, ErrorDetail

    resp = _to_proxy_response(
        ApiResponse(status="error", content=None,
                    error=ErrorDetail(code="invalid_request", message="x"))
    )
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


@pytest.mark.unit
def test_cors_origin_is_configurable(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGIN", "https://www.ifs.edu.br")
    from src.adapters.api_handler import _to_proxy_response
    from src.domain.models import ApiResponse

    resp = _to_proxy_response(ApiResponse(status="ok", content="oi", error=None))
    assert resp["headers"]["Access-Control-Allow-Origin"] == "https://www.ifs.edu.br"
