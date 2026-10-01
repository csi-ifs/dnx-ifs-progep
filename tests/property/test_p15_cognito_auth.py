"""Property-based test for mandatory Cognito authentication.

Feature: assistente-progep-ifs, Property 15: Autenticação obrigatória via Cognito

For any authentication token state (absent, invalid, expired or valid), the
request is forwarded to the Agente_IA if and only if the token is present, its
signature is valid and its expiry is strictly in the future. Any other state
blocks the request with a sanitized authentication error, without forwarding,
requiring re-authentication.

Validates: Requirements 5.2, 10.3, 10.5
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from src.domain.api import (
    AUTH_ERROR_CODE,
    CognitoToken,
    verify_token,
)

# Reference instant against which token expiry is evaluated.
NOW = datetime(2024, 1, 1, 12, 0, 0)


def _expires_at_strategy() -> st.SearchStrategy[datetime | None]:
    """Generate token expiry instants before/after ``NOW``, plus ``None``.

    Covers all expiry-relevant cases: missing expiry, expiry in the past, expiry
    exactly at ``NOW`` (boundary, treated as expired) and expiry in the future.
    """
    offsets = st.integers(min_value=-100_000, max_value=100_000)
    return st.one_of(
        st.none(),
        offsets.map(lambda s: NOW + timedelta(seconds=s)),
        st.just(NOW),  # boundary: expiry exactly at NOW is expired (<= now)
    )


def _token_strategy() -> st.SearchStrategy[CognitoToken | None]:
    """Generate arbitrary ``CognitoToken`` values plus ``None`` (no token)."""
    tokens = st.builds(
        CognitoToken,
        present=st.booleans(),
        signature_valid=st.booleans(),
        expires_at=_expires_at_strategy(),
    )
    return st.one_of(st.none(), tokens)


@given(token=_token_strategy())
def test_p15_forward_iff_valid_and_not_expired(token: CognitoToken | None) -> None:
    """Forwarded iff present AND signature valid AND expiry strictly in future.

    Feature: assistente-progep-ifs, Property 15: Autenticação obrigatória via Cognito
    Validates: Requirements 5.2, 10.3, 10.5
    """
    result = verify_token(token, NOW)

    should_forward = (
        token is not None
        and token.present
        and token.signature_valid
        and token.expires_at is not None
        and token.expires_at > NOW
    )

    # forwarded is True if and only if the token is valid and not expired.
    assert result.forwarded is should_forward

    if should_forward:
        # A valid, non-expired token is forwarded with no auth error and no
        # re-authentication requirement.
        assert result.requires_reauth is False
        assert result.error is None
    else:
        # Any other state blocks the request: no forwarding, re-auth required,
        # and a sanitized authentication error carrying AUTH_ERROR_CODE.
        assert result.requires_reauth is True
        assert result.error is not None
        assert result.error.code == AUTH_ERROR_CODE
        assert result.error.message
