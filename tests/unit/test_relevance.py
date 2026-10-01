"""Unit tests for the relevance decision logic (Task 6.3, Req 2.4)."""

from __future__ import annotations

import pytest

from src.domain.config import AgentStatus
from src.domain.models import AgentRequest
from src.domain.relevance import decide_relevance, has_relevant_document

REQUEST = AgentRequest(session_id="s-1", channel="site", text="pergunta original")


@pytest.mark.unit
def test_no_relevant_document_yields_not_found_and_escalation():
    decision = decide_relevance([0.1, 0.4, 0.49], threshold=0.5, original_request=REQUEST)

    assert decision.relevant is False
    assert decision.status is AgentStatus.NOT_FOUND
    assert decision.escalate is True
    # Original request preserved unchanged.
    assert decision.original_request is REQUEST


@pytest.mark.unit
def test_empty_scores_yields_not_found_and_escalation():
    decision = decide_relevance([], threshold=0.5, original_request=REQUEST)

    assert decision.relevant is False
    assert decision.status is AgentStatus.NOT_FOUND
    assert decision.escalate is True
    assert decision.original_request is REQUEST


@pytest.mark.unit
def test_score_at_threshold_is_relevant_no_escalation():
    decision = decide_relevance([0.2, 0.5], threshold=0.5, original_request=REQUEST)

    assert decision.relevant is True
    assert decision.status is None
    assert decision.escalate is False
    assert decision.original_request is REQUEST


@pytest.mark.unit
def test_score_above_threshold_is_relevant():
    decision = decide_relevance([0.9], threshold=0.5, original_request=REQUEST)

    assert decision.relevant is True
    assert decision.escalate is False


@pytest.mark.unit
def test_has_relevant_document_helper():
    assert has_relevant_document([0.5], 0.5) is True
    assert has_relevant_document([0.49], 0.5) is False
    assert has_relevant_document([], 0.5) is False
