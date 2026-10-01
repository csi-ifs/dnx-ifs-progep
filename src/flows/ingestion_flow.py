"""Ingestion pipeline wiring: S3 -> Ingestor_de_Documentos -> Knowledge Base (Task 14.3).

This module connects the S3 event that signals an object created/updated in the
data-source bucket to the pure-logic validation (:mod:`src.domain.ingestion`) and
to the Bedrock Knowledge Base ingestion job / incremental reindexation performed
by :class:`src.adapters.knowledge.KnowledgeBaseAdapter`.

Flow (Req 1.1, 1.7):

1. An S3 ``ObjectCreated`` event arrives (Lambda-style ``event``/``context``).
2. For every object in the event a :class:`~src.domain.models.DocumentCandidate`
   is assembled from the object key (source prefix), file extension
   (``content_type``) and reported size.
3. The candidate is validated by the pure-logic :func:`validate_document`; a
   rejected candidate produces a ``rejected`` :class:`IngestionRecord` and **no
   ingestion job is triggered** (Req 1.3).
4. An accepted candidate triggers the Knowledge Base ingestion job (Req 1.1). For
   updates to previously-indexed documents the incremental reindexation path is
   used (Req 1.7). Bedrock ingestion is incremental, so both paths converge on a
   single sync of the data source into the vector store.

The orchestration keeps **no** direct AWS calls of its own: all I/O is delegated
to the injected :class:`KnowledgeBaseAdapter`, which makes this flow fully
unit-testable with a mock adapter.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote_plus

from src.adapters.knowledge import IngestionJob, KnowledgeBaseAdapter
from src.domain.config import DocumentSource, IngestionStatus
from src.domain.ingestion import build_ingestion_record, validate_document
from src.domain.models import DocumentCandidate, IngestionRecord

logger = logging.getLogger(__name__)

#: Map of file extensions to the supported ``content_type`` values (Req 1.1).
#: Unknown extensions are passed through verbatim so :func:`validate_document`
#: rejects them with a descriptive reason (Req 1.3).
_EXTENSION_CONTENT_TYPE: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "txt",
    "html": "html",
    "htm": "html",
    "md": "md",
    "markdown": "md",
}

#: S3 event names that represent an object being created or overwritten. An
#: overwrite of an existing key is treated as an update for reindexation (Req 1.7).
_OBJECT_CREATED_PREFIX = "ObjectCreated"

@dataclass(frozen=True)
class S3ObjectRef:
    """A single object referenced by an S3 event record.

    Attributes:
        bucket: The S3 bucket name.
        key: The (URL-decoded) object key.
        size_bytes: The object size in bytes as reported by the event.
        event_name: The originating S3 event name (e.g. ``ObjectCreated:Put``).
    """

    bucket: str
    key: str
    size_bytes: int
    event_name: str = ""


def content_type_from_key(key: str) -> str:
    """Derive a document ``content_type`` from an object key's extension.

    Args:
        key: The S3 object key (e.g. ``"site/edital-2025.pdf"``).

    Returns:
        One of the supported content types (``pdf``/``docx``/``txt``/``html``/``md``)
        when the extension is recognized; otherwise the lower-cased raw
        extension (or ``""`` when the key has no extension), so that
        :func:`validate_document` rejects it with a descriptive reason.
    """
    filename = key.rsplit("/", 1)[-1]
    if "." not in filename:
        return ""
    extension = filename.rsplit(".", 1)[-1].strip().lower()
    return _EXTENSION_CONTENT_TYPE.get(extension, extension)


def source_from_key(key: str) -> str:
    """Derive the document ``source`` from the object key's leading prefix.

    The data-source bucket materializes both origins under distinct prefixes
    (Req 1.5): ``site/`` for the department site documentation/editais and
    ``cliente/`` for the client-provided repository.

    Args:
        key: The S3 object key.

    Returns:
        ``"site"`` or ``"cliente"`` when the prefix matches a known source;
        otherwise the first path segment verbatim, or ``"desconhecida"`` when the
        key has no directory prefix (guarantees a non-empty ``source`` for the
        ingestion record, per Req 1.2).
    """
    if "/" in key:
        prefix = key.split("/", 1)[0].strip().lower()
        for source in DocumentSource:
            if prefix == source.value:
                return source.value
        return prefix or "desconhecida"
    return "desconhecida"


def build_candidate_from_object(ref: S3ObjectRef) -> DocumentCandidate:
    """Assemble a :class:`DocumentCandidate` from an S3 object reference.

    The object key doubles as the unique ``doc_id`` (S3 keys are unique within a
    bucket), which keeps reindexation of an updated key idempotent (Req 1.7).

    Args:
        ref: The S3 object reference parsed from the event.

    Returns:
        A :class:`DocumentCandidate` ready for validation.
    """
    filename = ref.key.rsplit("/", 1)[-1]
    return DocumentCandidate(
        doc_id=ref.key,
        source=source_from_key(ref.key),
        filename=filename,
        content_type=content_type_from_key(ref.key),
        size_bytes=ref.size_bytes,
    )


def is_update_event(event_name: str) -> bool:
    """Return ``True`` when the S3 event represents an overwrite/update.

    S3 emits ``ObjectCreated:*`` for both new objects and overwrites of an
    existing key. A ``Copy`` or ``CompleteMultipartUpload`` on an existing key is
    treated as an update for the reindexation path (Req 1.7). Callers that have
    external knowledge of whether a key already existed can override the decision
    when invoking :func:`process_object`.
    """
    tail = event_name.split(":", 1)[-1] if ":" in event_name else event_name
    return tail in {"Copy", "CompleteMultipartUpload"}


def build_client_token(bucket: str, key: str) -> str:
    """Idempotency token for ``StartIngestionJob``, derived from the object.

    Keyed on the object identity so retries of the same S3 event do not spawn
    duplicate ingestion jobs. It must be a **hash**, not the ``bucket/key``
    itself: Bedrock validates the token against
    ``[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}``, which rejects the ``/``, ``.`` and
    ``_`` that every real key carries — a raw key made every automatic job fail
    with ``ValidationException``.
    """
    digest = hashlib.sha256(f"{bucket}/{key}".encode("utf-8")).hexdigest()
    return f"ingest{digest}"


def parse_s3_event(event: dict[str, Any]) -> list[S3ObjectRef]:
    """Parse a Lambda S3 notification event into object references.

    Only ``ObjectCreated*`` records are returned; other event types (e.g.
    ``ObjectRemoved``) are ignored by this ingestion flow.

    Args:
        event: The raw Lambda event payload with a ``Records`` array.

    Returns:
        A list of :class:`S3ObjectRef`, one per relevant record.
    """
    refs: list[S3ObjectRef] = []
    for record in event.get("Records", []) or []:
        event_name = str(record.get("eventName", ""))
        if event_name and not event_name.startswith(_OBJECT_CREATED_PREFIX):
            continue
        s3_block = record.get("s3", {}) or {}
        bucket = (s3_block.get("bucket", {}) or {}).get("name", "")
        obj = s3_block.get("object", {}) or {}
        raw_key = obj.get("key", "")
        if not (bucket and raw_key):
            logger.warning("Ignorando registro S3 sem bucket/key: %r", record)
            continue
        refs.append(
            S3ObjectRef(
                bucket=bucket,
                # S3 keys arrive URL-encoded in event notifications.
                key=unquote_plus(raw_key),
                size_bytes=int(obj.get("size", 0) or 0),
                event_name=event_name,
            )
        )
    return refs


def process_object(
    adapter: KnowledgeBaseAdapter,
    ref: S3ObjectRef,
    *,
    indexed_at: datetime | None = None,
    reindex: bool | None = None,
) -> IngestionRecord:
    """Validate and ingest a single S3 object into the Knowledge Base.

    Reuses the pure-logic validation/record assembly and the injected adapter for
    all AWS I/O. On rejection no ingestion job is triggered (Req 1.3); on adapter
    failure the previous Knowledge Base state is preserved and a ``failed`` record
    with the ``doc_id`` is produced (Req 1.4).

    Args:
        adapter: The Knowledge Base adapter (inject a mock in tests).
        ref: The S3 object reference to ingest.
        indexed_at: Timestamp for the record; defaults to ``datetime.now(UTC)``.
        reindex: Force the reindexation path (Req 1.7) when ``True`` or the
            ingestion path when ``False``. When ``None`` (default) the decision is
            derived from the event name via :func:`is_update_event`.

    Returns:
        An :class:`IngestionRecord` with status ``indexed``, ``rejected`` or
        ``failed``.
    """
    when = indexed_at or datetime.now(timezone.utc)
    candidate = build_candidate_from_object(ref)

    validation = validate_document(candidate)
    if not validation.accepted:
        logger.info("Documento rejeitado %s: %s", candidate.doc_id, validation.reason)
        return build_ingestion_record(
            candidate,
            when,
            status=IngestionStatus.REJECTED,
            reason=validation.reason,
        )

    use_reindex = is_update_event(ref.event_name) if reindex is None else reindex
    client_token = build_client_token(ref.bucket, ref.key)

    # ensure_ingestion_job aplica debounce: uploads em lote geram um único job de
    # sincronização (que cobre o data source inteiro) em vez de um job por objeto.
    job: IngestionJob = adapter.ensure_ingestion_job(
        client_token=client_token,
        description=f"{'reindex' if use_reindex else 'ingest'} {ref.key}",
    )

    if not job.started:
        logger.error(
            "Ingestion job não iniciado para %s: %s", candidate.doc_id, job.reason
        )
        return build_ingestion_record(
            candidate,
            when,
            status=IngestionStatus.FAILED,
            reason=job.reason or "ingestion job não iniciado",
        )

    logger.info(
        "Ingestion job %s iniciado para %s (status=%s, reindex=%s)",
        job.job_id,
        candidate.doc_id,
        job.status,
        use_reindex,
    )
    return build_ingestion_record(
        candidate,
        when,
        status=IngestionStatus.INDEXED,
        reason=None,
    )


def handle_s3_event(
    event: dict[str, Any],
    adapter: KnowledgeBaseAdapter,
    *,
    indexed_at: datetime | None = None,
) -> list[IngestionRecord]:
    """Orchestrate ingestion for every object in an S3 event.

    Parses the event, then validates and ingests each ``ObjectCreated`` object,
    returning one :class:`IngestionRecord` per processed object.

    Args:
        event: The raw Lambda S3 notification event.
        adapter: The Knowledge Base adapter (inject a mock in tests).
        indexed_at: Optional fixed timestamp for the produced records.

    Returns:
        A list of :class:`IngestionRecord`, one per processed object.
    """
    refs = parse_s3_event(event)
    records: list[IngestionRecord] = []
    for ref in refs:
        records.append(process_object(adapter, ref, indexed_at=indexed_at))
    return records


def _adapter_from_env() -> KnowledgeBaseAdapter:
    """Build a :class:`KnowledgeBaseAdapter` from environment configuration.

    Reads ``KNOWLEDGE_BASE_ID``, ``DATA_SOURCE_ID`` and optional ``KB_BUCKET`` /
    ``AWS_REGION`` variables. Clients are created lazily with the mandatory
    ``User_Agent_APN`` billing annotation (Req 8.3).

    Raises:
        RuntimeError: If required environment variables are missing.
    """
    kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
    ds_id = os.environ.get("DATA_SOURCE_ID")
    if not (kb_id and ds_id):
        raise RuntimeError(
            "KNOWLEDGE_BASE_ID e DATA_SOURCE_ID devem estar definidos no ambiente"
        )
    return KnowledgeBaseAdapter(
        knowledge_base_id=kb_id,
        data_source_id=ds_id,
        bucket=os.environ.get("KB_BUCKET"),
        region_name=os.environ.get("AWS_REGION"),
    )


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AWS Lambda entry point for the S3 -> Knowledge Base ingestion pipeline.

    Builds the Knowledge Base adapter from environment configuration, processes
    every object in the incoming S3 event and returns a structured summary.

    Args:
        event: The S3 notification event delivered by Lambda.
        context: The Lambda context object (unused).

    Returns:
        A summary dict with the count and per-document ingestion outcomes.
    """
    adapter = _adapter_from_env()
    records = handle_s3_event(event, adapter)
    return {
        "processed": len(records),
        "records": [
            {
                "doc_id": r.doc_id,
                "source": r.source,
                "status": r.status,
                "indexed_at": r.indexed_at.isoformat(),
                "reason": r.reason,
            }
            for r in records
        ],
    }
