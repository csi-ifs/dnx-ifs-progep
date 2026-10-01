"""Confidence-based routing decision for the Roteador_de_Atendimento.

Implements the pure-logic decision that determines whether an agent response is
delivered directly to the Usuario or escalated to an Atendente_Humano via Amazon
Connect. Per Requirements 3.1 and 3.2, an interaction is escalated when the
response confidence is below the configured Limite_de_Confianca, or when the
Usuario explicitly requests a human; otherwise the response is delivered.

This module is intentionally free of any I/O so it can be exercised by
property-based tests (Property 7). The actual escalation call to Amazon Connect
lives in a separate adapter.
"""

from __future__ import annotations

from src.domain.config import RoutingDecision
from src.domain.models import AgentResponse


def decide_routing(
    resp: AgentResponse,
    threshold: float,
    user_requested_human: bool,
) -> str:
    """Decide whether to deliver the response or escalate to a human (Req 3.1, 3.2).

    The interaction is escalated **if and only if** the response confidence is
    strictly below the configured ``threshold`` (Limite_de_Confianca, Req 3.1)
    **or** the Usuario explicitly requested a human (Req 3.2). In every other
    case the response is delivered.

    Args:
        resp: The response produced by the Agente_IA, carrying the confidence
            score used for the threshold comparison.
        threshold: The configured Limite_de_Confianca. A confidence value equal
            to or above this threshold is sufficient for delivery.
        user_requested_human: ``True`` when the Usuario explicitly asked to speak
            with an Atendente_Humano (Req 3.2).

    Returns:
        ``RoutingDecision.ESCALATE.value`` (``"escalate"``) when the interaction
        must be escalated, otherwise ``RoutingDecision.DELIVER.value``
        (``"deliver"``).
    """
    if resp.confidence < threshold or user_requested_human:
        return RoutingDecision.ESCALATE.value
    return RoutingDecision.DELIVER.value
