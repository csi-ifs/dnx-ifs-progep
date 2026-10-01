"""Pure-logic for the Ingestor_de_Documentos (Task 2.1).

This module contains the format/size validation of candidate documents and the
assembly of the ingestion metadata record. It is intentionally free of any I/O:
disparo do *ingestion job*, escrita de embeddings e acesso a S3/Knowledge Base
ficam nos adaptadores (Task 12). Manter esta camada pura permite validá-la por
propriedades (Hypothesis) e por testes unitários.

Requisitos cobertos:
- 1.1: formatos suportados (PDF, DOCX, TXT, HTML) e tamanho máximo de 50 MB.
- 1.2: registro de origem, identificador único e horário de indexação com
  precisão de segundos.
- 1.3: rejeição produz ``reason`` não vazio; nenhum embedding é gravado (esta
  camada nunca grava embeddings — a decisão de gravar depende de ``accepted``).
- 1.5: as fontes de origem suportadas são a documentação/editais do site
  (``DocumentSource.SITE``) e o repositório do cliente (``DocumentSource.CLIENTE``).
- 1.6: quando apenas uma das fontes está disponível, a ingestão ocorre a partir
  da fonte disponível e a indisponibilidade da outra é registrada.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from src.domain.config import (
    MAX_DOC_SIZE_BYTES,
    SUPPORTED_CONTENT_TYPES,
    DocumentSource,
    IngestionStatus,
)
from src.domain.models import DocumentCandidate, IngestionRecord, ValidationResult


def validate_document(doc: DocumentCandidate) -> ValidationResult:
    """Validate a candidate document by format and size (Req 1.1, 1.3).

    The document is accepted **if and only if** its ``content_type`` is one of
    the supported formats (pdf, docx, txt, html) **and** its ``size_bytes`` is
    between 0 and ``MAX_DOC_SIZE_BYTES`` (50 MB) inclusive.

    Every rejection yields ``accepted=False`` with a non-empty ``reason``
    (Req 1.3). This function performs no I/O and never writes embeddings; a
    caller must only persist embeddings when ``accepted`` is ``True``.

    Args:
        doc: The candidate document to validate.

    Returns:
        A :class:`ValidationResult`. When rejected, ``reason`` is a non-empty
        human-readable description listing every problem found.
    """
    reasons: list[str] = []

    content_type = (doc.content_type or "").strip().lower()
    if content_type not in SUPPORTED_CONTENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_CONTENT_TYPES))
        reasons.append(
            f"formato não suportado: {doc.content_type!r} "
            f"(suportados: {supported})"
        )

    if doc.size_bytes < 0:
        reasons.append(f"tamanho inválido: {doc.size_bytes} bytes (deve ser >= 0)")
    elif doc.size_bytes > MAX_DOC_SIZE_BYTES:
        reasons.append(
            f"tamanho excede o máximo: {doc.size_bytes} bytes "
            f"(máximo: {MAX_DOC_SIZE_BYTES} bytes)"
        )

    if reasons:
        return ValidationResult(accepted=False, reason="; ".join(reasons))
    return ValidationResult(accepted=True, reason=None)


def build_ingestion_record(
    doc: DocumentCandidate,
    indexed_at: datetime,
    status: IngestionStatus = IngestionStatus.INDEXED,
    reason: str | None = None,
) -> IngestionRecord:
    """Assemble the ingestion metadata record for a document (Req 1.2).

    Registers the origin (``source``), the unique identifier (``doc_id``) and the
    indexing timestamp truncated to **second precision** (Req 1.2). Microseconds
    are dropped so the recorded time reflects "data e hora com precisão de
    segundos" as required.

    Args:
        doc: The document being recorded. Its ``doc_id`` must be non-empty.
        indexed_at: The moment of indexing; truncated to whole seconds.
        status: The ingestion outcome (defaults to ``INDEXED``).
        reason: Rejection/failure reason. Required (non-empty) when ``status`` is
            ``REJECTED`` or ``FAILED``.

    Returns:
        An immutable :class:`IngestionRecord`.

    Raises:
        ValueError: If ``doc_id`` is empty/blank, if ``source`` is empty/blank,
            or if a rejected/failed status is missing a non-empty ``reason``.
    """
    doc_id = (doc.doc_id or "").strip()
    if not doc_id:
        raise ValueError("doc_id não pode ser vazio (Req 1.2)")

    source = (doc.source or "").strip()
    if not source:
        raise ValueError("source não pode ser vazia (Req 1.2)")

    if status in (IngestionStatus.REJECTED, IngestionStatus.FAILED) and not (
        reason and reason.strip()
    ):
        raise ValueError(
            f"status {status.value!r} exige um motivo não vazio (Req 1.3, 1.4)"
        )

    return IngestionRecord(
        doc_id=doc_id,
        source=source,
        indexed_at=indexed_at.replace(microsecond=0),
        status=status.value,
        reason=reason,
    )


@dataclass(frozen=True)
class SourceAvailabilityResolution:
    """Outcome of resolving which origin sources to ingest from (Req 1.5, 1.6).

    Attributes:
        to_ingest: The sources that are available and should be ingested, in the
            canonical order defined by :class:`DocumentSource`.
        unavailable: The known sources that are not available and whose
            unavailability must be recorded, in canonical order.
        notes: Human-readable unavailability records, one per unavailable source
            (Req 1.6). Empty when every known source is available.
    """

    to_ingest: tuple[str, ...]
    unavailable: tuple[str, ...]
    notes: tuple[str, ...]


def _normalize_source(value: DocumentSource | str) -> str:
    """Normalize a source input to its canonical string value.

    Accepts either a :class:`DocumentSource` member or its string value
    (case-insensitive). Raises ``ValueError`` for unknown sources so callers
    cannot silently pass an unsupported origin (Req 1.5).
    """
    if isinstance(value, DocumentSource):
        return value.value
    normalized = (value or "").strip().lower()
    try:
        return DocumentSource(normalized).value
    except ValueError as exc:
        supported = ", ".join(s.value for s in DocumentSource)
        raise ValueError(
            f"fonte de origem desconhecida: {value!r} (suportadas: {supported})"
        ) from exc


def resolve_available_sources(
    available: Iterable[DocumentSource | str],
) -> SourceAvailabilityResolution:
    """Resolve which origin sources to ingest and record the unavailable ones.

    The supported origin sources are the documentation/editais of the department
    site (``DocumentSource.SITE``) and the client-provided repository
    (``DocumentSource.CLIENTE``) (Req 1.5). Given the set of currently available
    sources, this pure-logic helper returns which ones to ingest from and records
    the unavailability of every known source that is missing (Req 1.6).

    When only one source is available, ingestion proceeds from that source and
    the other is recorded as unavailable. When both are available, ``unavailable``
    and ``notes`` are empty. When none is available, both known sources are
    recorded as unavailable and ``to_ingest`` is empty.

    Args:
        available: The sources currently available, as :class:`DocumentSource`
            members or their string values. Duplicates are ignored.

    Returns:
        A :class:`SourceAvailabilityResolution` describing the sources to ingest,
        the unavailable sources and the corresponding unavailability notes, all in
        the canonical :class:`DocumentSource` order.

    Raises:
        ValueError: If any provided source is not a supported origin (Req 1.5).
    """
    available_values = {_normalize_source(src) for src in available}

    to_ingest: list[str] = []
    unavailable: list[str] = []
    notes: list[str] = []
    for source in DocumentSource:
        if source.value in available_values:
            to_ingest.append(source.value)
        else:
            unavailable.append(source.value)
            notes.append(f"fonte de origem indisponível: {source.value}")

    return SourceAvailabilityResolution(
        to_ingest=tuple(to_ingest),
        unavailable=tuple(unavailable),
        notes=tuple(notes),
    )
