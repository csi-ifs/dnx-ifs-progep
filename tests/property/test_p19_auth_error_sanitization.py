"""Property-based test for authentication error message sanitization.

Feature: assistente-progep-ifs, Property 19: Sanitização de mensagens de erro de
autenticação.

For any contexto de falha de autenticação contendo dados institucionais, dados
de usuário ou detalhes internos, a mensagem de erro retornada ao solicitante não
contém nenhum desses dados sensíveis nem detalhes internos da falha.

Validates: Requirements 10.4
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.domain.api import (
    AUTH_ERROR_CODE,
    GENERIC_AUTH_ERROR_MESSAGE,
    auth_error,
    sanitize_auth_error,
)

# Distinctive, unmistakable sensitive markers injected into the failure context.
# None of these may ever appear in the user-facing authentication error message.
# They mimic institutional data, user PII, credentials/tokens and internal
# failure details (stack traces, exception text, JWKS/issuer internals).
SENSITIVE_MARKERS: tuple[str, ...] = (
    "SENSITIVE_CPF_12345678900",
    "SENSITIVE_SIAPE_9998887",
    "SENSITIVE_EMAIL_user@progep.ifs.edu.br",
    "SENSITIVE_TOKEN_eyJhbGciOiJIUzI1NiJ9.SECRET.SIGNATURE",
    "SENSITIVE_JWKS_KID_abc123",
    "SENSITIVE_ISSUER_https://cognito-idp.us-east-1.amazonaws.com/pool",
    "SENSITIVE_STACKTRACE_File_api.py_line_42",
    "SENSITIVE_INTERNAL_signature_verification_failed",
    "SENSITIVE_INSTITUTION_IFS_PROGEP_INTERNAL",
    "SENSITIVE_DB_HOST_10.0.0.5",
)


def _marker_string() -> st.SearchStrategy[str]:
    """Strings guaranteed to embed at least one distinctive sensitive marker."""
    return st.builds(
        lambda prefix, marker, suffix: f"{prefix}{marker}{suffix}",
        st.text(max_size=20),
        st.sampled_from(SENSITIVE_MARKERS),
        st.text(max_size=20),
    )


class _SensitiveException(Exception):
    """Exception whose message and attributes carry sensitive markers."""


def _sensitive_exception() -> st.SearchStrategy[BaseException]:
    """Build exceptions whose string form contains sensitive markers."""
    return st.builds(_SensitiveException, _marker_string())


def _sensitive_dict() -> st.SearchStrategy[dict]:
    """Dicts of fake user data / tokens / stack traces with sensitive markers."""
    return st.dictionaries(
        keys=st.sampled_from(
            ["cpf", "siape", "email", "token", "issuer", "stacktrace", "host"]
        ),
        values=_marker_string(),
        min_size=1,
        max_size=7,
    )


def _sensitive_context() -> st.SearchStrategy[object]:
    """Arbitrary failure context that always embeds sensitive markers.

    Covers strings, dicts of fake user data/tokens/stack traces, exceptions,
    lists and nested combinations thereof.
    """
    leaves = st.one_of(
        _marker_string(),
        _sensitive_dict(),
        _sensitive_exception(),
    )
    return st.recursive(
        leaves,
        lambda children: st.one_of(
            st.lists(children, min_size=1, max_size=4),
            st.tuples(children, children),
            st.dictionaries(
                keys=st.text(max_size=8), values=children, min_size=1, max_size=4
            ),
        ),
        max_leaves=6,
    )


def _no_marker_leaks(message: str) -> None:
    """Assert that no sensitive marker (verbatim or per-fragment) leaked."""
    for marker in SENSITIVE_MARKERS:
        assert marker not in message
        # Also guard against partial leakage of the distinctive fragments.
        for fragment in marker.split("_"):
            if len(fragment) >= 5:  # ignore short, generic tokens
                assert fragment not in message


@given(context=_sensitive_context())
def test_p19_sanitized_message_hides_sensitive_context(context: object) -> None:
    """The message is the fixed generic text and leaks no injected marker.

    Feature: assistente-progep-ifs, Property 19: Sanitização de mensagens de erro
    de autenticação. Validates: Requirements 10.4.
    """
    message = sanitize_auth_error(context)

    # The message is always exactly the fixed, generic, sanitized message.
    assert message == GENERIC_AUTH_ERROR_MESSAGE
    # None of the injected sensitive markers appear in the returned message.
    _no_marker_leaks(message)


@given(context=_sensitive_context())
def test_p19_auth_error_detail_hides_sensitive_context(context: object) -> None:
    """auth_error() yields a sanitized ErrorDetail leaking no injected marker.

    Feature: assistente-progep-ifs, Property 19: Sanitização de mensagens de erro
    de autenticação. Validates: Requirements 10.4.
    """
    detail = auth_error(context)

    assert detail.code == AUTH_ERROR_CODE
    assert detail.message == GENERIC_AUTH_ERROR_MESSAGE
    _no_marker_leaks(detail.message)
    # The stable error code must not carry sensitive context either.
    _no_marker_leaks(detail.code)
