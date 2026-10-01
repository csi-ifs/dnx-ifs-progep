"""Property-based test for the confidence-based routing decision.

Feature: assistente-progep-ifs, Property 7: Decisão de roteamento por confiança
e pedido explícito.

Validates: Requirements 3.1, 3.2

For any resposta com confiança arbitrária, limite de confiança e flag de pedido
explícito de humano, a decisão é ``escalate`` se e somente se a confiança é
inferior ao Limite_de_Confianca OU o Usuário solicitou explicitamente um humano;
caso contrário ``deliver``.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.config import RoutingDecision
from src.domain.models import AgentResponse
from src.domain.routing import decide_routing

# Finite floats only: NaN comparisons are not meaningful for a threshold.
_finite_floats = st.floats(allow_nan=False, allow_infinity=False)


@settings(max_examples=200)
@given(
    confidence=_finite_floats,
    threshold=_finite_floats,
    user_requested_human=st.booleans(),
)
def test_routing_decision_escalates_iff_low_confidence_or_human_requested(
    confidence: float,
    threshold: float,
    user_requested_human: bool,
) -> None:
    """decide_routing escalates iff confidence < threshold or human requested."""
    resp = AgentResponse(status="answered", content="x", confidence=confidence)

    decision = decide_routing(resp, threshold, user_requested_human)

    should_escalate = confidence < threshold or user_requested_human
    expected = (
        RoutingDecision.ESCALATE.value
        if should_escalate
        else RoutingDecision.DELIVER.value
    )
    assert decision == expected
