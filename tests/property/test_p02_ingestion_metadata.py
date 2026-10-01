"""Property-based test for ingestion metadata assembly.

Feature: assistente-progep-ifs, Property 2: Registro de metadados de ingestão

For any documento aceito, o ``IngestionRecord`` produzido contém a ``source`` de
origem, um ``doc_id`` não vazio e um ``indexed_at`` definido com precisão de
segundos (microssegundos zerados).

Validates: Requirements 1.2
"""

from __future__ import annotations

from datetime import datetime

from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import IngestionStatus
from src.domain.ingestion import build_ingestion_record
from src.domain.models import DocumentCandidate

# Non-blank text: has at least one non-whitespace character so that ``.strip()``
# yields a non-empty value (a precondition for accepted documents).
non_blank_text = st.text(min_size=1, max_size=64).filter(lambda s: s.strip() != "")

# Arbitrary datetimes including microseconds, so we exercise the truncation to
# whole-second precision required by Req 1.2.
any_datetime = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
)


@st.composite
def accepted_documents(draw: st.DrawFn) -> DocumentCandidate:
    """Generate arbitrary *accepted* documents with non-empty doc_id/source."""
    return DocumentCandidate(
        doc_id=draw(non_blank_text),
        source=draw(non_blank_text),
        filename=draw(st.text(max_size=64)),
        content_type=draw(st.sampled_from(["pdf", "docx", "txt", "html"])),
        size_bytes=draw(st.integers(min_value=0, max_value=52_428_800)),
    )


@given(doc=accepted_documents(), indexed_at=any_datetime)
def test_ingestion_record_preserves_metadata_with_second_precision(
    doc: DocumentCandidate, indexed_at: datetime
) -> None:
    """The record keeps the origin source, a non-empty doc_id and second-precision time."""
    record = build_ingestion_record(doc, indexed_at, status=IngestionStatus.INDEXED)

    # Preserves the origin source.
    assert record.source == doc.source.strip()

    # doc_id is present and non-empty.
    assert record.doc_id
    assert record.doc_id.strip() != ""
    assert record.doc_id == doc.doc_id.strip()

    # indexed_at is defined with second precision (microseconds dropped).
    assert record.indexed_at is not None
    assert record.indexed_at.microsecond == 0
    assert record.indexed_at == indexed_at.replace(microsecond=0)
