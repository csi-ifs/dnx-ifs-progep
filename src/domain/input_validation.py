"""Input validation and content classification (pure logic).

This module implements Task 4.1 of the Assistente PROGEP-IFS spec:

* ``validate_message`` — per-channel message size validation (1..4000 for the
  site API, 1..4096 for WhatsApp). Out-of-range messages are rejected with a
  warning about the allowed limit and are NOT forwarded to the Agente_IA
  (Req 2.6, 5.3, 6.4).
* ``classify_content`` — text-only content classification. The Base_de_Conhecimento
  is consulted if and only if the request contains only text; any image,
  document or audio content interrupts processing with an "apenas texto" warning
  without consulting the KB. For the WhatsApp channel, the non-text media is
  discarded and not forwarded (Req 2.7, 2.8, 6.3).

All functions are pure and side-effect free, reusing the shared models in
``models.py`` and constants/enums in ``config.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from src.domain.config import Channel, channel_char_limit
from src.domain.models import ValidationResult

# --- Content part types ---------------------------------------------------------
#: The content part type representing plain text.
TEXT_PART: str = "text"
#: Non-text content part types that are not supported as input (Req 2.8, 6.3).
NON_TEXT_PARTS: frozenset[str] = frozenset({"image", "document", "audio"})


# --- User-facing warning messages -----------------------------------------------

def too_long_warning(channel: str) -> str:
    """Return the limit warning shown when a message exceeds the channel limit."""
    limit = channel_char_limit(channel)
    return (
        f"A mensagem excede o limite maximo de {limit} caracteres permitido "
        f"neste canal. Envie uma mensagem mais curta."
    )


#: Warning shown when the message body is empty (below the 1-char minimum).
EMPTY_MESSAGE_WARNING: str = (
    "A mensagem esta vazia. Envie ao menos 1 caractere de texto."
)

#: Warning shown when non-text content is present (Req 2.8, 6.3).
TEXT_ONLY_WARNING: str = (
    "Apenas mensagens de texto sao suportadas. Reenvie sua duvida como texto."
)


# --- Message size validation (Req 2.6, 5.3, 6.4) --------------------------------

def validate_message(text: str, channel: str) -> ValidationResult:
    """Validate a message body against the channel's size limits.

    A message is accepted if and only if its length is within the channel limit:
    1..4000 characters for the site API and 1..4096 for WhatsApp. Messages that
    are empty or exceed the limit are rejected with a non-empty ``reason`` that
    informs the user of the applicable limit, and are NOT forwarded to the
    Agente_IA (Req 2.6, 5.3, 6.4).

    Args:
        text: The raw message body sent by the user.
        channel: The originating channel (``"site"`` or ``"whatsapp"``).

    Returns:
        A :class:`ValidationResult`. When ``accepted`` is ``False`` the
        ``reason`` field contains the user-facing limit warning.

    Raises:
        ValueError: If ``channel`` is not a recognized channel.
    """
    limit = channel_char_limit(channel)  # raises ValueError for unknown channel
    length = len(text)

    if length < 1:
        return ValidationResult(accepted=False, reason=EMPTY_MESSAGE_WARNING)
    if length > limit:
        return ValidationResult(accepted=False, reason=too_long_warning(channel))
    return ValidationResult(accepted=True, reason=None)


# --- Content classification (Req 2.7, 2.8, 6.3) ---------------------------------

@dataclass(frozen=True)
class ContentClassification:
    """Result of classifying the content parts of an incoming request.

    Attributes:
        consult_kb: ``True`` if and only if the request contains only text, in
            which case the Base_de_Conhecimento may be consulted (Req 2.7).
        warning: The user-facing "apenas texto" warning when non-text content is
            present, otherwise ``None`` (Req 2.8, 6.3).
        discarded_media: The non-text part types that were discarded. Populated
            only for the WhatsApp channel, where media is dropped and not
            forwarded to the Agente_IA (Req 6.3). Empty for other channels.
    """

    consult_kb: bool
    warning: str | None = None
    discarded_media: tuple[str, ...] = field(default_factory=tuple)


def classify_content(part_types: Iterable[str], channel: str) -> ContentClassification:
    """Classify a request's content parts as text-only or containing media.

    The Base_de_Conhecimento is consulted if and only if every content part is
    text. If any image, document or audio part is present, processing is
    interrupted with a text-only warning and the KB is NOT consulted. For the
    WhatsApp channel, the non-text media is reported as discarded so the
    connector can drop it without forwarding (Req 2.7, 2.8, 6.3).

    Args:
        part_types: The content part types of the incoming request (e.g.
            ``["text"]`` or ``["text", "image"]``).
        channel: The originating channel (``"site"`` or ``"whatsapp"``).

    Returns:
        A :class:`ContentClassification` describing whether the KB should be
        consulted, any warning to show the user, and media discarded on WhatsApp.

    Raises:
        ValueError: If ``channel`` is not a recognized channel.
    """
    # Validate the channel up front for consistency with validate_message.
    channel_char_limit(channel)  # raises ValueError for unknown channel

    parts = tuple(part_types)
    non_text = tuple(p for p in parts if p != TEXT_PART)

    if not non_text:
        return ContentClassification(consult_kb=True, warning=None, discarded_media=())

    discarded: tuple[str, ...] = ()
    if channel == Channel.WHATSAPP.value:
        discarded = non_text

    return ContentClassification(
        consult_kb=False,
        warning=TEXT_ONLY_WARNING,
        discarded_media=discarded,
    )
