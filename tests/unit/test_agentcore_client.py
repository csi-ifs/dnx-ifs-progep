"""Testes do adapter que invoca o AgentCore Runtime (arquitetura única)."""

from __future__ import annotations

import io
import json

import pytest

from src.adapters.agentcore_client import AgentCoreRuntimeAdapter
from src.domain.config import AgentStatus
from src.domain.models import AgentRequest

pytestmark = pytest.mark.unit

ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/progep_ifs_agent-abc"


class _FakeClient:
    """Fake do cliente bedrock-agentcore com resposta configurável."""

    def __init__(self, body, *, as_stream=True):
        self._body = body
        self._as_stream = as_stream
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        raw = json.dumps(self._body).encode("utf-8")
        return {"response": io.BytesIO(raw) if self._as_stream else raw}


def _req(text="Como peço férias?"):
    return AgentRequest(session_id="s-1", channel="site", text=text)


def test_invoke_parses_answered_response_and_sources():
    client = _FakeClient({
        "status": "answered",
        "content": "As férias são solicitadas via SIGRH.",
        "sources": [{"doc_id": "s3://kb/a.pdf", "score": 0.83}],
        "confidence": 0.83,
    })
    adapter = AgentCoreRuntimeAdapter(ARN, client=client)

    resp = adapter.invoke(_req())

    assert resp.status == AgentStatus.ANSWERED.value
    assert resp.content == "As férias são solicitadas via SIGRH."
    assert [(s.doc_id, s.score) for s in resp.sources] == [("s3://kb/a.pdf", 0.83)]
    assert resp.confidence == 0.83
    # payload enviado corretamente ao runtime
    sent = client.calls[0]
    assert sent["agentRuntimeArn"] == ARN
    body = json.loads(sent["payload"].decode("utf-8"))
    assert body["text"] == "Como peço férias?"
    assert body["channel"] == "site"


def test_invoke_handles_not_found():
    client = _FakeClient({"status": "not_found", "content": "", "sources": [], "confidence": 0.0})
    adapter = AgentCoreRuntimeAdapter(ARN, client=client)
    resp = adapter.invoke(_req())
    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.sources == ()


def test_invoke_accepts_raw_bytes_body():
    client = _FakeClient(
        {"status": "answered", "content": "ok", "sources": [], "confidence": 0.5},
        as_stream=False,
    )
    adapter = AgentCoreRuntimeAdapter(ARN, client=client)
    resp = adapter.invoke(_req())
    assert resp.content == "ok"


def test_invoke_unreadable_response_is_unavailable():
    class _BadClient:
        def invoke_agent_runtime(self, **kwargs):
            return {"response": io.BytesIO(b"not json <<")}

    adapter = AgentCoreRuntimeAdapter(ARN, client=_BadClient())
    resp = adapter.invoke(_req())
    assert resp.status == AgentStatus.UNAVAILABLE.value


def test_constructor_rejects_empty_arn():
    with pytest.raises(ValueError):
        AgentCoreRuntimeAdapter("", client=_FakeClient({}))
