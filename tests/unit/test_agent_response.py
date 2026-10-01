"""Unit tests for response assembly (Task 6.1).

Verify that :func:`assemble_response` produces an ``answered`` response with
exactly one :class:`SourceRef` per source document actually used (Req 2.2, 2.3).
"""

from __future__ import annotations

import pytest

from src.domain.agent_response import assemble_response
from src.domain.config import AgentStatus
from src.domain.models import SourceRef


pytestmark = pytest.mark.unit


def test_one_source_ref_per_used_document():
    used = (SourceRef("d1", 0.9), SourceRef("d2", 0.7))
    resp = assemble_response("resposta", used)

    assert resp.status == AgentStatus.ANSWERED.value
    assert resp.content == "resposta"
    assert [s.doc_id for s in resp.sources] == ["d1", "d2"]


def test_duplicate_documents_collapse_to_single_ref_with_max_score():
    # Two passages from the same document must yield exactly one SourceRef.
    used = (SourceRef("d1", 0.6), SourceRef("d2", 0.5), SourceRef("d1", 0.95))
    resp = assemble_response("resposta", used)

    doc_ids = [s.doc_id for s in resp.sources]
    assert doc_ids == ["d1", "d2"]  # first-seen order preserved
    d1 = next(s for s in resp.sources if s.doc_id == "d1")
    assert d1.score == 0.95  # highest score kept for the document


def test_confidence_defaults_to_max_source_score():
    used = (SourceRef("d1", 0.42), SourceRef("d2", 0.81))
    resp = assemble_response("resposta", used)
    assert resp.confidence == 0.81


def test_explicit_confidence_is_respected():
    used = (SourceRef("d1", 0.42),)
    resp = assemble_response("resposta", used, confidence=0.99)
    assert resp.confidence == 0.99


def test_no_sources_yields_empty_refs_and_zero_confidence():
    resp = assemble_response("resposta", ())
    assert resp.sources == ()
    assert resp.confidence == 0.0
