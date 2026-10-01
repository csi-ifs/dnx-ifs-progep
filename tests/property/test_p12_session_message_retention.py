"""Property-based test for session message retention.

Feature: assistente-progep-ifs, Property 12: Invariante de retenção de mensagens
da sessão.

Validates: Requirements 4.2.

For any session and any sequence of messages appended, after the insertions the
history holds at most MAX_MESSAGES (50) messages, and those are exactly the 50
(or fewer) most recent by timestamp, preserved in chronological order.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import MAX_MESSAGES
from src.domain.models import Message, Session
from src.domain.session import append_message

pytestmark = pytest.mark.property


# Base instant for building deterministic-but-arbitrary timestamps.
_BASE = datetime(2025, 1, 1, 0, 0, 0)


@st.composite
def messages(draw) -> Message:
    """Generate a Message with arbitrary role, text and timestamp."""
    role = draw(st.sampled_from(["user", "assistant"]))
    text = draw(st.text(max_size=50))
    # Offset in seconds keeps timestamps within a sane range while allowing
    # arbitrary ordering (messages may arrive out of chronological order).
    offset = draw(st.integers(min_value=0, max_value=10_000))
    return Message(role=role, text=text, at=_BASE + timedelta(seconds=offset))


def _empty_session() -> Session:
    return Session(
        session_id="s-fixed",
        user_key="u1",
        channel="site",
        messages=(),
        last_activity=None,
        active=True,
    )


@given(msgs=st.lists(messages(), max_size=120))
def test_session_retains_at_most_50_most_recent_in_order(msgs: list[Message]) -> None:
    """Appending any sequence keeps <=50 most-recent messages, chronological."""
    session = _empty_session()
    for m in msgs:
        session = append_message(session, m)

    kept = session.messages

    # 1) At most MAX_MESSAGES retained.
    assert len(kept) <= MAX_MESSAGES

    # Reference: what the retained set should be — the most recent MAX_MESSAGES
    # by timestamp, in chronological order.
    expected = tuple(sorted(msgs, key=lambda x: x.at))[-MAX_MESSAGES:]

    # 2) Retained messages are exactly the most recent ones by timestamp.
    assert kept == expected

    # 3) Retained messages are in chronological (non-decreasing) order.
    kept_times = [m.at for m in kept]
    assert kept_times == sorted(kept_times)

    # 4) Count equals min(total, MAX_MESSAGES).
    assert len(kept) == min(len(msgs), MAX_MESSAGES)
