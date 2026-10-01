"""Property-based test for full-history transfer on escalation.

Feature: assistente-progep-ifs, Property 8: Transferência do histórico completo
no escalonamento.

Validates: Requirements 3.3.

For any session, the escalation payload assembled for Amazon Connect contains all
messages of the current session history, in the same order they appear on the
session, and preserves the session identity (session_id, user_key, channel).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import Channel
from src.domain.escalation import build_escalation_payload
from src.domain.models import Message, Session

pytestmark = pytest.mark.property


# Base instant for building deterministic-but-arbitrary timestamps.
_BASE = datetime(2025, 1, 1, 0, 0, 0)

_CHANNELS = [c.value for c in Channel]


@st.composite
def messages(draw) -> Message:
    """Generate a Message with arbitrary role, text and timestamp."""
    role = draw(st.sampled_from(["user", "assistant"]))
    text = draw(st.text(max_size=50))
    offset = draw(st.integers(min_value=0, max_value=10_000))
    return Message(role=role, text=text, at=_BASE + timedelta(seconds=offset))


@st.composite
def sessions(draw) -> Session:
    """Generate a Session with an arbitrary list of messages in arbitrary order."""
    msgs = draw(st.lists(messages(), max_size=60))
    return Session(
        session_id=draw(st.text(min_size=1, max_size=12)),
        user_key=draw(st.text(min_size=1, max_size=12)),
        channel=draw(st.sampled_from(_CHANNELS)),
        messages=tuple(msgs),
        last_activity=None,
        active=True,
    )


@given(session=sessions())
def test_escalation_payload_transfers_full_history_in_order(session: Session) -> None:
    """The payload carries every message in the exact session order and identity."""
    payload = build_escalation_payload(session)

    # 1) Session identity is preserved verbatim.
    assert payload.session_id == session.session_id
    assert payload.user_key == session.user_key
    assert payload.channel == session.channel

    # 2) All messages are transferred (no truncation / filtering).
    assert len(payload.messages) == len(session.messages)

    # 3) Messages equal the session history in the same order (copied verbatim,
    #    not re-sorted).
    assert tuple(payload.messages) == tuple(session.messages)
