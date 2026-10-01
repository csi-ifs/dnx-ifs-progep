"""Unit tests for the knowledge-layer adapters (Task 12.1).

These tests inject mock boto3 clients so no real AWS call is made. They cover:

* Injection of the ``User_Agent_APN`` billing annotation into the boto3 client
  config / ``AWS_SDK_UA_APP_ID`` env var (Req 8.3).
* Triggering the Knowledge Base ingestion job and incremental reindexation
  (Req 1.1, 1.7).
* Failure handling that records the error with the ``doc_id`` while preserving
  the previous state of the Knowledge Base (Req 1.4).
* Rejection of invalid documents without starting an ingestion job (Req 1.3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.adapters import knowledge
from src.adapters.knowledge import (
    IngestionJob,
    KnowledgeBaseAdapter,
    apn_boto_config,
    make_client,
)
from src.domain.config import USER_AGENT_APN
from src.domain.models import DocumentCandidate


def _aws_error(message: str) -> Exception:
    """Build a realistic AWS error caught by the adapter's failure path.

    Uses ``ClientError`` when botocore is available, otherwise falls back to the
    generic ``Exception`` the guarded import binds when boto3 is absent.
    """
    if knowledge.boto3_available():
        from botocore.exceptions import ClientError

        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": message}},
            "StartIngestionJob",
        )
    return Exception(message)


def _doc(**overrides) -> DocumentCandidate:
    base = dict(
        doc_id="doc-1",
        source="site",
        filename="edital.pdf",
        content_type="pdf",
        size_bytes=1024,
    )
    base.update(overrides)
    return DocumentCandidate(**base)


def _adapter(agent=None, s3=None) -> KnowledgeBaseAdapter:
    return KnowledgeBaseAdapter(
        knowledge_base_id="kb-123",
        data_source_id="ds-456",
        bucket="progep-kb-bucket",
        bedrock_agent_client=agent if agent is not None else MagicMock(),
        s3_client=s3 if s3 is not None else MagicMock(),
    )


# --- Billing annotation (Req 8.3) ----------------------------------------------

@pytest.mark.unit
def test_apn_boto_config_injects_user_agent_and_env(monkeypatch):
    if not knowledge.boto3_available():
        pytest.skip("boto3 not available in this environment")
    monkeypatch.delenv("AWS_SDK_UA_APP_ID", raising=False)

    config = apn_boto_config()

    assert config.user_agent_extra == USER_AGENT_APN
    import os

    assert os.environ["AWS_SDK_UA_APP_ID"] == USER_AGENT_APN


@pytest.mark.unit
def test_make_client_uses_apn_config(monkeypatch):
    if not knowledge.boto3_available():
        pytest.skip("boto3 not available in this environment")

    captured = {}

    def fake_client(service_name, region_name=None, config=None, **kwargs):
        captured["service_name"] = service_name
        captured["config"] = config
        return MagicMock()

    monkeypatch.setattr(knowledge.boto3, "client", fake_client)

    make_client("bedrock-agent", region_name="us-east-1")

    assert captured["service_name"] == "bedrock-agent"
    assert captured["config"].user_agent_extra == USER_AGENT_APN


@pytest.mark.unit
def test_lazy_bedrock_agent_client_created_with_apn_config(monkeypatch):
    if not knowledge.boto3_available():
        pytest.skip("boto3 not available in this environment")

    captured = {}

    def fake_client(service_name, region_name=None, config=None, **kwargs):
        captured[service_name] = config
        return MagicMock()

    monkeypatch.setattr(knowledge.boto3, "client", fake_client)

    adapter = KnowledgeBaseAdapter(
        knowledge_base_id="kb-1", data_source_id="ds-1"
    )
    _ = adapter.bedrock_agent  # trigger lazy creation

    assert captured["bedrock-agent"].user_agent_extra == USER_AGENT_APN


# --- Ingestion job trigger (Req 1.1) -------------------------------------------

@pytest.mark.unit
def test_start_ingestion_job_success():
    agent = MagicMock()
    agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-1", "status": "STARTING"}
    }
    adapter = _adapter(agent=agent)

    job = adapter.start_ingestion_job()

    agent.start_ingestion_job.assert_called_once_with(
        knowledgeBaseId="kb-123", dataSourceId="ds-456"
    )
    assert job == IngestionJob(
        knowledge_base_id="kb-123",
        data_source_id="ds-456",
        started=True,
        job_id="job-1",
        status="STARTING",
    )


@pytest.mark.unit
def test_start_ingestion_job_forwards_optional_params():
    agent = MagicMock()
    agent.start_ingestion_job.return_value = {"ingestionJob": {}}
    adapter = _adapter(agent=agent)

    adapter.start_ingestion_job(client_token="tok-1", description="manual sync")

    agent.start_ingestion_job.assert_called_once_with(
        knowledgeBaseId="kb-123",
        dataSourceId="ds-456",
        clientToken="tok-1",
        description="manual sync",
    )


# --- Incremental reindexation (Req 1.7) ----------------------------------------

@pytest.mark.unit
def test_reindex_triggers_incremental_ingestion_job():
    agent = MagicMock()
    agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-9", "status": "STARTING"}
    }
    adapter = _adapter(agent=agent)

    job = adapter.reindex()

    assert job.started is True
    assert job.job_id == "job-9"
    _, kwargs = agent.start_ingestion_job.call_args
    assert kwargs["description"] == "incremental reindex"


# --- Failure preserves previous KB state (Req 1.4) -----------------------------

@pytest.mark.unit
def test_start_ingestion_job_failure_returns_not_started():
    agent = MagicMock()
    agent.start_ingestion_job.side_effect = _aws_error("boom")
    adapter = _adapter(agent=agent)

    job = adapter.start_ingestion_job()

    assert job.started is False
    assert job.status == "FAILED"
    assert job.reason and "boom" in job.reason
    assert job.job_id is None


@pytest.mark.unit
def test_ingest_document_failure_records_error_with_doc_id():
    agent = MagicMock()
    agent.start_ingestion_job.side_effect = _aws_error("kaboom")
    adapter = _adapter(agent=agent)
    when = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    record = adapter.ingest_document(_doc(doc_id="doc-42"), indexed_at=when)

    assert record.doc_id == "doc-42"
    assert record.status == "failed"
    assert record.reason and "kaboom" in record.reason
    assert record.indexed_at == when


@pytest.mark.unit
def test_ingest_document_success_records_indexed():
    agent = MagicMock()
    agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-1", "status": "STARTING"}
    }
    adapter = _adapter(agent=agent)

    record = adapter.ingest_document(_doc())

    assert record.status == "indexed"
    assert record.doc_id == "doc-1"
    assert record.reason is None
    agent.start_ingestion_job.assert_called_once()


@pytest.mark.unit
def test_ingest_document_rejects_invalid_without_starting_job():
    agent = MagicMock()
    adapter = _adapter(agent=agent)

    record = adapter.ingest_document(_doc(content_type="exe", size_bytes=10))

    assert record.status == "rejected"
    assert record.reason  # non-empty rejection reason
    agent.start_ingestion_job.assert_not_called()


@pytest.mark.unit
def test_ingest_document_rejects_oversized_without_starting_job():
    agent = MagicMock()
    adapter = _adapter(agent=agent)

    record = adapter.ingest_document(_doc(size_bytes=52_428_801))

    assert record.status == "rejected"
    agent.start_ingestion_job.assert_not_called()


# --- S3 upload -----------------------------------------------------------------

@pytest.mark.unit
def test_upload_document_puts_object_with_content_type():
    s3 = MagicMock()
    adapter = _adapter(s3=s3)

    adapter.upload_document("site/edital.pdf", b"bytes", content_type="application/pdf")

    s3.put_object.assert_called_once_with(
        Bucket="progep-kb-bucket",
        Key="site/edital.pdf",
        Body=b"bytes",
        ContentType="application/pdf",
    )


@pytest.mark.unit
def test_upload_document_without_bucket_raises():
    adapter = KnowledgeBaseAdapter(
        knowledge_base_id="kb-1",
        data_source_id="ds-1",
        s3_client=MagicMock(),
    )
    with pytest.raises(ValueError):
        adapter.upload_document("k", b"b")


# --- Constructor validation ----------------------------------------------------

@pytest.mark.unit
def test_constructor_rejects_empty_ids():
    with pytest.raises(ValueError):
        KnowledgeBaseAdapter(knowledge_base_id="", data_source_id="ds")
    with pytest.raises(ValueError):
        KnowledgeBaseAdapter(knowledge_base_id="kb", data_source_id="  ")


@pytest.mark.unit
def test_get_ingestion_job_returns_payload():
    agent = MagicMock()
    agent.get_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-1", "status": "COMPLETE"}
    }
    adapter = _adapter(agent=agent)

    payload = adapter.get_ingestion_job("job-1")

    assert payload["status"] == "COMPLETE"
    agent.get_ingestion_job.assert_called_once_with(
        knowledgeBaseId="kb-123", dataSourceId="ds-456", ingestionJobId="job-1"
    )


# --- Debounce / coalescência de ingestion jobs (ensure_ingestion_job) ----------

@pytest.mark.unit
def test_client_error_code_extracts_code_and_handles_non_client_errors():
    assert knowledge.client_error_code(_aws_error("x")) in {"ThrottlingException", None}
    assert knowledge.client_error_code(Exception("sem response")) is None


@pytest.mark.unit
def test_ensure_ingestion_job_coalesces_when_job_already_running():
    agent = MagicMock()
    agent.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [
            {"ingestionJobId": "job-run", "status": "IN_PROGRESS"},
            {"ingestionJobId": "job-old", "status": "COMPLETE"},
        ]
    }
    adapter = _adapter(agent=agent)

    job = adapter.ensure_ingestion_job(description="ingest site/a.html")

    assert job.started is True
    assert job.status == knowledge.COALESCED_STATUS
    assert job.job_id == "job-run"
    agent.start_ingestion_job.assert_not_called()


@pytest.mark.unit
def test_ensure_ingestion_job_starts_when_no_active_job():
    agent = MagicMock()
    agent.list_ingestion_jobs.return_value = {"ingestionJobSummaries": []}
    agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-new", "status": "STARTING"}
    }
    adapter = _adapter(agent=agent)

    job = adapter.ensure_ingestion_job(client_token="tok", description="ingest x")

    assert job.started is True
    assert job.job_id == "job-new"
    assert job.status == "STARTING"
    agent.start_ingestion_job.assert_called_once()


@pytest.mark.unit
def test_ensure_ingestion_job_treats_throttling_as_coalesced():
    if not knowledge.boto3_available():
        pytest.skip("requer botocore para ClientError com error code")
    agent = MagicMock()
    # Nenhum job ativo na primeira listagem; após o throttling, o job vencedor
    # da corrida aparece na segunda listagem.
    agent.list_ingestion_jobs.side_effect = [
        {"ingestionJobSummaries": []},
        {"ingestionJobSummaries": [{"ingestionJobId": "job-winner", "status": "STARTING"}]},
    ]
    agent.start_ingestion_job.side_effect = _aws_error("Rate limit exceeded")
    adapter = _adapter(agent=agent)

    job = adapter.ensure_ingestion_job(description="ingest y", sleep=lambda _s: None)

    assert job.started is True
    assert job.status == knowledge.COALESCED_STATUS
    assert job.job_id == "job-winner"
    # Coalescência só acontece apontando para um job real observado; o start
    # throttled da primeira tentativa não vira sucesso fantasma.
    assert agent.start_ingestion_job.call_count == 1


@pytest.mark.unit
def test_ensure_ingestion_job_non_coalescible_failure_stays_failed():
    if not knowledge.boto3_available():
        pytest.skip("requer botocore para ClientError com error code")
    from botocore.exceptions import ClientError

    agent = MagicMock()
    agent.list_ingestion_jobs.return_value = {"ingestionJobSummaries": []}
    agent.start_ingestion_job.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "sem permissão"}},
        "StartIngestionJob",
    )
    adapter = _adapter(agent=agent)

    job = adapter.ensure_ingestion_job(description="ingest z")

    assert job.started is False
    assert job.status == "FAILED"
    assert job.error_code == "AccessDeniedException"


@pytest.mark.unit
def test_ensure_ingestion_job_listing_failure_falls_back_to_start():
    agent = MagicMock()
    agent.list_ingestion_jobs.side_effect = _aws_error("listagem indisponível")
    agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-fallback", "status": "STARTING"}
    }
    adapter = _adapter(agent=agent)

    job = adapter.ensure_ingestion_job(description="ingest w")

    assert job.started is True
    assert job.job_id == "job-fallback"


@pytest.mark.unit
def test_ensure_ingestion_job_exhausted_without_real_job_reports_failure():
    """Sem job ativo observável e com todas as tentativas throttled, o resultado
    é uma falha honesta (started=False) — nunca um sucesso fantasma."""
    if not knowledge.boto3_available():
        pytest.skip("requer botocore para ClientError com error code")
    agent = MagicMock()
    agent.list_ingestion_jobs.return_value = {"ingestionJobSummaries": []}
    agent.start_ingestion_job.side_effect = _aws_error("Rate limit exceeded")
    adapter = _adapter(agent=agent)
    waits: list[float] = []

    job = adapter.ensure_ingestion_job(
        description="ingest q", max_attempts=3, sleep=waits.append
    )

    assert job.started is False
    assert job.error_code == "ThrottlingException"
    assert agent.start_ingestion_job.call_count == 3  # política limitada (<=3)
    assert waits == [10.0, 10.0]  # espera mínima entre tentativas
