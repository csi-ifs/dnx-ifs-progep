"""Unit tests for the S3 -> Ingestor -> Knowledge Base ingestion flow (Task 14.3).

These tests inject a mock ``KnowledgeBaseAdapter`` so no real AWS call is made.
They cover:

* Parsing S3 event records (URL-decoding keys, filtering non-created events).
* Deriving ``source``/``content_type`` from the object key (Req 1.5, 1.1).
* Triggering the ingestion job for valid documents (Req 1.1) and the incremental
  reindexation path for updates (Req 1.7).
* Rejecting invalid documents without triggering any job (Req 1.3).
* Preserving Knowledge Base state and recording failure with the ``doc_id`` when
  the adapter fails (Req 1.4).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters.knowledge import IngestionJob, KnowledgeBaseAdapter
from src.flows import ingestion_flow
from src.flows.ingestion_flow import (
    S3ObjectRef,
    build_candidate_from_object,
    build_client_token,
    content_type_from_key,
    handle_s3_event,
    is_update_event,
    lambda_handler,
    parse_s3_event,
    process_object,
    source_from_key,
)


def _mock_adapter(*, started: bool = True) -> MagicMock:
    """Build a mock adapter whose job triggers return a configurable outcome."""
    adapter = MagicMock(spec=KnowledgeBaseAdapter)
    adapter.knowledge_base_id = "kb-123"
    adapter.data_source_id = "ds-456"
    job = IngestionJob(
        knowledge_base_id="kb-123",
        data_source_id="ds-456",
        started=started,
        job_id="job-1" if started else None,
        status="STARTING" if started else "FAILED",
        reason=None if started else "throttled",
    )
    adapter.start_ingestion_job.return_value = job
    adapter.reindex.return_value = job
    adapter.ensure_ingestion_job.return_value = job
    return adapter


def _s3_event(key: str, *, size: int = 1024, event_name: str = "ObjectCreated:Put",
              bucket: str = "progep-kb-bucket") -> dict:
    return {
        "Records": [
            {
                "eventName": event_name,
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": size},
                },
            }
        ]
    }


# --- Key parsing helpers -------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "key,expected",
    [
        ("site/edital.pdf", "pdf"),
        ("cliente/manual.DOCX", "docx"),
        ("site/nota.txt", "txt"),
        ("site/pagina.html", "html"),
        ("site/pagina.htm", "html"),
        ("site/imagem.png", "png"),
        ("site/semext", ""),
    ],
)
def test_content_type_from_key(key, expected):
    assert content_type_from_key(key) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "key,expected",
    [
        ("site/edital.pdf", "site"),
        ("cliente/doc.pdf", "cliente"),
        ("outra/doc.pdf", "outra"),
        ("doc.pdf", "desconhecida"),
    ],
)
def test_source_from_key(key, expected):
    assert source_from_key(key) == expected


@pytest.mark.unit
def test_build_candidate_from_object_uses_key_as_doc_id():
    ref = S3ObjectRef(bucket="b", key="cliente/edital 2025.pdf", size_bytes=2048)
    candidate = build_candidate_from_object(ref)

    assert candidate.doc_id == "cliente/edital 2025.pdf"
    assert candidate.source == "cliente"
    assert candidate.filename == "edital 2025.pdf"
    assert candidate.content_type == "pdf"
    assert candidate.size_bytes == 2048


# --- Event parsing -------------------------------------------------------------

@pytest.mark.unit
def test_parse_s3_event_url_decodes_key():
    refs = parse_s3_event(_s3_event("site/edital+2025%20final.pdf"))

    assert len(refs) == 1
    assert refs[0].key == "site/edital 2025 final.pdf"
    assert refs[0].bucket == "progep-kb-bucket"
    assert refs[0].size_bytes == 1024


@pytest.mark.unit
def test_parse_s3_event_ignores_non_created_events():
    refs = parse_s3_event(_s3_event("site/x.pdf", event_name="ObjectRemoved:Delete"))
    assert refs == []


@pytest.mark.unit
def test_parse_s3_event_skips_records_without_bucket_or_key():
    event = {"Records": [{"eventName": "ObjectCreated:Put", "s3": {"object": {}}}]}
    assert parse_s3_event(event) == []




@pytest.mark.unit
@pytest.mark.parametrize(
    "event_name,expected",
    [
        ("ObjectCreated:Put", False),
        ("ObjectCreated:Post", False),
        ("ObjectCreated:Copy", True),
        ("ObjectCreated:CompleteMultipartUpload", True),
    ],
)
def test_is_update_event(event_name, expected):
    assert is_update_event(event_name) is expected


# --- process_object ------------------------------------------------------------

@pytest.mark.unit
def test_process_object_valid_triggers_ingestion_job():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="b", key="site/edital.pdf", size_bytes=1024,
                      event_name="ObjectCreated:Put")

    record = process_object(adapter, ref)

    assert record.status == "indexed"
    assert record.doc_id == "site/edital.pdf"
    assert record.source == "site"
    adapter.ensure_ingestion_job.assert_called_once()
    _, kwargs = adapter.ensure_ingestion_job.call_args
    assert kwargs["description"].startswith("ingest ")


@pytest.mark.unit
def test_process_object_update_event_uses_reindex():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="b", key="site/edital.pdf", size_bytes=1024,
                      event_name="ObjectCreated:Copy")

    record = process_object(adapter, ref)

    assert record.status == "indexed"
    adapter.ensure_ingestion_job.assert_called_once()
    _, kwargs = adapter.ensure_ingestion_job.call_args
    assert kwargs["description"].startswith("reindex ")


@pytest.mark.unit
def test_process_object_reindex_override_forces_reindex():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="b", key="site/edital.pdf", size_bytes=1024,
                      event_name="ObjectCreated:Put")

    process_object(adapter, ref, reindex=True)

    adapter.ensure_ingestion_job.assert_called_once()
    _, kwargs = adapter.ensure_ingestion_job.call_args
    assert kwargs["description"].startswith("reindex ")


@pytest.mark.unit
def test_process_object_rejects_invalid_format_without_job():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="b", key="site/video.mp4", size_bytes=1024,
                      event_name="ObjectCreated:Put")

    record = process_object(adapter, ref)

    assert record.status == "rejected"
    assert record.reason  # non-empty
    adapter.ensure_ingestion_job.assert_not_called()


@pytest.mark.unit
def test_process_object_rejects_oversized_without_job():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="b", key="site/big.pdf", size_bytes=52_428_801,
                      event_name="ObjectCreated:Put")

    record = process_object(adapter, ref)

    assert record.status == "rejected"
    adapter.ensure_ingestion_job.assert_not_called()


@pytest.mark.unit
def test_process_object_failure_records_failed_with_doc_id():
    adapter = _mock_adapter(started=False)
    ref = S3ObjectRef(bucket="b", key="cliente/edital.pdf", size_bytes=1024,
                      event_name="ObjectCreated:Put")
    when = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    record = process_object(adapter, ref, indexed_at=when)

    assert record.status == "failed"
    assert record.doc_id == "cliente/edital.pdf"
    assert record.reason and "throttled" in record.reason
    assert record.indexed_at == when


@pytest.mark.unit
def test_process_object_passes_idempotency_token():
    adapter = _mock_adapter()
    ref = S3ObjectRef(bucket="bkt", key="site/edital.pdf", size_bytes=1024,
                      event_name="ObjectCreated:Put")

    process_object(adapter, ref)

    _, kwargs = adapter.ensure_ingestion_job.call_args
    assert kwargs["client_token"] == build_client_token("bkt", "site/edital.pdf")


#: Formato exigido pelo Bedrock para o ``clientToken`` do StartIngestionJob.
CLIENT_TOKEN_RE = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}$")


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "site/edital.pdf",
        "site/images/DADOS_ABERTOS/PDA_2022-2024.pdf",
        "site/novos-projetos-e-programas-em-gestao-de-pessoas.html",
        "site/images/Instrução_Normativa_nº_04-2023-PROGEP.pdf",
        "a" * 400,
    ],
)
def test_client_token_matches_bedrock_pattern(key):
    """Chaves reais têm ``/``, ``.``, ``_`` e acentos — o token não pode tê-los."""
    assert CLIENT_TOKEN_RE.match(build_client_token("progep-ifs-documents", key))


@pytest.mark.unit
def test_client_token_is_stable_and_distinct_per_object():
    assert build_client_token("b", "k") == build_client_token("b", "k")
    assert build_client_token("b", "k1") != build_client_token("b", "k2")
    assert build_client_token("b1", "k") != build_client_token("b2", "k")


# --- handle_s3_event -----------------------------------------------------------

@pytest.mark.unit
def test_handle_s3_event_processes_all_records():
    adapter = _mock_adapter()
    event = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": "b"},
                       "object": {"key": "site/a.pdf", "size": 10}},
            },
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": "b"},
                       "object": {"key": "cliente/b.txt", "size": 20}},
            },
        ]
    }

    records = handle_s3_event(event, adapter)

    assert [r.doc_id for r in records] == ["site/a.pdf", "cliente/b.txt"]
    assert all(r.status == "indexed" for r in records)
    assert adapter.ensure_ingestion_job.call_count == 2


@pytest.mark.unit
def test_handle_s3_event_empty_returns_empty():
    adapter = _mock_adapter()
    assert handle_s3_event({"Records": []}, adapter) == []
    adapter.ensure_ingestion_job.assert_not_called()


# --- lambda_handler ------------------------------------------------------------

@pytest.mark.unit
def test_lambda_handler_builds_adapter_from_env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-1")
    monkeypatch.setenv("DATA_SOURCE_ID", "ds-1")
    adapter = _mock_adapter()
    monkeypatch.setattr(ingestion_flow, "_adapter_from_env", lambda: adapter)

    result = lambda_handler(_s3_event("site/edital.pdf"))

    assert result["processed"] == 1
    assert result["records"][0]["status"] == "indexed"
    assert result["records"][0]["doc_id"] == "site/edital.pdf"


@pytest.mark.unit
def test_lambda_handler_missing_env_raises(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)
    monkeypatch.delenv("DATA_SOURCE_ID", raising=False)

    with pytest.raises(RuntimeError):
        lambda_handler(_s3_event("site/edital.pdf"))
