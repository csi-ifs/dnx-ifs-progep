"""Unit tests for the unavailability handling logic (Task 7.3, Req 2.5, 5.5, 6.6)."""

from __future__ import annotations

import pytest

from src.domain.availability import (
    UNAVAILABLE_USER_MESSAGE,
    Dependency,
    FailureKind,
    UnavailabilityResult,
    signal_unavailable,
)
from src.domain.config import AgentStatus
from src.domain.models import AgentRequest

REQUEST = AgentRequest(session_id="s-1", channel="site", text="pergunta original")


@pytest.mark.unit
def test_kb_query_failure_signals_unavailable_escalates_and_preserves_input():
    result = signal_unavailable(Dependency.KB_QUERY, REQUEST)

    assert isinstance(result, UnavailabilityResult)
    assert result.status == AgentStatus.UNAVAILABLE.value
    assert result.dependency == Dependency.KB_QUERY.value
    assert result.failure_kind == FailureKind.FAILURE.value
    assert result.user_message == UNAVAILABLE_USER_MESSAGE
    # Req 2.5: KB failure triggers the Roteador_de_Atendimento.
    assert result.escalate is True
    # Original request preserved unchanged (same object, same value).
    assert result.original_input is REQUEST
    assert result.original_input == REQUEST


@pytest.mark.unit
def test_kb_query_timeout_marks_timeout_kind():
    result = signal_unavailable(Dependency.KB_QUERY, REQUEST, timed_out=True)

    assert result.failure_kind == FailureKind.TIMEOUT.value
    assert result.status == AgentStatus.UNAVAILABLE.value
    assert result.escalate is True


@pytest.mark.unit
def test_agent_response_unavailable_does_not_escalate_by_default():
    # Req 5.5: API returns a structured unavailability error, no human escalation.
    result = signal_unavailable(Dependency.AGENT_RESPONSE, REQUEST, timed_out=True)

    assert result.dependency == Dependency.AGENT_RESPONSE.value
    assert result.failure_kind == FailureKind.TIMEOUT.value
    assert result.escalate is False
    assert result.original_input is REQUEST


@pytest.mark.unit
def test_connect_forwarding_failure_preserves_message_no_escalation():
    # Req 6.6: WhatsApp connector preserves the received message and informs user.
    message = "Olá, preciso de ajuda com meu benefício."
    result = signal_unavailable(Dependency.CONNECT_FORWARDING, message)

    assert result.dependency == Dependency.CONNECT_FORWARDING.value
    assert result.escalate is False
    assert result.original_input == message
    assert result.status == AgentStatus.UNAVAILABLE.value


@pytest.mark.unit
def test_escalate_override_is_respected():
    forced = signal_unavailable(Dependency.CONNECT_FORWARDING, REQUEST, escalate=True)
    assert forced.escalate is True

    suppressed = signal_unavailable(Dependency.KB_QUERY, REQUEST, escalate=False)
    assert suppressed.escalate is False


@pytest.mark.unit
def test_custom_user_message_is_used():
    result = signal_unavailable(
        Dependency.KB_QUERY, REQUEST, user_message="Mensagem personalizada"
    )
    assert result.user_message == "Mensagem personalizada"


@pytest.mark.unit
def test_result_is_immutable():
    result = signal_unavailable(Dependency.KB_QUERY, REQUEST)
    with pytest.raises(Exception):
        result.status = "answered"  # type: ignore[misc]


@pytest.mark.unit
def test_invalid_dependency_raises_type_error():
    with pytest.raises(TypeError):
        signal_unavailable("kb_query", REQUEST)  # type: ignore[arg-type]
