"""Property-based test for absence of relevant documents.

Feature: assistente-progep-ifs, Property 4: Ausência de documentos relevantes

For any lista de scores de recuperação e limiar de relevância, se nenhum score
é >= limiar então a decisão resultante é ``not_found`` E o
Roteador_de_Atendimento é acionado (escalonamento); a requisição original é
preservada. Caso contrário (ao menos um score >= limiar) a decisão é relevante e
não há escalonamento nessa base.

Validates: Requirements 2.4
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import AgentStatus
from src.domain.models import AgentRequest
from src.domain.relevance import decide_relevance

pytestmark = pytest.mark.property

# Finite floats spanning below/at/above typical thresholds (includes negatives
# and values > 1 to avoid over-constraining the input space).
_score_strategy = st.floats(
    min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False
)
# Arbitrary score lists, including the empty list (no documents retrieved).
_scores_strategy = st.lists(_score_strategy, min_size=0, max_size=10)
_threshold_strategy = st.floats(
    min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False
)


@given(scores=_scores_strategy, threshold=_threshold_strategy)
def test_relevance_decision_matches_threshold(
    scores: list[float], threshold: float
) -> None:
    """No score >= threshold -> not_found + escalation; else relevant, no escalation.

    Validates: Requirements 2.4
    """
    original_request = AgentRequest(
        session_id="s-prop-04", channel="site", text="pergunta original"
    )

    decision = decide_relevance(scores, threshold, original_request)

    has_relevant = any(score >= threshold for score in scores)

    if not has_relevant:
        # Absence of relevant documents: not_found + escalation.
        assert decision.relevant is False
        assert decision.status is AgentStatus.NOT_FOUND
        assert decision.escalate is True
    else:
        # At least one relevant document: relevant, no escalation on this basis.
        assert decision.relevant is True
        assert decision.escalate is False

    # The original request is always preserved unchanged (identity).
    assert decision.original_request is original_request
