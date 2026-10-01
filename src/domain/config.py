"""Shared configuration constants and enums for the Assistente PROGEP-IFS.

These values are defined once here and reused across the pure-logic layer, the
I/O adapters and the IaC. They map directly to the "Configurações" table and the
billing-annotation requirements of the design document.
"""

from __future__ import annotations

from enum import Enum

# --- Session limits (Req 4.2) ---------------------------------------------------
#: Maximum number of most-recent messages retained per session.
MAX_MESSAGES: int = 50

# --- Message size limits per channel (Req 2.6, 5.3, 6.4) ------------------------
#: Maximum characters accepted by the site integration API.
MAX_CHARS_API: int = 4000
#: Maximum characters accepted by the WhatsApp connector.
MAX_CHARS_WHATSAPP: int = 4096
#: Minimum characters for a valid message (Req 5.1: 1..4000).
MIN_CHARS: int = 1

# --- Session inactivity timeout (Req 4.3) ---------------------------------------
#: Minimum configurable inactivity timeout, in minutes.
MIN_TIMEOUT_MIN: int = 5
#: Maximum configurable inactivity timeout, in minutes.
MAX_TIMEOUT_MIN: int = 120
#: Default inactivity timeout, in minutes.
DEFAULT_TIMEOUT_MIN: int = 30

# --- Retry policy (Req 3.5, 6.5, 7.5) -------------------------------------------
#: Maximum number of attempts for retryable operations.
MAX_RETRIES: int = 3
#: Minimum interval, in seconds, between WhatsApp delivery attempts (Req 6.5).
MIN_WHATSAPP_RETRY_INTERVAL_S: int = 10
#: Per-attempt connection timeout, in seconds, for Amazon Connect (Req 3.5).
CONNECT_ATTEMPT_TIMEOUT_S: int = 5

# --- Document ingestion limits (Req 1.1, 1.3) -----------------------------------
#: Maximum accepted document size in bytes (50 MB).
MAX_DOC_SIZE_BYTES: int = 52_428_800

# --- Billing annotation (Req 8.1, 8.3) ------------------------------------------
#: Mandatory APN tag applied to 100% of taggable AWS resources (Req 8.1, 8.4).
APN_TAG: dict[str, str] = {"aws-apn-id": "pc:7b0pljcsg0gii1a5gn5vfca0v"}
#: Mandatory user-agent annotation injected into every Bedrock LLM call (Req 8.3).
USER_AGENT_APN: str = "APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$"


class Channel(str, Enum):
    """Supported input channels."""

    SITE = "site"
    WHATSAPP = "whatsapp"


class ContentType(str, Enum):
    """Supported document content types for ingestion (Req 1.1, 1.3)."""

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    HTML = "html"
    #: A Knowledge Base do Bedrock indexa Markdown nativamente, e é o formato
    #: das transcrições dos fluxogramas do Mapeamento de Processos
    #: (``scripts/transcribe_flows.py``). Sem ele o Ingestor rejeita cada
    #: arquivo e **nenhum** ingestion job é disparado.
    MD = "md"


#: Set of accepted content types for quick membership checks.
SUPPORTED_CONTENT_TYPES: frozenset[str] = frozenset(ct.value for ct in ContentType)


class DocumentSource(str, Enum):
    """Origin of an ingested document (Req 1.5)."""

    SITE = "site"
    CLIENTE = "cliente"


#: Prefixo do painel dentro do bucket de documentos. O que o administrador
#: confirma entra por aqui, separado do que vem do crawler (``site/``) e da
#: carga inicial do repositório — a procedência precisa ficar legível na chave,
#: porque é dela que sai o rótulo da fonte citada na resposta.
PAINEL_PREFIX: str = f"{DocumentSource.CLIENTE.value}/painel/"

#: Prefixos do **bucket de preparo**, que é um bucket à parte e não pertence a
#: nenhum data source da Knowledge Base.
#:
#: A separação é por bucket, e não por prefixo, porque ``inclusionPrefixes`` de
#: um data source S3 aceita **no máximo um** prefixo (limite da API). Com os
#: dois prefixos de origem já em uso, não havia como declarar "varra ``site/``
#: e ``cliente/``, ignore o resto" — e sem essa declaração o data source varre o
#: bucket inteiro, indexando o que ninguém confirmou.
#:
#: * ``preparo/`` — envio aguardando confirmação. **Nada observa este bucket**:
#:   confirmar é copiar para o bucket de documentos, e é isso que torna a
#:   revisão real em vez de um aviso na tela.
#: * ``removidos/`` — exclusão reversível. O documento sai da base na hora (via
#:   ``DeleteKnowledgeBaseDocuments``) mas o arquivo continua recuperável: um
#:   clique que apaga norma institucional em definitivo é um clique perigoso
#:   demais.
PREPARO_PREFIX: str = "preparo/"
REMOVIDOS_PREFIX: str = "removidos/"


class IngestionStatus(str, Enum):
    """Outcome of a document ingestion (Req 1.3, 1.4)."""

    INDEXED = "indexed"
    REJECTED = "rejected"
    FAILED = "failed"


class AgentStatus(str, Enum):
    """Outcome status of an agent response (Req 2.2, 2.4, 2.5)."""

    ANSWERED = "answered"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    ESCALATE = "escalate"


class RoutingDecision(str, Enum):
    """Result of the routing decision (Req 3.1, 3.2)."""

    DELIVER = "deliver"
    ESCALATE = "escalate"


class ApiStatus(str, Enum):
    """Top-level status of a structured API response (Req 5.4)."""

    OK = "ok"
    ERROR = "error"


class MessageRole(str, Enum):
    """Role of a message within a session history."""

    USER = "user"
    ASSISTANT = "assistant"


def channel_char_limit(channel: str) -> int:
    """Return the maximum allowed message length for the given channel.

    Site/API messages are limited to 4000 characters (Req 5.3); WhatsApp
    messages are limited to 4096 characters (Req 6.4).
    """
    if channel == Channel.WHATSAPP.value:
        return MAX_CHARS_WHATSAPP
    if channel == Channel.SITE.value:
        return MAX_CHARS_API
    raise ValueError(f"unknown channel: {channel!r}")
