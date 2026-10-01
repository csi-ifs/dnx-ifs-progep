"""Unit tests for the Amazon Connect router adapter (Task 12.4).

Covers Req 3.1/3.2 (starting escalation), 3.3 (full history transfer),
3.4 (enqueue independent of availability), 3.5 (3 attempts of 5 s each,
unavailability + history preservation on exhaustion) and 3.6 (queue position /
estimated wait). The Amazon Connect client is mocked so no AWS access is needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.adapters.connect_router import (
    CONNECT_UNAVAILABLE_MESSAGE,
    ConnectRouterAdapter,
    EscalationResult,
    QueueStatus,
)
from src.domain.config import CONNECT_ATTEMPT_TIMEOUT_S, MAX_RETRIES
from src.domain.models import Message, Session

BASE = datetime(2025, 1, 1, 12, 0, 0)


def _session(n_messages: int = 3) -> Session:
    messages = tuple(
        Message(
            role="user" if i % 2 == 0 else "assistant",
            text=f"mensagem {i}",
            at=BASE + timedelta(minutes=i),
        )
        for i in range(n_messages)
    )
    return Session(
        session_id="sess-123",
        user_key="user-abc",
        channel="site",
        messages=messages,
        last_activity=BASE + timedelta(minutes=n_messages),
        active=True,
    )


def _metric_response(in_queue: int, oldest_age: int) -> dict:
    return {
        "MetricResults": [
            {
                "Collections": [
                    {
                        "Metric": {"Name": "CONTACTS_IN_QUEUE"},
                        "Value": float(in_queue),
                    },
                    {
                        "Metric": {"Name": "OLDEST_CONTACT_AGE"},
                        "Value": float(oldest_age),
                    },
                ]
            }
        ]
    }


def _adapter(client: MagicMock) -> ConnectRouterAdapter:
    return ConnectRouterAdapter(
        instance_id="inst-1",
        contact_flow_id="flow-1",
        queue_id="queue-1",
        connect_client=client,
    )


@pytest.mark.unit
def test_successful_escalation_enqueues_and_reports_queue_status():
    client = MagicMock()
    client.start_chat_contact.return_value = {"ContactId": "contact-xyz"}
    client.get_current_metric_data.return_value = _metric_response(4, 120)

    result = _adapter(client).escalate(_session())

    assert isinstance(result, EscalationResult)
    assert result.escalated is True
    assert result.contact_id == "contact-xyz"
    assert result.unavailable is False
    assert result.attempts_made == 1
    # Req 3.6: queue position / estimated wait reported.
    assert result.queue_status == QueueStatus(position=4, estimated_wait_seconds=120)


@pytest.mark.unit
def test_history_transferred_in_full_and_in_order():
    client = MagicMock()
    client.start_chat_contact.return_value = {"ContactId": "c1"}
    client.get_current_metric_data.return_value = _metric_response(1, 0)

    session = _session(5)
    _adapter(client).escalate(session)

    # Req 3.3 / Property 8: full history transferred in the same order.
    kwargs = client.start_chat_contact.call_args.kwargs
    history = json.loads(kwargs["Attributes"]["session_history"])
    assert [h["text"] for h in history] == [m.text for m in session.messages]
    assert [h["role"] for h in history] == [m.role for m in session.messages]
    # Session identity is carried in attributes.
    assert kwargs["Attributes"]["session_id"] == session.session_id
    assert kwargs["Attributes"]["channel"] == session.channel


@pytest.mark.unit
def test_enqueue_independent_of_attendant_availability():
    # Req 3.4: enqueue succeeds even when no agents are available (queue only).
    client = MagicMock()
    client.start_chat_contact.return_value = {"ContactId": "c1"}
    # Zero available agents reflected by a growing queue; escalation still enqueues.
    client.get_current_metric_data.return_value = _metric_response(7, 300)

    result = _adapter(client).escalate(_session())

    assert result.escalated is True
    assert client.start_chat_contact.called
    assert result.queue_status.position == 7


@pytest.mark.unit
def test_retries_up_to_three_attempts_then_succeeds():
    client = MagicMock()
    # Two transient failures, third attempt succeeds -> 3 attempts total (Req 3.5).
    client.start_chat_contact.side_effect = [
        ConnectionError("attempt 1 failed"),
        ConnectionError("attempt 2 failed"),
        {"ContactId": "c-late"},
    ]
    client.get_current_metric_data.return_value = _metric_response(2, 30)

    result = _adapter(client).escalate(_session())

    assert result.escalated is True
    assert result.contact_id == "c-late"
    assert result.attempts_made == MAX_RETRIES
    assert client.start_chat_contact.call_count == MAX_RETRIES


@pytest.mark.unit
def test_exhausted_attempts_signal_unavailable_and_preserve_history():
    client = MagicMock()
    client.start_chat_contact.side_effect = ConnectionError("connect down")

    session = _session(4)
    result = _adapter(client).escalate(session)

    # Req 3.5: after 3 failed attempts, signal unavailability.
    assert result.escalated is False
    assert result.unavailable is True
    assert result.contact_id is None
    assert result.attempts_made == MAX_RETRIES
    assert client.start_chat_contact.call_count == MAX_RETRIES
    assert result.user_message == CONNECT_UNAVAILABLE_MESSAGE
    # History preserved unchanged.
    assert result.preserved_history == session.messages


@pytest.mark.unit
def test_queue_status_best_effort_when_metrics_fail():
    client = MagicMock()
    client.start_chat_contact.return_value = {"ContactId": "c1"}
    client.get_current_metric_data.side_effect = ConnectionError("metrics down")

    result = _adapter(client).escalate(_session())

    # Escalation still succeeds; queue status is best-effort empty.
    assert result.escalated is True
    assert result.queue_status == QueueStatus(position=None, estimated_wait_seconds=None)


@pytest.mark.unit
def test_per_attempt_timeout_is_five_seconds():
    # Req 3.5: each connection attempt has a 5 s timeout.
    assert CONNECT_ATTEMPT_TIMEOUT_S == 5


@pytest.mark.unit
def test_module_imports_without_boto3():
    # The module must import even if boto3 is absent (guarded import).
    import src.adapters.connect_router as mod

    assert hasattr(mod, "ConnectRouterAdapter")
