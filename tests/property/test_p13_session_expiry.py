"""Property-based test for session inactivity expiry.

Feature: assistente-progep-ifs, Property 13: Expiração de sessão por inatividade.

Validates: Requirements 4.3.

For any current instant, last-message timestamp and inactivity timeout within
5..120 minutes, the session is considered active if and only if the time elapsed
since the last message is strictly less than the timeout; expired sessions have
their history removed (expire_session clears messages and sets active=False).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import MAX_TIMEOUT_MIN, MIN_TIMEOUT_MIN
from src.domain.models import Message, Session
from src.domain.session import expire_session, is_active

pytestmark = pytest.mark.property


# Base instant for building deterministic-but-arbitrary timestamps.
_BASE = datetime(2025, 1, 1, 0, 0, 0)


def _session(last_activity: datetime | None, messages: tuple[Message, ...] = ()) -> Session:
    return Session(
        session_id="s-fixed",
        user_key="u1",
        channel="site",
        messages=messages,
        last_activity=last_activity,
        active=True,
    )


@given(
    # Offset (in seconds) of the last activity relative to _BASE.
    last_offset=st.integers(min_value=0, max_value=1_000_000),
    # Signed elapsed offset (in seconds) applied to derive `now`; negative values
    # place `now` before the last activity, positive after.
    elapsed_s=st.integers(min_value=-500_000, max_value=500_000),
    timeout_min=st.integers(min_value=MIN_TIMEOUT_MIN, max_value=MAX_TIMEOUT_MIN),
)
def test_is_active_iff_elapsed_below_timeout(
    last_offset: int, elapsed_s: int, timeout_min: int
) -> None:
    """is_active holds iff (now - last_activity) < timeout minutes."""
    last_activity = _BASE + timedelta(seconds=last_offset)
    now = last_activity + timedelta(seconds=elapsed_s)

    elapsed = now - last_activity
    expected_active = elapsed < timedelta(minutes=timeout_min)

    assert is_active(_session(last_activity), now, timeout_min) is expected_active


@given(
    last_offset=st.integers(min_value=0, max_value=1_000_000),
    timeout_min=st.integers(min_value=MIN_TIMEOUT_MIN, max_value=MAX_TIMEOUT_MIN),
)
def test_exact_timeout_boundary_is_inactive(last_offset: int, timeout_min: int) -> None:
    """At exactly the timeout, elapsed is not strictly less, so it is inactive."""
    last_activity = _BASE + timedelta(seconds=last_offset)
    now = last_activity + timedelta(minutes=timeout_min)

    assert is_active(_session(last_activity), now, timeout_min) is False


@given(
    n_messages=st.integers(min_value=0, max_value=50),
    active_before=st.booleans(),
)
def test_expire_session_clears_history_and_deactivates(
    n_messages: int, active_before: bool
) -> None:
    """Expiring a session removes its history and marks it inactive (Req 4.3)."""
    messages = tuple(
        Message(role="user", text=f"m{i}", at=_BASE + timedelta(seconds=i))
        for i in range(n_messages)
    )
    session = Session(
        session_id="s-fixed",
        user_key="u1",
        channel="site",
        messages=messages,
        last_activity=_BASE if messages else None,
        active=active_before,
    )

    expired = expire_session(session)

    assert expired.messages == ()
    assert expired.active is False
    # Original session is untouched (immutability).
    assert session.messages == messages
