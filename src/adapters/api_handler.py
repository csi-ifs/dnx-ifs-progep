"""I/O adapter for the API_de_Integracao (API Gateway + Lambda) with Cognito.

Implements Task 12.5 of the Assistente PROGEP-IFS spec. This is the thin I/O
adapter that sits behind an Amazon API Gateway proxy integration and drives the
site-integration flow using the pure-logic layer:

1. **Authentication (Req 5.2, 10.3).** The ``Authorization`` header is verified
   against Amazon Cognito by a :class:`TokenVerifier`. The decoded token facts
   are handed to :func:`src.domain.api.verify_token`, which forwards the request
   to the Agente_IA **if and only if** the token is present, valid and not
   expired. Any other state blocks the request with a sanitized authentication
   error (Req 10.4) without forwarding.
2. **Payload validation (Req 5.3).** The request body is parsed and the message
   is validated with :func:`src.domain.input_validation.validate_message` for
   the ``site`` channel (1..4000 characters). A malformed body or an
   out-of-range message yields a structured ``invalid_request`` error without
   forwarding.
3. **Forwarding (Req 5.1).** The request is forwarded to the Agente_IA via an
   injected :class:`AgentInvoker`. The target is a handoff within 2 seconds; the
   agent response is awaited under a hard 30-second timeout.
4. **Unavailability (Req 5.5).** If the Agente_IA is unavailable or does not
   respond within 30 seconds, a structured unavailability error is returned via
   :func:`src.domain.availability.signal_unavailable`.

Every response is a structured envelope built by
:func:`src.domain.api.build_response` (Req 5.4) and wrapped as an API Gateway
proxy response.

The ``boto3`` import is guarded so this module imports even when ``boto3`` is
absent (e.g. in a test environment without AWS). The Cognito verification and
agent invocation are injected dependencies, so the handler is fully unit-testable
by passing mocks; the default implementations are only constructed lazily when
no override is supplied.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from src.domain.api import (
    AUTH_ERROR_CODE,
    CognitoToken,
    auth_error,
    build_response,
    verify_token,
)
from src.domain.availability import Dependency, signal_unavailable
from src.domain.config import Channel
from src.domain.input_validation import validate_message
from src.domain.models import AgentRequest, AgentResponse, ApiResponse, ErrorDetail

logger = logging.getLogger(__name__)

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3 is unavailable. The default adapters that
# rely on boto3 are only constructed when the handler is invoked without injected
# dependencies, so importing this module never requires boto3 to be present.
try:  # pragma: no cover - trivial import guard
    import boto3  # type: ignore
    from botocore.exceptions import ClientError  # type: ignore

    _BOTO3_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only when boto3 is missing
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore
    _BOTO3_AVAILABLE = False


# --- Error contract -------------------------------------------------------------

#: Error code for a malformed request or an out-of-range message (Req 5.3).
INVALID_REQUEST_CODE: str = "invalid_request"
#: Error code for an unavailable / timed-out Agente_IA (Req 5.5).
AGENT_UNAVAILABLE_CODE: str = "agent_unavailable"

#: Hard timeout, in seconds, for the Agente_IA response (Req 5.5).
AGENT_TIMEOUT_S: float = 30.0
#: Target handoff latency, in seconds, for forwarding to the Agente_IA (Req 5.1).
#: Enforced as a soft target: forwarding faster than this is the design goal, but
#: the authoritative hard limit for the whole call is :data:`AGENT_TIMEOUT_S`.
FORWARD_TARGET_S: float = 2.0

#: Mapping from structured error codes to HTTP status codes for the proxy result.
_HTTP_STATUS_BY_CODE: dict[str, int] = {
    AUTH_ERROR_CODE: 401,
    INVALID_REQUEST_CODE: 400,
    AGENT_UNAVAILABLE_CODE: 503,
}


# --- Injected dependency contracts ---------------------------------------------

class TokenVerifier(Protocol):
    """Verifies a raw Cognito bearer token into decoded :class:`CognitoToken` facts.

    Implementations perform the network/JWKS verification against Amazon Cognito
    and MUST NOT raise for an invalid/expired token; instead they return a
    :class:`CognitoToken` describing what was observed, which the pure logic in
    :func:`src.domain.api.verify_token` then classifies.
    """

    def verify(self, raw_token: str | None, now: datetime) -> CognitoToken:  # pragma: no cover - protocol
        ...


class AgentInvoker(Protocol):
    """Forwards an :class:`AgentRequest` to the Agente_IA and returns its response."""

    def __call__(self, request: AgentRequest) -> AgentResponse:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class CognitoTokenVerifier:
    """Default :class:`TokenVerifier` backed by Amazon Cognito via boto3.

    The access token is validated by calling ``cognito-idp:GetUser``. A
    successful call means the token is present, valid and not expired. A
    ``NotAuthorizedException`` whose message indicates expiry is classified as
    expired; any other authorization failure is classified as an invalid
    signature. A missing token is classified as absent. This keeps the adapter
    free of JWT/JWKS handling while producing the :class:`CognitoToken` facts the
    pure logic needs.
    """

    client: Any

    @classmethod
    def create(cls, *, region_name: str | None = None) -> "CognitoTokenVerifier":
        """Build a verifier with a real boto3 ``cognito-idp`` client.

        Raises:
            RuntimeError: If boto3 is not installed in the runtime.
        """
        if not _BOTO3_AVAILABLE:
            raise RuntimeError(
                "boto3 is required for the default CognitoTokenVerifier; "
                "inject a TokenVerifier for environments without boto3."
            )
        client = boto3.client("cognito-idp", region_name=region_name)  # type: ignore
        return cls(client=client)

    def verify(self, raw_token: str | None, now: datetime) -> CognitoToken:
        if not raw_token:
            return CognitoToken(present=False, signature_valid=False, expires_at=None)
        try:
            self.client.get_user(AccessToken=raw_token)
        except ClientError as exc:  # type: ignore[misc]
            code = _client_error_code(exc)
            message = _client_error_message(exc)
            if code == "NotAuthorizedException" and "expired" in message.lower():
                # Present and well-formed but past its expiry (Req 10.5).
                return CognitoToken(
                    present=True,
                    signature_valid=True,
                    expires_at=now,  # <= now => classified as expired
                )
            # Any other authorization failure: treat as an invalid token.
            return CognitoToken(present=True, signature_valid=False, expires_at=None)
        except Exception:
            # Unknown verification failure: fail closed as invalid (Req 10.4).
            return CognitoToken(present=True, signature_valid=False, expires_at=None)
        # Valid, non-expired token. expires_at strictly in the future.
        return CognitoToken(
            present=True,
            signature_valid=True,
            expires_at=now.replace(year=now.year + 1),
        )


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return ""


def _client_error_message(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Message", ""))
    return str(exc)


# --- Event parsing helpers ------------------------------------------------------

#: Header alternativo com o *access token* do Cognito. O header ``Authorization``
#: carrega o *ID token* exigido pelo authorizer do API Gateway; a verificação
#: interna (``cognito-idp get_user``) requer o *access token*, enviado aqui.
ACCESS_TOKEN_HEADER: str = "x-access-token"


def _extract_bearer_token(event: Mapping[str, Any]) -> str | None:
    """Extract the raw Cognito access token from the request headers.

    Prefers the dedicated ``x-access-token`` header (the ``Authorization``
    header is consumed by the API Gateway Cognito authorizer, which requires
    the *ID token*); falls back to ``Authorization`` for direct invocations.
    Header lookup is case-insensitive and a ``Bearer `` prefix is stripped.
    Returns ``None`` when no token is supplied.
    """
    headers = event.get("headers") or {}
    if not isinstance(headers, Mapping):
        return None
    by_name = {
        key.lower(): value
        for key, value in headers.items()
        if isinstance(key, str)
    }
    raw = by_name.get(ACCESS_TOKEN_HEADER) or by_name.get("authorization")
    if not raw or not isinstance(raw, str):
        return None
    token = raw.strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer "):].strip()
    return token or None


def _parse_message(event: Mapping[str, Any]) -> tuple[str | None, ErrorDetail | None]:
    """Parse and extract the ``message`` field from the request body.

    Returns ``(message, None)`` on success or ``(None, error)`` when the body is
    missing, is not valid JSON, is not an object, or lacks a string ``message``
    field (Req 5.3).
    """
    body = event.get("body")
    if body is None:
        return None, ErrorDetail(
            code=INVALID_REQUEST_CODE,
            message="Requisicao malformada: corpo ausente.",
        )
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except Exception:
            return None, ErrorDetail(
                code=INVALID_REQUEST_CODE,
                message="Requisicao malformada: corpo ilegivel.",
            )
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            return None, ErrorDetail(
                code=INVALID_REQUEST_CODE,
                message="Requisicao malformada: JSON invalido.",
            )
    elif isinstance(body, Mapping):
        parsed = body
    else:
        return None, ErrorDetail(
            code=INVALID_REQUEST_CODE,
            message="Requisicao malformada: corpo invalido.",
        )

    if not isinstance(parsed, Mapping):
        return None, ErrorDetail(
            code=INVALID_REQUEST_CODE,
            message="Requisicao malformada: corpo deve ser um objeto JSON.",
        )
    message = parsed.get("message")
    if not isinstance(message, str):
        return None, ErrorDetail(
            code=INVALID_REQUEST_CODE,
            message="Requisicao malformada: campo 'message' de texto ausente.",
        )
    return message, None


#: Origem permitida no CORS. Com integração Lambda-proxy os cabeçalhos CORS têm
#: de vir na resposta da própria função (o API Gateway não os injeta). Configure
#: ``CORS_ALLOW_ORIGIN`` com o domínio institucional em produção; o default ``*``
#: serve para desenvolvimento/integração inicial.
def _cors_headers() -> dict[str, str]:
    """Cabeçalhos CORS aplicados a toda resposta da API (Req 5.4)."""
    return {
        "Access-Control-Allow-Origin": os.environ.get("CORS_ALLOW_ORIGIN", "*"),
        "Access-Control-Allow-Headers": "Content-Type,Authorization,x-access-token",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Access-Control-Max-Age": "3600",
    }


def _to_proxy_response(api_response: ApiResponse) -> dict[str, Any]:
    """Wrap an :class:`ApiResponse` as an API Gateway proxy integration result.

    The structured envelope (Req 5.4) is serialized as the JSON body, and an
    appropriate HTTP status code is derived from the outcome. Os cabeçalhos CORS
    acompanham toda resposta para permitir o consumo direto pelo site.
    """
    if api_response.error is not None:
        status_code = _HTTP_STATUS_BY_CODE.get(api_response.error.code, 400)
        body: dict[str, Any] = {
            "status": api_response.status,
            "error": {
                "code": api_response.error.code,
                "message": api_response.error.message,
            },
        }
    else:
        status_code = 200
        body = {"status": api_response.status, "content": api_response.content}
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **_cors_headers()},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _invoke_with_timeout(
    invoker: AgentInvoker,
    request: AgentRequest,
    timeout_s: float,
) -> AgentResponse:
    """Invoke the agent under a hard timeout (Req 5.5).

    Raises:
        FuturesTimeoutError: If the agent does not respond within ``timeout_s``.
        Exception: Any error raised by the invoker (agent unavailable).
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(invoker, request)
        return future.result(timeout=timeout_s)


# --- Lambda handler -------------------------------------------------------------

def handler(
    event: Mapping[str, Any],
    context: Any = None,
    *,
    verifier: TokenVerifier | None = None,
    invoker: AgentInvoker | None = None,
    now: datetime | None = None,
    agent_timeout_s: float = AGENT_TIMEOUT_S,
) -> dict[str, Any]:
    """Handle an API Gateway proxy request for the site integration.

    The flow is: authenticate via Cognito (Req 5.2, 10.3), validate the payload
    (Req 5.3), forward to the Agente_IA (Req 5.1) and return a structured
    envelope (Req 5.4); a timeout or agent unavailability yields a structured
    unavailability error (Req 5.5).

    Args:
        event: The API Gateway proxy event (headers + body).
        context: The Lambda context (unused; accepted for signature compatibility).
        verifier: Cognito token verifier. Defaults to a boto3-backed
            :class:`CognitoTokenVerifier` when omitted.
        invoker: Agent invoker. Must be supplied in production wiring; required
            (no boto3 default is fabricated here to avoid a partial default).
        now: Current instant used for token-expiry evaluation. Defaults to
            :func:`datetime.now` in UTC.
        agent_timeout_s: Hard timeout, in seconds, for the agent response
            (Req 5.5). Defaults to :data:`AGENT_TIMEOUT_S` (30 s).

    Returns:
        An API Gateway proxy response dict with ``statusCode``, ``headers`` and a
        JSON ``body`` carrying the structured envelope.
    """
    now = now or datetime.now(timezone.utc)

    # 1. Authentication (Req 5.2, 10.3, 10.5).
    if verifier is None:
        verifier = CognitoTokenVerifier.create()
    raw_token = _extract_bearer_token(event)
    token = verifier.verify(raw_token, now)
    auth = verify_token(token, now)
    if not auth.forwarded:
        # Sanitized auth error, never forwarded (Req 10.4, Property 15).
        return _to_proxy_response(build_response(error=auth.error or auth_error()))

    # 2. Payload validation (Req 5.3).
    message, parse_error = _parse_message(event)
    if parse_error is not None:
        return _to_proxy_response(build_response(error=parse_error))
    assert message is not None  # narrowed by parse_error is None
    validation = validate_message(message, Channel.SITE.value)
    if not validation.accepted:
        return _to_proxy_response(
            build_response(
                error=ErrorDetail(
                    code=INVALID_REQUEST_CODE,
                    message=validation.reason or "Mensagem invalida.",
                )
            )
        )

    # 3. Forward to the Agente_IA (Req 5.1) under a hard 30s timeout (Req 5.5).
    if invoker is None:
        raise RuntimeError(
            "An AgentInvoker must be supplied to forward requests to the Agente_IA."
        )
    session_id = _session_id_from_event(event)
    request = AgentRequest(
        session_id=session_id,
        channel=Channel.SITE.value,
        text=message,
    )
    try:
        agent_response = _invoke_with_timeout(invoker, request, agent_timeout_s)
    except FuturesTimeoutError:
        # 4. Timeout: structured unavailability error, input preserved (Req 5.5).
        result = signal_unavailable(
            Dependency.AGENT_RESPONSE, request, timed_out=True
        )
        return _to_proxy_response(
            build_response(
                error=ErrorDetail(
                    code=AGENT_UNAVAILABLE_CODE, message=result.user_message
                )
            )
        )
    except Exception:
        # 4. Agent unavailable: structured unavailability error (Req 5.5). The
        # underlying error is logged (server-side only) so operators can diagnose
        # it; the user-facing envelope stays sanitized (Req 10.4).
        logger.exception("Falha ao encaminhar requisição ao Agente_IA")
        result = signal_unavailable(
            Dependency.AGENT_RESPONSE, request, timed_out=False
        )
        return _to_proxy_response(
            build_response(
                error=ErrorDetail(
                    code=AGENT_UNAVAILABLE_CODE, message=result.user_message
                )
            )
        )

    # 5. Success: structured envelope carrying the agent's answer (Req 5.1, 5.4).
    return _to_proxy_response(build_response(content=agent_response.content))


def _session_id_from_event(event: Mapping[str, Any]) -> str:
    """Best-effort extraction of a session identifier from the event.

    Prefers an explicit ``sessionId`` in the parsed body, then a request context
    request id, falling back to an empty string. Session lifecycle itself is
    owned by the Gestor_de_Sessao adapter (Task 12.2); the API handler only
    threads through an identifier for the agent request.
    """
    body = event.get("body")
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except Exception:
            body = None
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
    elif isinstance(body, Mapping):
        parsed = body
    else:
        parsed = None
    if isinstance(parsed, Mapping):
        sid = parsed.get("sessionId")
        if isinstance(sid, str) and sid:
            return sid
    ctx = event.get("requestContext")
    if isinstance(ctx, Mapping):
        rid = ctx.get("requestId")
        if isinstance(rid, str) and rid:
            return rid
    return ""
