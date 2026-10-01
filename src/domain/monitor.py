"""Pure-logic consumption monitoring for the Monitor_de_Consumo.

The Monitor_de_Consumo records LLM call attempts and their outcomes so token
consumption and model responses can be tracked (Req 7.x). This module implements
the failure-recording branch of that responsibility as a side-effect-free
function over an immutable collection of records.

Failure recording (Req 7.4): when an LLM call fails *before completing*, the
Monitor_de_Consumo records the attempt with a failure status and the associated
``session_id``, **preserving the records already made** (Property 18). Rather than
mutating an existing collection, :func:`record_llm_failure` returns a brand new
immutable tuple with the previous records untouched (same objects, same order)
and the new failure record appended at the end.

This module is intentionally free of any I/O — it builds the in-memory record
collection but never emits metrics or calls CloudWatch/AgentCore Observability.
The actual metric emission, retries and error surfacing (Req 7.3, 7.5) live in
the Monitor adapter, which consumes the records produced here. Keeping the logic
pure makes it exercisable by property-based tests (Property 18).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CallStatus(str, Enum):
    """Outcome status of a recorded LLM call attempt (Req 7.1, 7.4)."""

    #: The LLM call completed successfully (tokens recorded, Req 7.1).
    SUCCESS = "success"
    #: The LLM call failed before completing (Req 7.4).
    FAILED = "failed"


@dataclass(frozen=True)
class CallRecord:
    """An immutable record of a single LLM call attempt (Req 7.1, 7.4).

    Attributes:
        session_id: Identifier of the session the call belongs to (Req 7.4).
        status: Outcome of the attempt — one of :class:`CallStatus` values.
        reason: Optional human-readable reason for a failure; ``None`` for a
            successful call or when no reason is supplied.
    """

    session_id: str
    status: str
    reason: str | None = None


def record_llm_failure(
    records: tuple[CallRecord, ...],
    session_id: str,
    *,
    reason: str | None = None,
) -> tuple[CallRecord, ...]:
    """Record a failed LLM call attempt, preserving prior records (Req 7.4).

    Appends a new :class:`CallRecord` with :attr:`CallStatus.FAILED` and the given
    ``session_id`` to ``records``, returning a **new** tuple. The previously
    recorded entries are preserved unchanged — same objects, same order — and the
    input tuple is never mutated (Property 18).

    Args:
        records: The records already made, in chronological order. An empty tuple
            represents no prior records.
        session_id: Identifier of the session whose LLM call failed. Must be a
            non-empty string.
        reason: Optional description of the failure to store on the record.

    Returns:
        A new tuple containing every record in ``records`` (unchanged, in order)
        followed by the newly created failure record.

    Raises:
        TypeError: If ``records`` is not a tuple or ``session_id`` is not a string.
        ValueError: If ``session_id`` is empty.
    """
    if not isinstance(records, tuple):
        raise TypeError(f"records must be a tuple, got {type(records).__name__}")
    if not isinstance(session_id, str):
        raise TypeError(
            f"session_id must be a str, got {type(session_id).__name__}"
        )
    if not session_id:
        raise ValueError("session_id must be a non-empty string")

    failure = CallRecord(
        session_id=session_id,
        status=CallStatus.FAILED.value,
        reason=reason,
    )
    return records + (failure,)
