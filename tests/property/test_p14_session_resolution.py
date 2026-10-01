"""Property-based test for session resolution by user and channel.

Feature: assistente-progep-ifs, Property 14: Resolução de sessão por usuário e canal

For any conjunto de sessões e par (usuário, canal), a resolução retorna a sessão
ativa correspondente quando ela existe; quando não existe sessão ativa
correspondente, a resolução indica a necessidade de criar uma nova sessão antes
de processar a requisição (retornando ``None``).

Validates: Requirements 4.4, 4.5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.config import MAX_TIMEOUT_MIN, MIN_TIMEOUT_MIN, Channel
from src.domain.models import Session
from src.domain.session import is_active, resolve_session

pytestmark = pytest.mark.property


# Reference instant against which session activity is evaluated.
_NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Small pools so that queries collide with generated sessions often enough to
# exercise both the "match found" and "no match" branches of resolve_session.
_USER_KEYS = ["u-alice", "u-bob", "u-carol", "u-dave"]
_CHANNELS = [c.value for c in Channel]


@st.composite
def sessions(draw) -> Session:
    """Generate a Session with varying user_key, channel, last_activity, active.

    ``last_activity`` is drawn as a minute offset relative to ``_NOW`` spanning
    values well within and well beyond the maximum timeout, plus ``None``, so
    both active and inactive sessions are produced. The ``active`` flag varies
    independently of the time-based activity so the test does not conflate the
    two notions.
    """
    user_key = draw(st.sampled_from(_USER_KEYS))
    channel = draw(st.sampled_from(_CHANNELS))
    minutes_ago = draw(st.one_of(st.none(), st.integers(min_value=-5, max_value=200)))
    last_activity = None if minutes_ago is None else _NOW - timedelta(minutes=minutes_ago)
    active = draw(st.booleans())
    return Session(
        session_id=f"s-{draw(st.integers(min_value=0, max_value=1_000_000))}",
        user_key=user_key,
        channel=channel,
        messages=(),
        last_activity=last_activity,
        active=active,
    )


@settings(max_examples=200)
@given(
    session_list=st.lists(sessions(), max_size=25),
    query_user=st.sampled_from(_USER_KEYS),
    query_channel=st.sampled_from(_CHANNELS),
    timeout_min=st.integers(min_value=MIN_TIMEOUT_MIN, max_value=MAX_TIMEOUT_MIN),
)
def test_resolve_returns_active_match_or_none(
    session_list: list[Session],
    query_user: str,
    query_channel: str,
    timeout_min: int,
) -> None:
    """resolve_session returns an active matching session, else None.

    Feature: assistente-progep-ifs, Property 14: Resolução de sessão por usuário e canal
    Validates: Requirements 4.4, 4.5

    The oracle for "active matching session" is a session whose (user_key,
    channel) equal the query and which is_active at _NOW under timeout_min —
    exactly the notion resolve_session is specified against (Req 4.4). When no
    such session exists, resolution must return None, signalling that a new
    session has to be created before processing the request (Req 4.5).
    """
    result = resolve_session(
        session_list, query_user, query_channel, _NOW, timeout_min
    )

    # Reference set of sessions that qualify as an active match.
    candidates = [
        s
        for s in session_list
        if s.user_key == query_user
        and s.channel == query_channel
        and is_active(s, _NOW, timeout_min)
    ]

    if candidates:
        # A matching active session exists → resolution must return one of them.
        assert result is not None
        assert result in candidates
        assert result.user_key == query_user
        assert result.channel == query_channel
        assert is_active(result, _NOW, timeout_min)
    else:
        # No matching active session → resolution signals a new session is needed.
        assert result is None
