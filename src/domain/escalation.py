"""Pure-logic escalation payload assembly for the Roteador_de_Atendimento.

When an interaction is escalated to an Atendente_Humano, the full history of the
current session must be transferred to Amazon Connect (Req 3.3). This module
builds that transfer payload as a side-effect-free function over the immutable
:class:`~src.domain.models.Session` type.

Invariant (Property 8): the payload contains *all* messages of the current
session history, in the *same order* as they appear in the session. The order is
preserved exactly — the messages are copied verbatim, not re-sorted — so the
Atendente_Humano receives the conversation as it unfolded.

This module is intentionally free of any I/O so it can be exercised by
property-based tests. The actual call to Amazon Connect (including retries and
queueing) lives in a separate adapter.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.models import Message, Session


@dataclass(frozen=True)
class EscalationPayload:
    """The context transferred to Amazon Connect when escalating a session.

    Carries the full session history so the Atendente_Humano has the complete
    conversation context (Req 3.3).

    Attributes:
        session_id: Identifier of the escalated session.
        user_key: Identification of the Usuario on the originating channel.
        channel: Originating channel (``"site"`` | ``"whatsapp"``).
        messages: The complete session history, in the same order as recorded on
            the session (Property 8).
    """

    session_id: str
    user_key: str
    channel: str
    messages: tuple[Message, ...]


def build_escalation_payload(session: Session) -> EscalationPayload:
    """Build the Amazon Connect escalation payload for ``session`` (Req 3.3).

    The resulting payload includes every message of the current session history,
    in the same order they appear on the session, so the complete conversation is
    transferred to the Atendente_Humano (Property 8). The messages are copied
    verbatim without reordering, filtering or truncation.

    Args:
        session: The current session being escalated to a human attendant.

    Returns:
        An :class:`EscalationPayload` carrying the session identity and the full,
        order-preserving message history.
    """
    return EscalationPayload(
        session_id=session.session_id,
        user_key=session.user_key,
        channel=session.channel,
        messages=tuple(session.messages),
    )
