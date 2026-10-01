"""Unit tests for per-channel message validation and content classification
(Task 4.1 — Req 2.6, 2.7, 2.8, 5.3, 6.3, 6.4).
"""

from __future__ import annotations

import pytest

from src.domain import config
from src.domain.input_validation import (
    TEXT_ONLY_WARNING,
    ContentClassification,
    classify_content,
    validate_message,
)

pytestmark = pytest.mark.unit


# --- validate_message -----------------------------------------------------------

def test_site_accepts_within_limit():
    result = validate_message("olá", "site")
    assert result.accepted is True
    assert result.reason is None


def test_site_accepts_exactly_at_limit():
    result = validate_message("a" * config.MAX_CHARS_API, "site")
    assert result.accepted is True


def test_site_rejects_over_limit_with_warning():
    result = validate_message("a" * (config.MAX_CHARS_API + 1), "site")
    assert result.accepted is False
    assert result.reason and str(config.MAX_CHARS_API) in result.reason


def test_whatsapp_accepts_exactly_at_limit():
    result = validate_message("a" * config.MAX_CHARS_WHATSAPP, "whatsapp")
    assert result.accepted is True


def test_whatsapp_rejects_over_limit_with_warning():
    result = validate_message("a" * (config.MAX_CHARS_WHATSAPP + 1), "whatsapp")
    assert result.accepted is False
    assert result.reason and str(config.MAX_CHARS_WHATSAPP) in result.reason


def test_site_allows_length_between_api_and_whatsapp_limits_on_whatsapp():
    length = config.MAX_CHARS_API + 10  # 4010: rejected on site, accepted on WhatsApp
    assert validate_message("a" * length, "site").accepted is False
    assert validate_message("a" * length, "whatsapp").accepted is True


def test_empty_message_rejected_with_reason():
    result = validate_message("", "site")
    assert result.accepted is False
    assert result.reason  # non-empty reason


def test_unknown_channel_raises():
    with pytest.raises(ValueError):
        validate_message("hi", "sms")


# --- classify_content -----------------------------------------------------------

def test_text_only_consults_kb():
    result = classify_content(["text"], "site")
    assert result == ContentClassification(
        consult_kb=True, warning=None, discarded_media=()
    )


def test_empty_parts_treated_as_text_only():
    result = classify_content([], "site")
    assert result.consult_kb is True
    assert result.warning is None


@pytest.mark.parametrize("media", ["image", "document", "audio"])
def test_site_media_interrupts_without_consulting_kb(media):
    result = classify_content(["text", media], "site")
    assert result.consult_kb is False
    assert result.warning == TEXT_ONLY_WARNING
    # Site does not report discarded media (only WhatsApp discards).
    assert result.discarded_media == ()


@pytest.mark.parametrize("media", ["image", "document", "audio"])
def test_whatsapp_media_is_discarded_and_not_forwarded(media):
    result = classify_content(["text", media], "whatsapp")
    assert result.consult_kb is False
    assert result.warning == TEXT_ONLY_WARNING
    assert result.discarded_media == (media,)


def test_whatsapp_multiple_media_all_discarded():
    result = classify_content(["image", "audio"], "whatsapp")
    assert result.consult_kb is False
    assert result.discarded_media == ("image", "audio")


def test_classify_unknown_channel_raises():
    with pytest.raises(ValueError):
        classify_content(["text"], "sms")
