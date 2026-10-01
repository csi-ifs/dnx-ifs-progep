"""Property test for session identifier uniqueness.

Feature: assistente-progep-ifs, Property 11: Unicidade de identificador de sessão

For any sequência de criações de sessão, todos os session_id gerados são
distintos entre si, considerando tanto sessões ativas quanto encerradas.

Validates: Requirements 4.1
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.config import Channel
from src.domain.session import create_session, expire_session, generate_session_id

# Base instant used to derive per-session ``now`` timestamps deterministically.
_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)

# A single session-creation instruction: a (user_key, channel, expire?) triple.
_creation = st.tuples(
    st.text(min_size=1, max_size=20).filter(lambda s: s.strip() != ""),
    st.sampled_from([c.value for c in Channel]),
    st.booleans(),
)


@settings(max_examples=200)
@given(creations=st.lists(_creation, min_size=1, max_size=100))
def test_generated_session_ids_are_distinct(
    creations: list[tuple[str, str, bool]],
) -> None:
    """All session_ids from a sequence of creations are pairwise distinct.

    Feature: assistente-progep-ifs, Property 11: Unicidade de identificador de sessão
    Validates: Requirements 4.1

    Sessions are created for arbitrary (user_key, channel) pairs — including
    repeated pairs — and some are subsequently expired (encerradas). Regardless
    of activity state, every generated identifier must be unique.
    """
    session_ids: list[str] = []
    for index, (user_key, channel, should_expire) in enumerate(creations):
        now = _BASE + timedelta(seconds=index)
        session = create_session(user_key, channel, now)
        if should_expire:
            session = expire_session(session)
        session_ids.append(session.session_id)

    assert len(session_ids) == len(set(session_ids))


@settings(max_examples=200)
@given(count=st.integers(min_value=1, max_value=500))
def test_generate_session_id_produces_distinct_values(count: int) -> None:
    """Repeated direct calls to generate_session_id yield distinct identifiers.

    Feature: assistente-progep-ifs, Property 11: Unicidade de identificador de sessão
    Validates: Requirements 4.1
    """
    ids = [generate_session_id() for _ in range(count)]

    assert len(ids) == len(set(ids))
