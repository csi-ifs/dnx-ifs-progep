"""Property-based test for failure/unavailability handling.

Feature: assistente-progep-ifs, Property 9: Falha/indisponibilidade preserva a
entrada e sinaliza indisponibilidade.

For any requisição ou mensagem original, dado um sinal de falha ou expiração de
tempo em uma dependência (KB, Agente, ou Connect), o resultado sinaliza
indisponibilidade de forma estruturada E preserva a entrada original inalterada.

Validates: Requirements 2.5, 5.5, 6.6
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.availability import (
    Dependency,
    FailureKind,
    UnavailabilityResult,
    signal_unavailable,
)
from src.domain.config import AgentStatus
from src.domain.models import AgentRequest

# --- Generators -----------------------------------------------------------------

#: Any downstream dependency that may fail or time out (KB, Agente, Connect).
dependencies = st.sampled_from(list(Dependency))

#: AgentRequest inputs (site/whatsapp requests routed to the agent).
agent_requests = st.builds(
    AgentRequest,
    session_id=st.text(min_size=1, max_size=20),
    channel=st.sampled_from(["site", "whatsapp"]),
    text=st.text(max_size=100),
)

#: Varied original inputs: structured requests, plain strings, and tuples.
original_inputs = st.one_of(
    agent_requests,
    st.text(max_size=200),
    st.tuples(st.text(max_size=20), st.integers()),
)


@pytest.mark.property
@settings(max_examples=200)
@given(
    dependency=dependencies,
    timed_out=st.booleans(),
    original_input=original_inputs,
)
def test_failure_signals_unavailable_and_preserves_input(
    dependency: Dependency, timed_out: bool, original_input: object
) -> None:
    """Property 9: unavailability is signalled structurally, input preserved."""
    result = signal_unavailable(dependency, original_input, timed_out=timed_out)

    # Structured result.
    assert isinstance(result, UnavailabilityResult)

    # Signals unavailability in a machine-readable way.
    assert result.status == AgentStatus.UNAVAILABLE.value

    # The reported dependency matches the failing one.
    assert result.dependency == dependency.value

    # failure_kind reflects whether it was a timeout or a generic failure.
    expected_kind = FailureKind.TIMEOUT if timed_out else FailureKind.FAILURE
    assert result.failure_kind == expected_kind.value

    # The original input is preserved unchanged: identity AND equality.
    assert result.original_input is original_input
    assert result.original_input == original_input

    # A user-facing (non-empty) message accompanies the unavailability.
    assert isinstance(result.user_message, str)
    assert result.user_message


@pytest.mark.property
@settings(max_examples=200)
@given(
    timed_out=st.booleans(),
    original_input=original_inputs,
)
def test_kb_query_escalates_by_default(
    timed_out: bool, original_input: object
) -> None:
    """Property 9 (Req 2.5): a KB failure escalates to the Roteador by default."""
    result = signal_unavailable(Dependency.KB_QUERY, original_input, timed_out=timed_out)

    assert result.escalate is True
    assert result.status == AgentStatus.UNAVAILABLE.value
    assert result.original_input is original_input
