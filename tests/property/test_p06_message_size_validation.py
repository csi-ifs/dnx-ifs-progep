"""Property-based test for per-channel message size validation.

Feature: assistente-progep-ifs, Property 6: Validação de tamanho de mensagem por
canal.

For any texto de entrada, a mensagem é aceita **se e somente se** seu comprimento
está dentro do limite do canal (1..4000 para o site, 1..4096 para o WhatsApp);
mensagens fora do intervalo são rejeitadas com um aviso do limite e sem
consultar/encaminhar (a camada pura nunca encaminha — o chamador só prossegue
quando ``accepted`` é True).

Validates: Requirements 2.6, 5.3, 6.4
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import (
    MAX_CHARS_API,
    MAX_CHARS_WHATSAPP,
    MIN_CHARS,
    Channel,
    channel_char_limit,
)
from src.domain.input_validation import validate_message

pytestmark = pytest.mark.property


# --- Generators -----------------------------------------------------------------

# Both supported channels.
_channels = st.sampled_from([Channel.SITE.value, Channel.WHATSAPP.value])

# The largest per-channel limit; used to bound the "broad" length range so the
# generated space always straddles both channel boundaries.
_MAX_LIMIT = max(MAX_CHARS_API, MAX_CHARS_WHATSAPP)

# Explicit lengths that MUST be covered: empty (0), the minimum (1), and the
# exact boundary / just-above boundary for each channel.
_explicit_lengths = st.sampled_from(
    [
        0,
        1,
        MIN_CHARS,
        MAX_CHARS_API - 1,
        MAX_CHARS_API,  # site boundary: accepted on site
        MAX_CHARS_API + 1,  # just above site limit: rejected on site
        MAX_CHARS_WHATSAPP - 1,
        MAX_CHARS_WHATSAPP,  # whatsapp boundary: accepted on whatsapp
        MAX_CHARS_WHATSAPP + 1,  # just above whatsapp limit: rejected everywhere
    ]
)


def _lengths() -> st.SearchStrategy[int]:
    """Lengths covering zero, boundaries and above, with a broad middle range.

    Uses integer lengths (not full strings) so the generator stays cheap; the
    actual message is materialised lazily in the test with the chosen length.
    """
    around_site = st.integers(
        min_value=MAX_CHARS_API - 3, max_value=MAX_CHARS_API + 3
    )
    around_whatsapp = st.integers(
        min_value=MAX_CHARS_WHATSAPP - 3, max_value=MAX_CHARS_WHATSAPP + 3
    )
    broad = st.integers(min_value=0, max_value=_MAX_LIMIT + 50)
    return st.one_of(_explicit_lengths, around_site, around_whatsapp, broad)


# --- Property -------------------------------------------------------------------

@given(channel=_channels, length=_lengths(), fill=st.characters())
def test_property_6_message_size_validation_iff(
    channel: str, length: int, fill: str
) -> None:
    """Acceptance holds iff 1 <= len(text) <= channel limit.

    Feature: assistente-progep-ifs, Property 6: Validação de tamanho de mensagem
    por canal. Validates: Requirements 2.6, 5.3, 6.4.
    """
    text = fill * length
    assert len(text) == length  # sanity: single-codepoint fill => length chars

    result = validate_message(text, channel)

    # Reference acceptance condition computed independently of the implementation.
    limit = channel_char_limit(channel)
    expected_accepted = MIN_CHARS <= length <= limit

    # If and only if: acceptance matches the reference condition exactly.
    assert result.accepted is expected_accepted

    if result.accepted:
        # Accepted messages carry no rejection reason.
        assert result.reason is None
    else:
        # Every rejection produces a non-empty reason informing of the limit.
        assert result.reason is not None
        assert result.reason.strip() != ""
        # Over-limit rejections mention the applicable channel limit.
        if length > limit:
            assert str(limit) in result.reason


def test_property_6_site_and_whatsapp_differ_at_intermediate_length() -> None:
    """A length between the site and WhatsApp limits is rejected on the site
    but accepted on WhatsApp, confirming the limit is per-channel.

    Feature: assistente-progep-ifs, Property 6: Validação de tamanho de mensagem
    por canal. Validates: Requirements 2.6, 5.3, 6.4.
    """
    length = MAX_CHARS_API + 1  # 4001: above site limit, within WhatsApp limit
    text = "a" * length
    assert validate_message(text, Channel.SITE.value).accepted is False
    assert validate_message(text, Channel.WHATSAPP.value).accepted is True
