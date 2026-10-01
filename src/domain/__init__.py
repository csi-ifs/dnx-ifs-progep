"""Pure-logic domain layer for the Assistente PROGEP-IFS.

Exposes the shared immutable domain models and configuration constants used
across the pure-logic layer, the I/O adapters and the IaC.
"""

from __future__ import annotations

from src.domain.config import (
    APN_TAG,
    CONNECT_ATTEMPT_TIMEOUT_S,
    DEFAULT_TIMEOUT_MIN,
    MAX_CHARS_API,
    MAX_CHARS_WHATSAPP,
    MAX_DOC_SIZE_BYTES,
    MAX_MESSAGES,
    MAX_RETRIES,
    MAX_TIMEOUT_MIN,
    MIN_CHARS,
    MIN_TIMEOUT_MIN,
    MIN_WHATSAPP_RETRY_INTERVAL_S,
    SUPPORTED_CONTENT_TYPES,
    USER_AGENT_APN,
    AgentStatus,
    ApiStatus,
    Channel,
    ContentType,
    DocumentSource,
    IngestionStatus,
    MessageRole,
    RoutingDecision,
    channel_char_limit,
)
from src.domain.availability import (
    UNAVAILABLE_USER_MESSAGE,
    Dependency,
    FailureKind,
    UnavailabilityResult,
    signal_unavailable,
)
from src.domain.escalation import EscalationPayload, build_escalation_payload
from src.domain.iam import (
    EFFECT_ALLOW,
    EFFECT_DENY,
    FULL_WILDCARD,
    ExcessivePrivilegeError,
    IamPolicy,
    IamStatement,
    PolicyValidationResult,
    build_policy,
    build_statement,
    grants_admin,
    is_broad_action_wildcard,
    is_broad_resource_wildcard,
    normalize_tokens,
    to_policy_document,
    validate_policy,
    validate_statement,
)
from src.domain.ingestion import build_ingestion_record, validate_document
from src.domain.input_validation import (
    NON_TEXT_PARTS,
    TEXT_ONLY_WARNING,
    TEXT_PART,
    ContentClassification,
    classify_content,
    validate_message,
)
from src.domain.monitor import CallRecord, CallStatus, record_llm_failure
from src.domain.models import (
    AgentRequest,
    AgentResponse,
    ApiResponse,
    DocumentCandidate,
    ErrorDetail,
    IngestionRecord,
    Message,
    Session,
    SourceRef,
    ValidationResult,
)
from src.domain.session import (
    SESSION_CREATION_FAILED,
    SessionCreationError,
    SessionResolution,
    append_message,
    create_session,
    expire_session,
    generate_session_id,
    is_active,
    resolve_or_create_session,
    resolve_session,
)

__all__ = [
    # models
    "DocumentCandidate",
    "ValidationResult",
    "IngestionRecord",
    "AgentRequest",
    "AgentResponse",
    "SourceRef",
    "Message",
    "Session",
    "ApiResponse",
    "ErrorDetail",
    # config constants
    "MAX_MESSAGES",
    "MAX_CHARS_API",
    "MAX_CHARS_WHATSAPP",
    "MIN_CHARS",
    "MIN_TIMEOUT_MIN",
    "MAX_TIMEOUT_MIN",
    "DEFAULT_TIMEOUT_MIN",
    "MAX_RETRIES",
    "MIN_WHATSAPP_RETRY_INTERVAL_S",
    "CONNECT_ATTEMPT_TIMEOUT_S",
    "MAX_DOC_SIZE_BYTES",
    "APN_TAG",
    "USER_AGENT_APN",
    "SUPPORTED_CONTENT_TYPES",
    # enums
    "Channel",
    "ContentType",
    "DocumentSource",
    "IngestionStatus",
    "AgentStatus",
    "RoutingDecision",
    "ApiStatus",
    "MessageRole",
    # helpers
    "channel_char_limit",
    # ingestion logic
    "validate_document",
    "build_ingestion_record",
    # escalation logic
    "build_escalation_payload",
    "EscalationPayload",
    # input validation & content classification
    "validate_message",
    "classify_content",
    "ContentClassification",
    "TEXT_PART",
    "NON_TEXT_PARTS",
    "TEXT_ONLY_WARNING",
    # session logic
    "generate_session_id",
    "create_session",
    "append_message",
    "is_active",
    "expire_session",
    "resolve_session",
    "resolve_or_create_session",
    "SessionResolution",
    "SessionCreationError",
    "SESSION_CREATION_FAILED",
    # availability / unavailability handling
    "signal_unavailable",
    "UnavailabilityResult",
    "Dependency",
    "FailureKind",
    "UNAVAILABLE_USER_MESSAGE",
    # consumption monitoring
    "record_llm_failure",
    "CallRecord",
    "CallStatus",
    # IAM least-privilege logic
    "IamStatement",
    "IamPolicy",
    "PolicyValidationResult",
    "ExcessivePrivilegeError",
    "validate_statement",
    "validate_policy",
    "build_statement",
    "build_policy",
    "to_policy_document",
    "grants_admin",
    "is_broad_action_wildcard",
    "is_broad_resource_wildcard",
    "normalize_tokens",
    "EFFECT_ALLOW",
    "EFFECT_DENY",
    "FULL_WILDCARD",
]
