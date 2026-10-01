"""Knowledge-layer I/O adapters (Task 12.1).

Thin, unit-testable adapters that wrap the AWS services backing the
``Base_de_Conhecimento``:

* **S3** — the data source bucket where documents/editais are materialized.
* **Amazon Bedrock Knowledge Base** — triggers/schedules the *ingestion job* that
  converts documents into embeddings, and performs the incremental reindexation
  when a previously indexed document is updated (Req 1.1, 1.7).

Design constraints honoured here:

* The pure-logic layer (:mod:`src.domain.ingestion`) is reused for validation and
  for assembling the :class:`~src.domain.models.IngestionRecord`; this module only
  performs I/O and error translation.
* On ingestion failure the adapter records the error with the ``doc_id`` and
  **preserves the previous state of the Knowledge Base unchanged** — it never
  mutates state on the failure path and simply returns a ``failed``
  :class:`~src.domain.models.IngestionRecord` (Req 1.4).
* Every boto3 client is created with the mandatory partner billing annotation
  injected via ``Config(user_agent_extra=...)`` and the ``AWS_SDK_UA_APP_ID``
  environment variable, so 100% of Bedrock calls carry the ``User_Agent_APN``
  (Req 8.3).

The boto3 import is guarded so the module imports even in environments where
boto3 is not installed; the AWS-backed clients are only constructed lazily when
an operation actually needs them. This keeps the adapter unit-testable by
injecting mock clients.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.domain.config import USER_AGENT_APN, IngestionStatus
from src.domain.ingestion import build_ingestion_record, validate_document
from src.domain.models import DocumentCandidate, IngestionRecord, ValidationResult

logger = logging.getLogger(__name__)

# --- Guarded boto3 import -------------------------------------------------------
# The module must import even when boto3 is absent; AWS clients are built lazily.
try:  # pragma: no cover - exercised indirectly depending on the environment
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import BotoCoreError, ClientError

    _BOTO3_AVAILABLE = True
    # Exceptions raised by boto3 operations that we translate into failure records.
    _AWS_ERRORS: tuple[type[Exception], ...] = (BotoCoreError, ClientError)
except ImportError:  # pragma: no cover - depends on the runtime environment
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment]
    BotoCoreError = ClientError = Exception  # type: ignore[assignment,misc]
    _BOTO3_AVAILABLE = False
    _AWS_ERRORS = (Exception,)


def boto3_available() -> bool:
    """Return ``True`` when boto3/botocore are importable in this environment."""
    return _BOTO3_AVAILABLE


def _require_boto3() -> None:
    """Raise a clear error when an AWS operation is attempted without boto3."""
    if not _BOTO3_AVAILABLE:
        raise RuntimeError(
            "boto3 is not installed; install it to construct AWS clients "
            "(pip install boto3) or inject mock clients into the adapter."
        )


def apn_boto_config(**overrides: Any) -> "BotoConfig":
    """Build a botocore ``Config`` carrying the mandatory ``User_Agent_APN``.

    Injects the partner billing annotation into the client user-agent via
    ``user_agent_extra`` so every AWS/Bedrock call made through the resulting
    client is attributed to the partner (Req 8.3). Callers may pass extra
    ``Config`` keyword overrides (e.g. ``region_name``, ``retries``).

    Args:
        **overrides: Additional ``botocore.config.Config`` keyword arguments.

    Returns:
        A configured ``botocore.config.Config`` instance.

    Raises:
        RuntimeError: If boto3/botocore are not available in the environment.
    """
    _require_boto3()
    # AWS_SDK_UA_APP_ID is the SDK-standard env var for a user-agent app id and is
    # honoured by botocore in addition to user_agent_extra (Req 8.3).
    os.environ.setdefault("AWS_SDK_UA_APP_ID", USER_AGENT_APN)
    overrides.setdefault("user_agent_extra", USER_AGENT_APN)
    return BotoConfig(**overrides)


def make_client(service_name: str, *, region_name: str | None = None, **kwargs: Any) -> Any:
    """Create a boto3 client with the ``User_Agent_APN`` billing annotation.

    All knowledge-layer clients (S3, ``bedrock-agent``, ``bedrock-runtime``) are
    created through this helper so the annotation is applied uniformly (Req 8.3).

    Args:
        service_name: The AWS service name (e.g. ``"bedrock-agent"``, ``"s3"``).
        region_name: Optional AWS region.
        **kwargs: Extra keyword arguments forwarded to ``boto3.client``.

    Returns:
        A configured boto3 client.

    Raises:
        RuntimeError: If boto3 is not available in the environment.
    """
    _require_boto3()
    return boto3.client(
        service_name,
        region_name=region_name,
        config=apn_boto_config(),
        **kwargs,
    )


#: Ingestion-job statuses that mean a sync is already underway; a new job would
#: conflict with (and is subsumed by) the running one.
ACTIVE_JOB_STATUSES: frozenset[str] = frozenset({"STARTING", "IN_PROGRESS"})

#: AWS error codes treated as coalescible when starting an ingestion job: they
#: mean "a sync is already running / too many concurrent starts", and since a
#: Bedrock ingestion job syncs the whole data source, the concurrent job covers
#: this document too — the failure is non-fatal (debounce).
COALESCIBLE_ERROR_CODES: frozenset[str] = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ConflictException"}
)

#: Synthetic status for a request coalesced into an already-running job.
COALESCED_STATUS: str = "COALESCED"


def client_error_code(exc: Exception) -> str | None:
    """Extract the AWS error code from a botocore ``ClientError``-like exception.

    Pure helper (no boto3 required) so the coalescing decision is unit-testable:
    reads ``exc.response["Error"]["Code"]`` when present, else returns ``None``.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if code:
                return str(code)
    return None


def is_coalescible_error(exc: Exception) -> bool:
    """Return ``True`` when a start-job failure can be coalesced (debounce)."""
    return client_error_code(exc) in COALESCIBLE_ERROR_CODES


@dataclass(frozen=True)
class IngestionJob:
    """Result of triggering/scheduling a Knowledge Base ingestion job.

    Attributes:
        job_id: The ingestion job identifier returned by Bedrock, or ``None`` when
            the trigger failed before a job was created.
        status: The ingestion job status reported by Bedrock (e.g. ``STARTING``),
            ``"COALESCED"`` when the request was folded into an already-running
            job, or ``"FAILED"`` when the trigger call itself failed.
        knowledge_base_id: The target Knowledge Base id.
        data_source_id: The target data source id.
        started: ``True`` when Bedrock accepted the request, a job was created, or
            the request was coalesced into a job already in progress.
        reason: A non-empty description when ``started`` is ``False`` (or context
            for a coalesced request).
        error_code: The AWS error code when the trigger failed, if identifiable.
    """

    knowledge_base_id: str
    data_source_id: str
    started: bool
    job_id: str | None = None
    status: str | None = None
    reason: str | None = None
    error_code: str | None = None


class KnowledgeBaseAdapter:
    """Adapter to upload documents to S3 and drive Bedrock KB ingestion jobs.

    The adapter reuses the pure-logic validation/record assembly and confines all
    I/O and AWS-error translation to this class. Clients may be injected for
    unit testing; otherwise they are created lazily with the APN billing
    annotation (Req 8.3).
    """

    def __init__(
        self,
        *,
        knowledge_base_id: str,
        data_source_id: str,
        bucket: str | None = None,
        region_name: str | None = None,
        bedrock_agent_client: Any | None = None,
        s3_client: Any | None = None,
    ) -> None:
        """Initialize the adapter.

        Args:
            knowledge_base_id: Target Bedrock Knowledge Base id.
            data_source_id: Target data source id within the Knowledge Base.
            bucket: Optional S3 bucket that backs the data source (for uploads).
            region_name: Optional AWS region for lazily-created clients.
            bedrock_agent_client: Optional pre-built ``bedrock-agent`` client
                (inject a mock in tests). Created lazily when omitted.
            s3_client: Optional pre-built ``s3`` client. Created lazily when
                omitted and an S3 operation is requested.
        """
        if not (knowledge_base_id and knowledge_base_id.strip()):
            raise ValueError("knowledge_base_id não pode ser vazio")
        if not (data_source_id and data_source_id.strip()):
            raise ValueError("data_source_id não pode ser vazio")

        self.knowledge_base_id = knowledge_base_id
        self.data_source_id = data_source_id
        self.bucket = bucket
        self._region_name = region_name
        self._bedrock_agent_client = bedrock_agent_client
        self._s3_client = s3_client

    # -- lazy clients ------------------------------------------------------------

    @property
    def bedrock_agent(self) -> Any:
        """The ``bedrock-agent`` client, created lazily with the APN config."""
        if self._bedrock_agent_client is None:
            self._bedrock_agent_client = make_client(
                "bedrock-agent", region_name=self._region_name
            )
        return self._bedrock_agent_client

    @property
    def s3(self) -> Any:
        """The ``s3`` client, created lazily with the APN config."""
        if self._s3_client is None:
            self._s3_client = make_client("s3", region_name=self._region_name)
        return self._s3_client

    # -- S3 ----------------------------------------------------------------------

    def upload_document(self, key: str, body: bytes, content_type: str | None = None) -> None:
        """Upload a document object to the data source bucket.

        Args:
            key: The S3 object key (prefix identifies the ``site``/``cliente``
                source per the design).
            body: Raw document bytes.
            content_type: Optional MIME content type for the object.

        Raises:
            ValueError: If no ``bucket`` was configured on the adapter.
        """
        if not self.bucket:
            raise ValueError("bucket não configurado para upload no S3")
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, **extra)

    # -- Knowledge Base ingestion ------------------------------------------------

    def start_ingestion_job(
        self,
        *,
        client_token: str | None = None,
        description: str | None = None,
    ) -> IngestionJob:
        """Trigger (or schedule) a Bedrock Knowledge Base ingestion job (Req 1.1).

        A Bedrock ingestion job synchronizes the data source into the vector
        store, converting new/updated documents into embeddings. Bedrock
        ingestion is incremental by design, so the same call also serves the
        reindexation flow (Req 1.7) — see :meth:`reindex`.

        On any AWS error the previous state of the Knowledge Base is left
        untouched (no mutation happens on the failure path) and a non-started
        :class:`IngestionJob` describing the failure is returned (Req 1.4).

        Args:
            client_token: Optional idempotency token forwarded to Bedrock.
            description: Optional human-readable job description.

        Returns:
            An :class:`IngestionJob` describing the outcome.
        """
        params: dict[str, Any] = {
            "knowledgeBaseId": self.knowledge_base_id,
            "dataSourceId": self.data_source_id,
        }
        if client_token:
            params["clientToken"] = client_token
        if description:
            params["description"] = description

        try:
            response = self.bedrock_agent.start_ingestion_job(**params)
        except _AWS_ERRORS as exc:  # pragma: no cover - error path via mocks
            logger.error(
                "Falha ao iniciar ingestion job da KB %s (data source %s): %s",
                self.knowledge_base_id,
                self.data_source_id,
                exc,
            )
            return IngestionJob(
                knowledge_base_id=self.knowledge_base_id,
                data_source_id=self.data_source_id,
                started=False,
                status="FAILED",
                reason=str(exc),
                error_code=client_error_code(exc),
            )

        job = (response or {}).get("ingestionJob", {})
        return IngestionJob(
            knowledge_base_id=self.knowledge_base_id,
            data_source_id=self.data_source_id,
            started=True,
            job_id=job.get("ingestionJobId"),
            status=job.get("status"),
        )

    def find_active_ingestion_job(self) -> str | None:
        """Return the id of an ingestion job already syncing this data source.

        Used for debounce: a Bedrock KB allows one concurrent ingestion job per
        data source and every job syncs the whole source, so when a job is
        ``STARTING``/``IN_PROGRESS`` there is no point starting another. Errors
        while listing are swallowed (returns ``None``) so the caller falls back
        to attempting the start normally.
        """
        try:
            response = self.bedrock_agent.list_ingestion_jobs(
                knowledgeBaseId=self.knowledge_base_id,
                dataSourceId=self.data_source_id,
                maxResults=10,
                sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            )
        except _AWS_ERRORS as exc:
            logger.warning("Falha ao listar ingestion jobs (seguindo sem debounce): %s", exc)
            return None
        for summary in (response or {}).get("ingestionJobSummaries", []) or []:
            if summary.get("status") in ACTIVE_JOB_STATUSES:
                return summary.get("ingestionJobId")
        return None

    def ensure_ingestion_job(
        self,
        *,
        client_token: str | None = None,
        description: str | None = None,
        max_attempts: int = 3,
        wait_seconds: float = 10.0,
        sleep: Any = None,
    ) -> IngestionJob:
        """Start an ingestion job, coalescing with any job already running.

        Debounce for burst uploads (e.g. many S3 objects created at once):

        1. If a job is already ``STARTING``/``IN_PROGRESS``, do not start another
           — return a started :class:`IngestionJob` with status ``COALESCED``
           pointing at the running job (it syncs the whole data source anyway).
        2. Otherwise try to start one. On a coalescible failure
           (throttling/conflict) the winner of the race may not be visible yet,
           so wait ``wait_seconds`` and repeat, up to ``max_attempts`` tries
           (bounded retry policy, ≤ 3 per the design).
        3. A request is only reported as ``COALESCED`` when a **real** running
           job is observed; if every attempt fails and no job exists, the last
           failure is returned unchanged (``started=False``) so the document is
           honestly recorded as ``failed`` — never a phantom success.
        4. Any non-coalescible failure is returned immediately.

        Args:
            client_token: Optional idempotency token forwarded to Bedrock.
            description: Optional human-readable job description.
            max_attempts: Maximum start attempts (bounded, default 3).
            wait_seconds: Minimum wait between attempts.
            sleep: Injectable sleep function (for tests); defaults to
                ``time.sleep``.
        """
        do_sleep = sleep if sleep is not None else time.sleep
        attempts = max(1, max_attempts)
        last_failure: IngestionJob | None = None

        for attempt in range(attempts):
            active_job_id = self.find_active_ingestion_job()
            if active_job_id:
                logger.info(
                    "Ingestion job %s já em andamento; requisição coalescida (debounce)",
                    active_job_id,
                )
                return IngestionJob(
                    knowledge_base_id=self.knowledge_base_id,
                    data_source_id=self.data_source_id,
                    started=True,
                    job_id=active_job_id,
                    status=COALESCED_STATUS,
                    reason="job de ingestão já em andamento cobre este documento",
                )

            job = self.start_ingestion_job(
                client_token=client_token, description=description
            )
            if job.started or job.error_code not in COALESCIBLE_ERROR_CODES:
                return job

            last_failure = job
            logger.warning(
                "StartIngestionJob falhou com %s (tentativa %d/%d): %s",
                job.error_code,
                attempt + 1,
                attempts,
                job.reason,
            )
            if attempt < attempts - 1:
                do_sleep(wait_seconds)

        # Attempts exhausted: only coalesce if a real running job is observed.
        active_job_id = self.find_active_ingestion_job()
        if active_job_id:
            return IngestionJob(
                knowledge_base_id=self.knowledge_base_id,
                data_source_id=self.data_source_id,
                started=True,
                job_id=active_job_id,
                status=COALESCED_STATUS,
                reason="job de ingestão em andamento após tentativas de início",
            )
        assert last_failure is not None  # loop always ran at least once
        return last_failure

    def reindex(
        self,
        *,
        client_token: str | None = None,
        description: str | None = None,
    ) -> IngestionJob:
        """Trigger incremental reindexation of updated documents (Req 1.7).

        Bedrock ingestion jobs re-sync the data source incrementally, so updated
        documents are reindexed and queries started after completion reflect the
        new version. This is a thin, intention-revealing alias over
        :meth:`start_ingestion_job`.
        """
        return self.start_ingestion_job(
            client_token=client_token,
            description=description or "incremental reindex",
        )

    def get_ingestion_job(self, ingestion_job_id: str) -> dict[str, Any]:
        """Fetch the current status of a previously started ingestion job.

        Args:
            ingestion_job_id: The Bedrock ingestion job id.

        Returns:
            The ``ingestionJob`` payload returned by Bedrock (possibly empty).
        """
        response = self.bedrock_agent.get_ingestion_job(
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
            ingestionJobId=ingestion_job_id,
        )
        return (response or {}).get("ingestionJob", {})

    # -- High-level ingest with failure preservation -----------------------------

    def ingest_document(
        self,
        doc: DocumentCandidate,
        *,
        indexed_at: datetime | None = None,
        client_token: str | None = None,
    ) -> IngestionRecord:
        """Validate and ingest a document, recording the outcome (Req 1.1-1.4).

        Flow:

        1. Validate format/size via the pure-logic :func:`validate_document`. On
           rejection, no ingestion job is started and a ``rejected`` record with a
           non-empty reason is returned (Req 1.3).
        2. Otherwise trigger the Knowledge Base ingestion job (Req 1.1).
        3. If the trigger fails, record the error **with the ``doc_id``** and
           preserve the previous state of the Knowledge Base unchanged by not
           mutating anything on the failure path (Req 1.4); a ``failed`` record is
           returned.

        Args:
            doc: The candidate document.
            indexed_at: Timestamp to record; defaults to ``datetime.now(UTC)``.
            client_token: Optional idempotency token for the ingestion job.

        Returns:
            An :class:`IngestionRecord` with status ``indexed``, ``rejected`` or
            ``failed``.
        """
        when = indexed_at or datetime.now(timezone.utc)

        validation: ValidationResult = validate_document(doc)
        if not validation.accepted:
            return build_ingestion_record(
                doc,
                when,
                status=IngestionStatus.REJECTED,
                reason=validation.reason,
            )

        job = self.start_ingestion_job(client_token=client_token)
        if not job.started:
            # Failure: record with doc_id; KB previous state is left unchanged.
            return build_ingestion_record(
                doc,
                when,
                status=IngestionStatus.FAILED,
                reason=job.reason or "ingestion job não iniciado",
            )

        return build_ingestion_record(
            doc,
            when,
            status=IngestionStatus.INDEXED,
            reason=None,
        )
