"""Pure-logic response assembly for the Agente_IA (Agentic RAG).

Implements the assembly of an :class:`AgentResponse` from the source documents
that were actually used to ground the answer. The response content is derived
exclusively from the retrieved documents (Req 2.2), and the response includes
exactly one :class:`SourceRef` per source document effectively used (Req 2.3).

This module is intentionally free of any I/O: it operates only on already
retrieved passages and the generated content, so it can be exercised by
example-based and property-based tests without external dependencies.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.domain.config import AgentStatus
from src.domain.models import AgentResponse, SourceRef


def _dedupe_sources(used_sources: Iterable[SourceRef]) -> tuple[SourceRef, ...]:
    """Return exactly one :class:`SourceRef` per source document.

    Retrieval may return several passages from the same document (same
    ``doc_id``). Req 2.3 requires the response to reference *each source
    document* used, so the passages are collapsed to a single reference per
    ``doc_id``. When a document contributes multiple passages, the highest
    similarity score is kept (the most relevant passage represents the
    document). First-seen order is preserved for deterministic, stable output.
    """
    best_score: dict[str, float] = {}
    order: list[str] = []
    for ref in used_sources:
        if ref.doc_id not in best_score:
            best_score[ref.doc_id] = ref.score
            order.append(ref.doc_id)
        elif ref.score > best_score[ref.doc_id]:
            best_score[ref.doc_id] = ref.score
    return tuple(SourceRef(doc_id=doc_id, score=best_score[doc_id]) for doc_id in order)


def assemble_response(
    content: str,
    used_sources: Iterable[SourceRef],
    *,
    confidence: float | None = None,
) -> AgentResponse:
    """Assemble an :class:`AgentResponse` grounded in the used source documents.

    The response includes exactly one :class:`SourceRef` per distinct source
    document that was actually used to build the answer (Req 2.3). The
    ``content`` is expected to be derived exclusively from those documents
    (Req 2.2); this function does not synthesize content, it only packages the
    provided content together with the deduplicated source references.

    Args:
        content: The generated answer text, derived only from the used
            documents (Req 2.2).
        used_sources: The source passages actually used to ground the answer.
            Multiple passages from the same ``doc_id`` are collapsed to a single
            reference (Req 2.3).
        confidence: Optional confidence score for the response. When omitted, it
            defaults to the highest similarity score among the used sources, or
            ``0.0`` when there are no sources.

    Returns:
        An :class:`AgentResponse` with ``status == AgentStatus.ANSWERED`` and
        one :class:`SourceRef` per distinct source document used.
    """
    sources = _dedupe_sources(used_sources)
    if confidence is None:
        confidence = max((ref.score for ref in sources), default=0.0)
    return AgentResponse(
        status=AgentStatus.ANSWERED.value,
        content=content,
        sources=sources,
        confidence=confidence,
    )
