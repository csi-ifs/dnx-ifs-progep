"""Shared immutable domain models for the Assistente PROGEP-IFS.

All dataclasses are ``frozen=True`` to enforce immutability across the pure-logic
layer. Signatures mirror the interfaces defined in ``design.md`` (Components and
Interfaces section).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


# --- Ingestor_de_Documentos -----------------------------------------------------

@dataclass(frozen=True)
class DocumentCandidate:
    """A document being considered for ingestion into the Knowledge Base."""

    doc_id: str
    source: str            # "site" | "cliente"
    filename: str
    content_type: str      # "pdf" | "docx" | "txt" | "html" | outro
    size_bytes: int


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a validation (document or message).

    When ``accepted`` is ``False``, ``reason`` MUST be a non-empty description of
    the rejection (Req 1.3, 5.3).
    """

    accepted: bool
    reason: str | None = None   # motivo da rejeição quando accepted=False


@dataclass(frozen=True)
class IngestionRecord:
    """Metadata recorded when a document is ingested (Req 1.2, 1.3, 1.4)."""

    doc_id: str
    source: str
    indexed_at: datetime   # precisão de segundos
    status: str            # "indexed" | "rejected" | "failed"
    reason: str | None = None


# --- Agente_IA ------------------------------------------------------------------

@dataclass(frozen=True)
class AgentRequest:
    """A text request routed to the AI agent."""

    session_id: str
    channel: str           # "site" | "whatsapp"
    text: str


@dataclass(frozen=True)
class SourceRef:
    """A reference to a source document used to build a response (Req 2.3)."""

    doc_id: str
    score: float


@dataclass(frozen=True)
class AgentResponse:
    """A response produced by the AI agent."""

    status: str            # "answered" | "not_found" | "unavailable" | "escalate"
    content: str
    sources: tuple[SourceRef, ...] = ()
    confidence: float = 0.0
    #: Setores a indicar quando não há resposta (códigos de
    #: :mod:`src.domain.contatos`); vazio nas respostas fundamentadas.
    setores: tuple[str, ...] = ()


# --- Gestor_de_Sessao -----------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """A single message within a session history."""

    role: str              # "user" | "assistant"
    text: str
    at: datetime


@dataclass(frozen=True)
class Session:
    """A user conversation session (Req 4.1-4.5)."""

    session_id: str
    user_key: str
    channel: str           # "site" | "whatsapp"
    messages: tuple[Message, ...] = ()   # <= 50 (mais recentes)
    last_activity: datetime | None = None
    active: bool = True


# --- API_de_Integracao ----------------------------------------------------------

@dataclass(frozen=True)
class ErrorDetail:
    """Structured error detail returned by the API (Req 5.4)."""

    code: str              # "unauthenticated" | "invalid_request" | "agent_unavailable" | ...
    message: str


@dataclass(frozen=True)
class ApiResponse:
    """Structured response envelope returned by the API (Req 5.1, 5.4).

    On success ``status == "ok"`` and ``content`` is set; on error
    ``status == "error"`` and ``error`` is set. Exactly one of ``content`` or
    ``error`` is populated (Property 16).
    """

    status: str            # "ok" | "error"
    content: str | None = None
    error: ErrorDetail | None = None
