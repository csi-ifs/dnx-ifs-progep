"""Property-based test for text-only content classification.

Feature: assistente-progep-ifs, Property 5: Classificação de conteúdo somente-texto

For any requisição de qualquer canal, o processamento consulta a
Base_de_Conhecimento se e somente se a requisição contém apenas texto;
requisições com conteúdo de imagem, documento ou áudio são interrompidas com
aviso de "apenas texto" e sem consulta à KB (para WhatsApp, a mídia é descartada
e não encaminhada).

Validates: Requirements 2.7, 2.8, 6.3
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.config import Channel
from src.domain.input_validation import (
    NON_TEXT_PARTS,
    TEXT_ONLY_WARNING,
    TEXT_PART,
    classify_content,
)

pytestmark = pytest.mark.property

# Channels under test.
_CHANNELS = [c.value for c in Channel]  # ["site", "whatsapp"]

# A single content part: either "text" or one of the non-text media types.
_part_strategy = st.sampled_from([TEXT_PART, *sorted(NON_TEXT_PARTS)])
# Arbitrary lists of content parts (may be empty, text-only, or mixed).
_parts_strategy = st.lists(_part_strategy, min_size=0, max_size=8)


@given(part_types=_parts_strategy, channel=st.sampled_from(_CHANNELS))
def test_kb_consulted_iff_text_only(part_types: list[str], channel: str) -> None:
    """consult_kb is True if and only if the request contains only text.

    Validates: Requirements 2.7, 2.8, 6.3
    """
    result = classify_content(part_types, channel)

    is_text_only = all(p == TEXT_PART for p in part_types)
    # Biconditional: KB is consulted exactly when the content is text-only.
    assert result.consult_kb is is_text_only

    if is_text_only:
        # Text-only: no warning, nothing discarded, on either channel.
        assert result.warning is None
        assert result.discarded_media == ()
    else:
        # Media present: interrupted with the text-only warning, no KB consult.
        assert result.warning == TEXT_ONLY_WARNING
        assert result.consult_kb is False

        non_text = tuple(p for p in part_types if p != TEXT_PART)
        if channel == Channel.WHATSAPP.value:
            # WhatsApp discards exactly the non-text media, in order.
            assert result.discarded_media == non_text
        else:
            # Other channels do not report discarded media.
            assert result.discarded_media == ()
