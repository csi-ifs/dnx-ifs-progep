"""Property-based test for the structured API response envelope.

Feature: assistente-progep-ifs, Property 16: Envelope de resposta estruturado

For any resultado de processamento (sucesso ou erro), a resposta da API contém
sempre um indicador de status e exatamente um entre ``content`` (sucesso) ou
``error`` com ``code`` e ``message`` (erro).

Validates: Requirements 5.1, 5.4
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.api import build_response
from src.domain.config import ApiStatus
from src.domain.models import ErrorDetail

pytestmark = pytest.mark.property


# Include the empty string: an empty-string content is a valid success payload;
# only ``None`` counts as "absent" for the exactly-one contract.
_content_strategy = st.text(min_size=0, max_size=200)

_error_strategy = st.builds(
    ErrorDetail,
    code=st.text(min_size=1, max_size=50),
    message=st.text(min_size=1, max_size=200),
)


@settings(max_examples=200)
@given(outcome=st.one_of(_content_strategy, _error_strategy))
def test_envelope_has_status_and_exactly_one_payload(
    outcome: str | ErrorDetail,
) -> None:
    """Every envelope carries a status and exactly one of content/error.

    Feature: assistente-progep-ifs, Property 16: Envelope de resposta estruturado
    Validates: Requirements 5.1, 5.4

    A single outcome (either a success ``content`` string or an ``ErrorDetail``)
    is generated and dispatched to build_response accordingly. The resulting
    envelope must always set ``status`` and populate exactly one of ``content``
    or ``error`` consistently with that status.
    """
    if isinstance(outcome, ErrorDetail):
        response = build_response(error=outcome)
    else:
        response = build_response(content=outcome)

    # A status indicator is always present and is one of the two valid values.
    assert response.status in (ApiStatus.OK.value, ApiStatus.ERROR.value)

    # Exactly one of content / error is populated (only None counts as absent).
    has_content = response.content is not None
    has_error = response.error is not None
    assert has_content != has_error

    if isinstance(outcome, ErrorDetail):
        # Error outcome: status error, structured error with code + message.
        assert response.status == ApiStatus.ERROR.value
        assert response.content is None
        assert response.error is not None
        assert response.error == outcome
        assert response.error.code == outcome.code
        assert response.error.message == outcome.message
    else:
        # Success outcome: status ok, content set (empty string allowed).
        assert response.status == ApiStatus.OK.value
        assert response.error is None
        assert response.content == outcome


@given(
    content=st.text(min_size=0, max_size=200),
    error=st.builds(
        ErrorDetail,
        code=st.text(min_size=1, max_size=50),
        message=st.text(min_size=1, max_size=200),
    ),
)
def test_build_response_rejects_both_content_and_error(
    content: str,
    error: ErrorDetail,
) -> None:
    """Supplying both content and error is a programming error.

    Feature: assistente-progep-ifs, Property 16: Envelope de resposta estruturado
    Validates: Requirements 5.1, 5.4
    """
    with pytest.raises(ValueError):
        build_response(content=content, error=error)


def test_build_response_rejects_neither_content_nor_error() -> None:
    """Supplying neither content nor error is a programming error.

    Feature: assistente-progep-ifs, Property 16: Envelope de resposta estruturado
    Validates: Requirements 5.1, 5.4
    """
    with pytest.raises(ValueError):
        build_response()
