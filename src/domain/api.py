"""Pure-logic API envelope and Cognito authentication for the API_de_Integracao.

This module implements Task 8.1 of the Assistente PROGEP-IFS spec:

* ``build_response`` — assembles the structured response envelope returned by the
  API_de_Integracao. Every envelope carries a ``status`` indicator and **exactly
  one** of ``content`` (on success) or ``error`` with ``code``/``message`` (on
  failure) (Req 5.1, 5.4, Property 16).
* ``verify_token`` — classifies a Cognito authentication token and decides
  whether the request may be forwarded to the Agente_IA. The request is
  forwarded **if and only if** the token is present, valid and not expired; an
  absent, invalid or expired token blocks the request with an authentication
  error and requires re-authentication (Req 5.2, 10.3, 10.5, Property 15).
* ``sanitize_auth_error`` — produces the user-facing authentication error
  message. It never exposes institutional data, user data or internal failure
  details (Req 10.4, Property 19).

All functions are pure and side-effect free. The actual Cognito signature
verification (a network/JWKS operation) lives in an I/O adapter; this module
operates on the already-decoded token facts so it can be exercised by
property-based tests without external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from src.domain.config import ApiStatus
from src.domain.models import ApiResponse, ErrorDetail

# --- Authentication error contract (Req 10.4) -----------------------------------

#: Error code reported when a request is not authenticated (Req 5.2, 10.3).
AUTH_ERROR_CODE: str = "unauthenticated"

#: Generic, sanitized authentication error message. It deliberately contains no
#: institutional data, user data or internal failure details (Req 10.4) and asks
#: the user to authenticate again (Req 10.5).
GENERIC_AUTH_ERROR_MESSAGE: str = (
    "Falha na autenticacao. Autentique-se novamente para continuar."
)


class TokenState(str, Enum):
    """Classification of the authentication token supplied with a request.

    Only :attr:`VALID` results in the request being forwarded to the Agente_IA;
    every other state blocks the request (Req 5.2, 10.5, Property 15).
    """

    ABSENT = "absent"      # no token supplied
    INVALID = "invalid"    # token present but signature/claims are not valid
    EXPIRED = "expired"    # token present and valid but past its expiry
    VALID = "valid"        # token present, valid and not expired


@dataclass(frozen=True)
class CognitoToken:
    """Already-decoded facts about a Cognito token, as seen by the pure logic.

    The Cognito signature/JWKS verification is performed by an I/O adapter, which
    fills in these fields. This keeps :func:`verify_token` free of I/O and fully
    testable.

    Attributes:
        present: ``True`` when a token was supplied with the request.
        signature_valid: ``True`` when the adapter confirmed the token signature
            and issuer/audience claims are valid.
        expires_at: The token expiry instant, or ``None`` when unknown/absent.
    """

    present: bool
    signature_valid: bool
    expires_at: datetime | None = None


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticating a request (Req 5.2, 10.3, 10.5).

    Attributes:
        forwarded: ``True`` if and only if the token is valid and not expired, in
            which case the request may be forwarded to the Agente_IA.
        state: The :class:`TokenState` value that produced this outcome.
        requires_reauth: ``True`` when the request is blocked and the user must
            authenticate again via Amazon Cognito before retrying (Req 10.5).
        error: The sanitized :class:`ErrorDetail` to return when blocked, or
            ``None`` when the request is forwarded.
    """

    forwarded: bool
    state: str
    requires_reauth: bool
    error: ErrorDetail | None


# --- Structured response envelope (Req 5.1, 5.4, Property 16) -------------------

def build_response(
    *,
    content: str | None = None,
    error: ErrorDetail | None = None,
) -> ApiResponse:
    """Assemble the structured API response envelope (Req 5.1, 5.4, Property 16).

    The envelope always carries a ``status`` indicator and **exactly one** of
    ``content`` (success) or ``error`` (failure). Callers must supply exactly one
    of ``content`` or ``error``; supplying both or neither is a programming error.

    Note that an empty string is a valid success ``content`` — only ``None`` is
    treated as "absent", so the success/error distinction is unambiguous.

    Args:
        content: The response content on success. May be an empty string.
        error: The structured error detail on failure.

    Returns:
        An :class:`ApiResponse` with ``status == "ok"`` and ``content`` set on
        success, or ``status == "error"`` and ``error`` set on failure.

    Raises:
        ValueError: If both or neither of ``content`` and ``error`` are provided.
    """
    if (content is None) == (error is None):
        raise ValueError(
            "build_response requires exactly one of 'content' or 'error'"
        )
    if error is not None:
        return ApiResponse(status=ApiStatus.ERROR.value, content=None, error=error)
    return ApiResponse(status=ApiStatus.OK.value, content=content, error=None)


def success_response(content: str) -> ApiResponse:
    """Return a success envelope carrying ``content`` (Req 5.1, 5.4)."""
    return build_response(content=content)


def error_response(code: str, message: str) -> ApiResponse:
    """Return an error envelope carrying an :class:`ErrorDetail` (Req 5.4)."""
    return build_response(error=ErrorDetail(code=code, message=message))


# --- Authentication error sanitization (Req 10.4, Property 19) ------------------

def sanitize_auth_error(context: object = None) -> str:
    """Return a sanitized, user-facing authentication error message (Req 10.4).

    The returned message never contains institutional data, user data or internal
    failure details. This is achieved by construction: the function always returns
    a fixed generic message and **deliberately ignores** ``context``, so no data
    from the failure can ever leak into the response (Property 19).

    Args:
        context: Any failure context (exception, claims, diagnostic string, ...).
            It is intentionally discarded and only accepted so callers can pass
            their raw context without risking leakage.

    Returns:
        The constant :data:`GENERIC_AUTH_ERROR_MESSAGE`.
    """
    return GENERIC_AUTH_ERROR_MESSAGE


def auth_error(context: object = None) -> ErrorDetail:
    """Return a sanitized authentication :class:`ErrorDetail` (Req 5.2, 10.4)."""
    return ErrorDetail(code=AUTH_ERROR_CODE, message=sanitize_auth_error(context))


# --- Cognito token verification (Req 5.2, 10.3, 10.5, Property 15) --------------

def classify_token(token: CognitoToken | None, now: datetime) -> str:
    """Classify ``token`` at instant ``now`` into a :class:`TokenState` value.

    The token is :attr:`~TokenState.VALID` if and only if it is present, its
    signature is valid and its expiry is strictly in the future. An absent token
    yields :attr:`~TokenState.ABSENT`, a present token with an invalid signature
    yields :attr:`~TokenState.INVALID`, and a present, valid token whose expiry
    is missing or not in the future yields :attr:`~TokenState.EXPIRED`.
    """
    if token is None or not token.present:
        return TokenState.ABSENT.value
    if not token.signature_valid:
        return TokenState.INVALID.value
    if token.expires_at is None or token.expires_at <= now:
        return TokenState.EXPIRED.value
    return TokenState.VALID.value


def verify_token(token: CognitoToken | None, now: datetime) -> AuthResult:
    """Verify a Cognito token and decide whether to forward the request.

    The request is forwarded to the Agente_IA **if and only if** the token is
    present, valid and not expired (Req 5.2, 10.3). Any other state — absent,
    invalid or expired — blocks the request with a sanitized authentication error
    and requires the user to authenticate again via Amazon Cognito before
    retrying (Req 10.4, 10.5, Property 15). The request is never forwarded when
    blocked.

    Args:
        token: The already-decoded token facts, or ``None`` when no token was
            supplied.
        now: The current instant used to evaluate expiry.

    Returns:
        An :class:`AuthResult`. When ``state == "valid"`` it has
        ``forwarded=True`` and no error; otherwise ``forwarded=False``,
        ``requires_reauth=True`` and a sanitized ``error``.
    """
    state = classify_token(token, now)
    if state == TokenState.VALID.value:
        return AuthResult(
            forwarded=True,
            state=state,
            requires_reauth=False,
            error=None,
        )
    return AuthResult(
        forwarded=False,
        state=state,
        requires_reauth=True,
        error=auth_error(),
    )
