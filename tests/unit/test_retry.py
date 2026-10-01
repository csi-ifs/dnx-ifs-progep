"""Unit tests for the bounded retry policy (Task 7.5, Req 3.5, 6.5, 7.5)."""

from __future__ import annotations

import pytest

from src.domain.config import MAX_RETRIES, MIN_WHATSAPP_RETRY_INTERVAL_S
from src.domain.retry import (
    RetryOperation,
    decide_retry,
    min_retry_interval_s,
    plan_retries,
)


@pytest.mark.unit
def test_min_interval_only_for_whatsapp():
    assert min_retry_interval_s(RetryOperation.WHATSAPP_DELIVERY) == (
        MIN_WHATSAPP_RETRY_INTERVAL_S
    )
    assert min_retry_interval_s(RetryOperation.CONNECT_ESCALATION) == 0
    assert min_retry_interval_s(RetryOperation.MONITOR_RECORDING) == 0


@pytest.mark.unit
@pytest.mark.parametrize("operation", list(RetryOperation))
def test_plan_never_exceeds_max_attempts(operation):
    plan = plan_retries(operation)

    assert plan.max_attempts == MAX_RETRIES
    assert len(plan.attempts) == MAX_RETRIES
    assert [a.attempt for a in plan.attempts] == list(range(1, MAX_RETRIES + 1))
    assert plan.signals_unavailability() is True


@pytest.mark.unit
def test_whatsapp_plan_spaces_attempts_by_min_interval():
    plan = plan_retries(RetryOperation.WHATSAPP_DELIVERY)

    offsets = [a.earliest_offset_s for a in plan.attempts]
    gaps = [b - a for a, b in zip(offsets, offsets[1:])]
    assert all(gap >= MIN_WHATSAPP_RETRY_INTERVAL_S for gap in gaps)


@pytest.mark.unit
def test_connect_plan_has_no_forced_spacing():
    plan = plan_retries(RetryOperation.CONNECT_ESCALATION)

    assert all(a.earliest_offset_s == 0 for a in plan.attempts)


@pytest.mark.unit
def test_decide_retry_allows_until_max():
    for attempts_made in range(MAX_RETRIES):
        decision = decide_retry(RetryOperation.WHATSAPP_DELIVERY, attempts_made)
        assert decision.should_retry is True
        assert decision.next_attempt == attempts_made + 1
        assert decision.wait_s == MIN_WHATSAPP_RETRY_INTERVAL_S
        assert decision.exhausted is False
        assert decision.unavailable is False


@pytest.mark.unit
def test_decide_retry_signals_unavailable_on_exhaustion():
    decision = decide_retry(RetryOperation.CONNECT_ESCALATION, MAX_RETRIES)

    assert decision.should_retry is False
    assert decision.next_attempt is None
    assert decision.wait_s is None
    assert decision.exhausted is True
    assert decision.unavailable is True
    assert decision.reason is not None
    assert "connect_escalation" in decision.reason


@pytest.mark.unit
def test_decide_retry_monitor_no_wait():
    decision = decide_retry(RetryOperation.MONITOR_RECORDING, 0)

    assert decision.should_retry is True
    assert decision.wait_s == 0


@pytest.mark.unit
def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        decide_retry(RetryOperation.WHATSAPP_DELIVERY, -1)
    with pytest.raises(ValueError):
        decide_retry(RetryOperation.WHATSAPP_DELIVERY, 0, max_attempts=MAX_RETRIES + 1)
    with pytest.raises(ValueError):
        plan_retries(RetryOperation.WHATSAPP_DELIVERY, max_attempts=0)
