"""Unit tests for grounding the agent answer in retrieved documents (Task 6.7).

Curated example-based tests asserting that the assembled response content is
derived *exclusively* from the retrieved documents whose similarity score meets
the relevance threshold.

**Validates: Requirements 2.2** — the agent generates a response whose content
is derived exclusively from the retrieved documents (score >= relevance
threshold).

The Knowledge Base client is a lightweight injected fake, so no AWS access is
required.
"""

from __future__ import annotations

import pytest

from src.adapters.agent import BedrockAgentAdapter
from src.domain.config import AgentStatus
from src.domain.models import AgentRequest


pytestmark = pytest.mark.unit


class FakeKbClient:
    """A stand-in for the boto3 ``bedrock-agent-runtime`` client."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _kb_response(*scored_uris):
    """Build a Bedrock ``retrieve`` response from ``(uri, score)`` pairs."""
    return {
        "retrievalResults": [
            {
                "content": {"text": f"trecho de {uri}"},
                "location": {"s3Location": {"uri": uri}},
                "score": score,
            }
            for uri, score in scored_uris
        ]
    }


def _request(text="Como solicito ferias?"):
    return AgentRequest(session_id="s-1", channel="site", text=text)


def _adapter(response):
    return BedrockAgentAdapter("kb-123", kb_client=FakeKbClient(response))


# --- Curated grounding examples (Req 2.2) --------------------------------------

def test_content_references_only_documents_meeting_threshold():
    """Only docs with score >= threshold ground the answer; the rest are absent
    from both the sources and the generated content (Req 2.2)."""
    adapter = _adapter(
        _kb_response(
            ("s3://kb/relevante-a.pdf", 0.92),
            ("s3://kb/relevante-b.pdf", 0.71),
            ("s3://kb/abaixo-limiar.pdf", 0.30),
        )
    )

    resp = adapter.forward_request(_request(), threshold=0.6)

    assert resp.status == AgentStatus.ANSWERED.value
    used_ids = {s.doc_id for s in resp.sources}
    assert used_ids == {"s3://kb/relevante-a.pdf", "s3://kb/relevante-b.pdf"}
    # The below-threshold document must not ground the answer at all.
    assert "s3://kb/abaixo-limiar.pdf" not in used_ids
    assert "s3://kb/abaixo-limiar.pdf" not in resp.content


def test_answered_response_sources_are_subset_of_retrieved_docs():
    """Every source in an answered response was actually retrieved (Req 2.2)."""
    retrieved = (
        ("s3://kb/doc-1.pdf", 0.88),
        ("s3://kb/doc-2.pdf", 0.55),
        ("s3://kb/doc-3.pdf", 0.10),
    )
    adapter = _adapter(_kb_response(*retrieved))

    resp = adapter.forward_request(_request(), threshold=0.5)

    retrieved_ids = {uri for uri, _ in retrieved}
    response_ids = {s.doc_id for s in resp.sources}
    assert response_ids  # answered responses cite at least one document
    assert response_ids.issubset(retrieved_ids)


def test_grounding_content_mentions_each_used_document_id():
    """The grounded content names every used document id (Req 2.2)."""
    adapter = _adapter(
        _kb_response(
            ("s3://kb/manual-ferias.pdf", 0.90),
            ("s3://kb/portaria-2024.pdf", 0.80),
        )
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    for source in resp.sources:
        assert source.doc_id in resp.content


def test_threshold_boundary_document_is_included_in_grounding():
    """A document scoring exactly at the threshold grounds the answer (Req 2.2)."""
    adapter = _adapter(
        _kb_response(
            ("s3://kb/no-limiar.pdf", 0.60),
            ("s3://kb/abaixo.pdf", 0.59),
        )
    )

    resp = adapter.forward_request(_request(), threshold=0.60)

    used_ids = {s.doc_id for s in resp.sources}
    assert used_ids == {"s3://kb/no-limiar.pdf"}
    assert "s3://kb/abaixo.pdf" not in resp.content


def test_no_content_derived_when_no_document_meets_threshold():
    """With no relevant document there is no grounded content or sources (Req 2.2)."""
    adapter = _adapter(_kb_response(("s3://kb/irrelevante.pdf", 0.20)))

    resp = adapter.forward_request(_request(), threshold=0.7)

    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.sources == ()
    assert "s3://kb/irrelevante.pdf" not in resp.content
