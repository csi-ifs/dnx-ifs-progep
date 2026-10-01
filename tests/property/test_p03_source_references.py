"""Property-based test for source-document references in assembled responses.

Feature: assistente-progep-ifs, Property 3: Referências de documentos-fonte na
resposta.

**Validates: Requirements 2.3**

Property 3 text: For any conjunto de documentos recuperados e efetivamente
usados para gerar uma resposta, a resposta montada contém exatamente uma
referência de identificação para cada documento-fonte utilizado.

The implementation collapses multiple passages of the same ``doc_id`` to a
single :class:`SourceRef`, keeping the maximum score for that document and
preserving first-seen order.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.agent_response import assemble_response
from src.domain.models import SourceRef


pytestmark = pytest.mark.property


# doc_ids are drawn from a small alphabet so that repeated doc_ids (multiple
# passages of the same document) occur frequently, exercising the dedupe logic.
_doc_ids = st.sampled_from(["d1", "d2", "d3", "d4", "d5"])
_scores = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_source_refs = st.lists(
    st.builds(SourceRef, doc_id=_doc_ids, score=_scores),
    min_size=0,
    max_size=20,
)


@given(content=st.text(), used=_source_refs)
def test_exactly_one_ref_per_distinct_used_document(content: str, used: list[SourceRef]):
    resp = assemble_response(content, used)

    result_doc_ids = [s.doc_id for s in resp.sources]

    # Expected: first-seen order of distinct doc_ids among the used sources.
    expected_order: list[str] = []
    seen: set[str] = set()
    for ref in used:
        if ref.doc_id not in seen:
            seen.add(ref.doc_id)
            expected_order.append(ref.doc_id)

    # Exactly one SourceRef per distinct used document, in first-seen order.
    assert result_doc_ids == expected_order
    # No duplicates in the resulting references.
    assert len(result_doc_ids) == len(set(result_doc_ids))

    # The kept score for each document is the maximum score among its passages.
    for ref in resp.sources:
        expected_max = max(s.score for s in used if s.doc_id == ref.doc_id)
        assert ref.score == expected_max
