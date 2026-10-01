"""Unit tests for the consumption-monitoring failure recording (Task 9.3, Req 7.4)."""

from __future__ import annotations

import pytest

from src.domain.monitor import CallRecord, CallStatus, record_llm_failure


@pytest.mark.unit
def test_records_failure_with_correct_session_id_and_status():
    result = record_llm_failure((), "sess-1")

    assert len(result) == 1
    assert result[0] == CallRecord(session_id="sess-1", status="failed", reason=None)
    assert result[0].status == CallStatus.FAILED.value


@pytest.mark.unit
def test_preserves_previous_records_unchanged():
    prior = (
        CallRecord(session_id="sess-1", status=CallStatus.SUCCESS.value),
        CallRecord(session_id="sess-2", status=CallStatus.FAILED.value, reason="boom"),
    )

    result = record_llm_failure(prior, "sess-3", reason="timeout")

    # Previous records preserved verbatim, in order, as the same objects.
    assert result[0] is prior[0]
    assert result[1] is prior[1]
    # New failure record appended at the end with the correct session_id.
    assert result[2] == CallRecord(
        session_id="sess-3", status="failed", reason="timeout"
    )
    assert len(result) == 3


@pytest.mark.unit
def test_input_tuple_is_not_mutated():
    prior = (CallRecord(session_id="sess-1", status=CallStatus.SUCCESS.value),)

    record_llm_failure(prior, "sess-2")

    assert len(prior) == 1  # original collection untouched


@pytest.mark.unit
def test_records_are_immutable():
    result = record_llm_failure((), "sess-1")
    with pytest.raises(Exception):
        result[0].status = "success"  # type: ignore[misc]


@pytest.mark.unit
def test_empty_session_id_raises_value_error():
    with pytest.raises(ValueError):
        record_llm_failure((), "")


@pytest.mark.unit
def test_non_string_session_id_raises_type_error():
    with pytest.raises(TypeError):
        record_llm_failure((), 123)  # type: ignore[arg-type]


@pytest.mark.unit
def test_non_tuple_records_raises_type_error():
    with pytest.raises(TypeError):
        record_llm_failure([], "sess-1")  # type: ignore[arg-type]
