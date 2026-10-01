"""Pure-logic session management for the Assistente PROGEP-IFS.

Implements the Gestor_de_Sessao behaviours as side-effect-free functions over the
immutable :class:`~src.domain.models.Session` type (Req 4.1-4.6). The signatures
mirror the "Components and Interfaces" section of ``design.md``:

    def append_message(s: Session, m: Message) -> Session: ...
    def is_active(s: Session, now: datetime, timeout_min: int) -> bool: ...
    def resolve_session(sessions, user_key, channel, now, timeout_min) -> Session | None: ...

Because :class:`Session` is ``frozen=True`` and every function returns a brand-new
value, there is no shared mutable state and no possibility of partially persisted
data: a session is either produced whole or not at all (Req 4.6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from src.domain.config import (
    MAX_MESSAGES,
    MAX_TIMEOUT_MIN,
    MIN_TIMEOUT_MIN,
    Channel,
)
from src.domain.models import ErrorDetail, Message, Session

#: Valid channel values accepted when creating/resolving sessions (Req 4.4).
_VALID_CHANNELS: frozenset[str] = frozenset(c.value for c in Channel)

#: Error code reported when session creation fails (Req 4.6).
SESSION_CREATION_FAILED: str = "session_creation_failed"


@dataclass(frozen=True)
class SessionResolution:
    """Outcome of resolving-or-creating a session for a (user_key, channel) pair.

    Exactly one of the following holds:

    * ``session`` set and ``error is None`` — a session is available (either an
      existing active one, ``created=False``, or a newly created one,
      ``created=True``).
    * ``session is None`` and ``error`` set — session creation failed and no
      partial data was persisted (Req 4.6).
    """

    session: Session | None
    created: bool
    error: ErrorDetail | None


def generate_session_id() -> str:
    """Return a fresh, universally-unique session identifier (Req 4.1).

    Uses UUID4 so identifiers are distinct across all active and closed sessions
    with overwhelming probability.
    """
    return str(uuid.uuid4())


def _validate_timeout(timeout_min: int) -> None:
    """Raise ``ValueError`` if ``timeout_min`` is outside the configurable range.

    The inactivity timeout must lie within 5..120 minutes (Req 4.3).
    """
    if not (MIN_TIMEOUT_MIN <= timeout_min <= MAX_TIMEOUT_MIN):
        raise ValueError(
            f"timeout_min must be within [{MIN_TIMEOUT_MIN}, {MAX_TIMEOUT_MIN}] "
            f"minutes, got {timeout_min}"
        )


class SessionCreationError(Exception):
    """Raised when a session cannot be created from the supplied inputs (Req 4.6)."""


def create_session(
    user_key: str,
    channel: str,
    now: datetime,
    *,
    session_id: str | None = None,
) -> Session:
    """Create a new, complete session for ``user_key`` on ``channel`` (Req 4.1).

    A unique ``session_id`` (UUID4) is generated unless one is supplied. The
    session starts with no messages, ``last_activity`` set to ``now`` and
    ``active=True``.

    Raises :class:`SessionCreationError` when ``user_key`` is blank or ``channel``
    is not a supported channel. Because the returned value is built atomically,
    no partial session data can be persisted on failure (Req 4.6).
    """
    if user_key is None or not user_key.strip():
        raise SessionCreationError("user_key must be a non-empty string")
    if channel not in _VALID_CHANNELS:
        raise SessionCreationError(f"unknown channel: {channel!r}")

    return Session(
        session_id=session_id or generate_session_id(),
        user_key=user_key,
        channel=channel,
        messages=(),
        last_activity=now,
        active=True,
    )


def append_message(s: Session, m: Message) -> Session:
    """Return a copy of ``s`` with ``m`` appended, retaining recent history.

    Invariant (Req 4.2, Property 12): after the call the history holds at most
    :data:`MAX_MESSAGES` (50) messages, and those are exactly the most recent
    ones ordered chronologically by ``at``. Messages are sorted by timestamp so
    the "50 most recent" guarantee holds even if messages arrive out of order.
    ``last_activity`` is advanced to the latest message timestamp.
    """
    ordered = tuple(sorted((*s.messages, m), key=lambda msg: msg.at))
    kept = ordered[-MAX_MESSAGES:]
    latest = kept[-1].at
    last_activity = latest if s.last_activity is None else max(s.last_activity, latest)
    return replace(s, messages=kept, last_activity=last_activity)


def is_active(s: Session, now: datetime, timeout_min: int) -> bool:
    """Return whether ``s`` is still active at ``now`` (Req 4.2, 4.3, Property 13).

    A session is active if and only if the time elapsed since its last activity
    is strictly less than the configured inactivity ``timeout_min`` (which must
    be within 5..120 minutes). A session without a recorded ``last_activity`` is
    treated as inactive.
    """
    _validate_timeout(timeout_min)
    if s.last_activity is None:
        return False
    elapsed = now - s.last_activity
    return elapsed < timedelta(minutes=timeout_min)


def expire_session(s: Session) -> Session:
    """Return an expired copy of ``s`` with its history removed (Req 4.3).

    Deterministic cleanup: the returned session is marked ``active=False`` and
    its message history is cleared, regardless of the previous cleanup state.
    """
    return replace(s, messages=(), active=False)


def resolve_session(
    sessions: list[Session],
    user_key: str,
    channel: str,
    now: datetime,
    timeout_min: int,
) -> Session | None:
    """Return the active session matching ``(user_key, channel)`` or ``None``.

    Scans ``sessions`` for one whose ``user_key`` and ``channel`` match and which
    :func:`is_active` at ``now`` (Req 4.4). Returns ``None`` when no active
    matching session exists, signalling that a new session must be created before
    processing the request (Req 4.5).
    """
    _validate_timeout(timeout_min)
    for s in sessions:
        if (
            s.user_key == user_key
            and s.channel == channel
            and is_active(s, now, timeout_min)
        ):
            return s
    return None


def resolve_or_create_session(
    sessions: list[Session],
    user_key: str,
    channel: str,
    now: datetime,
    timeout_min: int,
    *,
    session_id: str | None = None,
) -> SessionResolution:
    """Resolve an active session for the pair, or create a new one (Req 4.4-4.6).

    * If an active matching session exists, it is returned with ``created=False``.
    * Otherwise a new session is created and returned with ``created=True``.
    * If creation fails, a :class:`SessionResolution` with ``session=None`` and a
      populated ``error`` is returned, without persisting any partial data
      (Req 4.6). The caller decides whether to append the created session to its
      store, so a failure never leaves partial state behind.
    """
    existing = resolve_session(sessions, user_key, channel, now, timeout_min)
    if existing is not None:
        return SessionResolution(session=existing, created=False, error=None)

    try:
        created = create_session(user_key, channel, now, session_id=session_id)
    except SessionCreationError as exc:
        return SessionResolution(
            session=None,
            created=False,
            error=ErrorDetail(code=SESSION_CREATION_FAILED, message=str(exc)),
        )
    return SessionResolution(session=created, created=True, error=None)
