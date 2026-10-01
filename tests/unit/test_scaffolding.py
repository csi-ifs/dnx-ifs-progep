"""Smoke tests validating the project scaffolding, shared domain models and
configuration constants defined in Task 1.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src import domain
from src.domain import config, models


pytestmark = pytest.mark.unit


def test_config_constants_match_design():
    assert config.MAX_MESSAGES == 50
    assert config.MAX_CHARS_API == 4000
    assert config.MAX_CHARS_WHATSAPP == 4096
    assert config.MIN_TIMEOUT_MIN == 5
    assert config.MAX_TIMEOUT_MIN == 120
    assert config.DEFAULT_TIMEOUT_MIN == 30
    assert config.MAX_RETRIES == 3
    assert config.MAX_DOC_SIZE_BYTES == 52_428_800
    assert config.APN_TAG == {"aws-apn-id": "pc:7b0pljcsg0gii1a5gn5vfca0v"}
    assert config.USER_AGENT_APN == "APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$"


def test_channel_char_limit():
    assert config.channel_char_limit("site") == 4000
    assert config.channel_char_limit("whatsapp") == 4096
    with pytest.raises(ValueError):
        config.channel_char_limit("unknown")


def test_enums_values():
    assert config.Channel.SITE.value == "site"
    assert config.Channel.WHATSAPP.value == "whatsapp"
    assert config.SUPPORTED_CONTENT_TYPES == {"pdf", "docx", "txt", "html", "md"}
    assert config.RoutingDecision.ESCALATE.value == "escalate"


def test_models_are_frozen():
    doc = models.DocumentCandidate(
        doc_id="d1", source="site", filename="a.pdf",
        content_type="pdf", size_bytes=10,
    )
    with pytest.raises(Exception):
        doc.doc_id = "d2"  # type: ignore[misc]


def test_shared_models_instantiate():
    now = datetime(2025, 1, 1, 12, 0, 0)
    vr = models.ValidationResult(accepted=False, reason="too big")
    assert vr.accepted is False and vr.reason == "too big"

    rec = models.IngestionRecord(
        doc_id="d1", source="site", indexed_at=now, status="indexed",
    )
    assert rec.reason is None

    req = models.AgentRequest(session_id="s1", channel="site", text="oi")
    resp = models.AgentResponse(
        status="answered", content="ok",
        sources=(models.SourceRef(doc_id="d1", score=0.9),), confidence=0.8,
    )
    assert req.text == "oi" and resp.sources[0].doc_id == "d1"

    msg = models.Message(role="user", text="oi", at=now)
    sess = models.Session(
        session_id="s1", user_key="u1", channel="site",
        messages=(msg,), last_activity=now, active=True,
    )
    assert sess.messages[0].text == "oi"

    ok = models.ApiResponse(status="ok", content="pronto")
    err = models.ApiResponse(
        status="error",
        error=models.ErrorDetail(code="unauthenticated", message="falha"),
    )
    assert ok.content == "pronto" and err.error is not None


def test_package_exports_all_symbols():
    for name in (
        "DocumentCandidate", "ValidationResult", "IngestionRecord",
        "AgentRequest", "AgentResponse", "SourceRef", "Message", "Session",
        "ApiResponse", "ErrorDetail", "MAX_MESSAGES", "APN_TAG",
        "USER_AGENT_APN",
    ):
        assert hasattr(domain, name), f"missing export: {name}"
