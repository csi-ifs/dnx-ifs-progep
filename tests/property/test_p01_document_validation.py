"""Property-based test for document validation.

Feature: assistente-progep-ifs, Property 1: Validação de documento por formato e
tamanho.

For any documento candidato com ``content_type`` e ``size_bytes`` arbitrários, a
validação aceita o documento **se e somente se** o formato pertence a
{pdf, docx, txt, html} **e** ``size_bytes`` <= 52.428.800 (50 MB); toda rejeição
produz um ``reason`` não vazio (e nenhum embedding é gravado — a camada pura
nunca grava embeddings, o chamador só persiste quando ``accepted`` é True).

Validates: Requirements 1.3
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import MAX_DOC_SIZE_BYTES, SUPPORTED_CONTENT_TYPES
from src.domain.ingestion import validate_document
from src.domain.models import DocumentCandidate

pytestmark = pytest.mark.property


# --- Generators -----------------------------------------------------------------

# Valid content types exactly as accepted by the validator.
_valid_content_types = st.sampled_from(sorted(SUPPORTED_CONTENT_TYPES))

# Arbitrary (mostly invalid) content types: free text, empty, and near-misses
# such as uppercase/whitespace variants and unsupported formats.
_arbitrary_content_types = st.one_of(
    _valid_content_types,
    st.sampled_from(["", "PDF", " pdf ", "pptx", "jpg", "mp3", "xml", "csv", "doc"]),
    st.text(max_size=12),
)


def _sizes() -> st.SearchStrategy[int]:
    """Sizes covering negatives, zero, the boundary and above the boundary."""
    around_boundary = st.integers(
        min_value=MAX_DOC_SIZE_BYTES - 5,
        max_value=MAX_DOC_SIZE_BYTES + 5,
    )
    broad = st.integers(min_value=-1_000, max_value=MAX_DOC_SIZE_BYTES * 2)
    explicit = st.sampled_from(
        [
            -1,
            0,
            1,
            MAX_DOC_SIZE_BYTES - 1,
            MAX_DOC_SIZE_BYTES,  # boundary: accepted
            MAX_DOC_SIZE_BYTES + 1,  # just above: rejected
            MAX_DOC_SIZE_BYTES * 10,
        ]
    )
    return st.one_of(around_boundary, broad, explicit)


_documents = st.builds(
    DocumentCandidate,
    doc_id=st.text(min_size=1, max_size=8),
    source=st.sampled_from(["site", "cliente"]),
    filename=st.text(max_size=16),
    content_type=_arbitrary_content_types,
    size_bytes=_sizes(),
)


# --- Property -------------------------------------------------------------------

@given(doc=_documents)
def test_property_1_document_validation_iff(doc: DocumentCandidate) -> None:
    """Acceptance holds iff supported format AND 0 <= size <= 50 MB.

    Feature: assistente-progep-ifs, Property 1: Validação de documento por
    formato e tamanho. Validates: Requirements 1.3.
    """
    result = validate_document(doc)

    # Reference definition of the acceptance condition, computed independently
    # from the implementation. The validator normalises the content type by
    # trimming and lowercasing before membership check.
    normalized_type = (doc.content_type or "").strip().lower()
    format_ok = normalized_type in SUPPORTED_CONTENT_TYPES
    size_ok = 0 <= doc.size_bytes <= MAX_DOC_SIZE_BYTES
    expected_accepted = format_ok and size_ok

    # If and only if: acceptance matches the reference condition exactly.
    assert result.accepted is expected_accepted

    # Every rejection produces a non-empty reason (Req 1.3).
    if not result.accepted:
        assert result.reason is not None
        assert result.reason.strip() != ""
    else:
        # Accepted documents carry no rejection reason.
        assert result.reason is None
