"""Unit tests for session-creation failure without partial persistence (Task 3.6).

Covers Requirement 4.6: IF the creation of a session fails, THEN the system
rejects the request and returns an error indicating the failure, WITHOUT
persisting partial session data.

These are example-based tests (not property tests) exercising the pure-logic
functions ``create_session`` and ``resolve_or_create_session`` in
``src.domain.session``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.config import DEFAULT_TIMEOUT_MIN
from src.domain.models import ErrorDetail, Session
from src.domain.session import (
    SESSION_CREATION_FAILED,
    SessionCreationError,
    create_session,
    resolve_or_create_session,
)

NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


# --- create_session raises on invalid inputs (Req 4.6) --------------------------

@pytest.mark.parametrize("bad_user_key", ["", "   ", "\t", "\n", "  \t\n "])
def test_create_session_rejects_blank_user_key(bad_user_key):
    """A blank/whitespace-only user_key must be rejected (Req 4.6)."""
    with pytest.raises(SessionCreationError):
        create_session(bad_user_key, "site", NOW)


def test_create_session_rejects_none_user_key():
    """A ``None`` user_key must be rejected (Req 4.6)."""
    with pytest.raises(SessionCreationError):
        create_session(None, "site", NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_channel", ["", "email", "sms", "SITE", "telegram"])
def test_create_session_rejects_unknown_channel(bad_channel):
    """An unsupported channel must be rejected (Req 4.6)."""
    with pytest.raises(SessionCreationError):
        create_session("user-1", bad_channel, NOW)


def test_create_session_succeeds_for_valid_inputs():
    """Valid inputs produce a whole, active session with no messages (Req 4.1)."""
    session = create_session("user-1", "site", NOW)
    assert isinstance(session, Session)
    assert session.user_key == "user-1"
    assert session.channel == "site"
    assert session.messages == ()
    assert session.last_activity == NOW
    assert session.active is True
    assert session.session_id  # non-empty


# --- resolve_or_create_session returns error, no partial data (Req 4.6) ---------

def test_resolve_or_create_returns_error_on_blank_user_key():
    """Failure yields session=None and a populated SESSION_CREATION_FAILED error."""
    result = resolve_or_create_session(
        [], "   ", "site", NOW, DEFAULT_TIMEOUT_MIN
    )
    assert result.session is None
    assert result.created is False
    assert isinstance(result.error, ErrorDetail)
    assert result.error.code == SESSION_CREATION_FAILED
    assert result.error.message  # non-empty description


def test_resolve_or_create_returns_error_on_unknown_channel():
    """Unknown channel fails resolution without producing a session."""
    result = resolve_or_create_session(
        [], "user-1", "carrier-pigeon", NOW, DEFAULT_TIMEOUT_MIN
    )
    assert result.session is None
    assert result.created is False
    assert result.error is not None
    assert result.error.code == SESSION_CREATION_FAILED


def test_resolve_or_create_failure_does_not_mutate_store():
    """A failed creation must not persist partial session data (Req 4.6).

    The caller-owned store list is passed by reference; a failure must leave it
    untouched (no partial session appended).
    """
    store: list[Session] = []
    result = resolve_or_create_session(
        store, "", "site", NOW, DEFAULT_TIMEOUT_MIN
    )
    assert result.session is None
    assert store == []  # nothing persisted


def test_resolve_or_create_creates_session_on_success():
    """A valid new pair yields a created session and no error."""
    result = resolve_or_create_session(
        [], "user-99", "whatsapp", NOW, DEFAULT_TIMEOUT_MIN
    )
    assert result.error is None
    assert result.created is True
    assert result.session is not None
    assert result.session.user_key == "user-99"
    assert result.session.channel == "whatsapp"
    assert result.session.active is True
