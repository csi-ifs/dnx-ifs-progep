"""Relevance decision logic for the Agente_IA (pure-logic layer).

Implements the decision taken when the Knowledge Base retrieval returns no
document with a similarity score at or above the configured relevance
threshold. Per Requirement 2.4, in that case the agent must inform the user
that the information was *not found* and must trigger the Roteador_de_Atendimento
(escalation), while preserving the original request unchanged.

This module is intentionally free of any I/O so it can be exercised by
property-based tests (Property 4). Actual escalation to Amazon Connect and the
confidence-based routing decision live in separate modules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.domain.config import AgentStatus
from src.domain.models import AgentRequest


@dataclass(frozen=True)
class RelevanceDecision:
    """Outcome of the relevance check for a retrieval result (Req 2.4).

    Attributes:
        relevant: ``True`` when at least one retrieved document meets the
            relevance threshold, ``False`` otherwise.
        status: The resulting agent status. When ``relevant`` is ``False`` this
            is :attr:`AgentStatus.NOT_FOUND`; when ``relevant`` is ``True`` it is
            ``None`` (the answered/routing status is decided downstream).
        escalate: ``True`` when the Roteador_de_Atendimento must be triggered.
            This mirrors ``not relevant`` for Req 2.4.
        original_request: The original request, preserved unchanged so it can be
            re-processed or handed off during escalation without loss.
    """

    relevant: bool
    status: AgentStatus | None
    escalate: bool
    original_request: AgentRequest


def has_relevant_document(scores: Sequence[float], threshold: float) -> bool:
    """Return ``True`` iff at least one score is >= the relevance threshold.

    An empty ``scores`` sequence yields ``False`` (no relevant document).
    """
    return any(score >= threshold for score in scores)


def decide_relevance(
    scores: Sequence[float],
    threshold: float,
    original_request: AgentRequest,
) -> RelevanceDecision:
    """Decide the relevance outcome for a Knowledge Base retrieval (Req 2.4).

    If no retrieved document has a similarity score greater than or equal to the
    configured relevance ``threshold``, the decision is ``not_found`` and the
    Roteador_de_Atendimento is triggered (``escalate=True``); the
    ``original_request`` is preserved unchanged. Otherwise the decision is marked
    relevant and no escalation is triggered on this basis.

    Args:
        scores: Similarity scores of the documents retrieved from the
            Base_de_Conhecimento for the request.
        threshold: The configured relevance threshold (``limiar_relevancia``).
        original_request: The original user request to preserve.

    Returns:
        A :class:`RelevanceDecision` describing the outcome.
    """
    if has_relevant_document(scores, threshold):
        return RelevanceDecision(
            relevant=True,
            status=None,
            escalate=False,
            original_request=original_request,
        )
    return RelevanceDecision(
        relevant=False,
        status=AgentStatus.NOT_FOUND,
        escalate=True,
        original_request=original_request,
    )
