"""Mandatory billing-annotation guard for Bedrock LLM calls (pure-logic layer).

Implements the pure-logic that enforces the partner billing annotation (PRM/APN)
on every call to the Amazon Bedrock LLM. Per Requirements 8.3 and 8.5, the
Agente_IA must annotate 100% of its LLM calls with the ``User_Agent_APN`` of
value exactly ``APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$``. When that annotation is
missing or has a different value, the call MUST be blocked and an error
reporting the missing/incorrect ``User_Agent_APN`` MUST be reported.

The guard is intentionally free of any I/O so it can be exercised by
property-based tests (Property 17). The actual Bedrock client configuration —
injecting the annotation via ``Config(user_agent_extra=...)`` /
``AWS_SDK_UA_APP_ID`` — lives in a separate adapter, which calls this guard on
the resolved configuration **before** dispatching the request to Bedrock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.domain.config import USER_AGENT_APN
from src.domain.models import ErrorDetail

#: Error code reported when the mandatory billing annotation is missing/incorrect
#: on an LLM call (Req 8.5).
BILLING_ERROR_CODE: str = "missing_user_agent_apn"

#: User-facing/operator-facing error message reported when the call is blocked.
#: It names the annotation that must be present without leaking any other detail.
BILLING_ERROR_MESSAGE: str = (
    "Chamada de LLM bloqueada: o User_Agent_APN obrigatorio "
    f"'{USER_AGENT_APN}' esta ausente ou incorreto na configuracao."
)

#: Default configuration key under which the user-agent annotation is carried.
#: Mirrors the ``user_agent_extra`` argument of the Bedrock client ``Config``
#: used by the adapter layer (see design "Anotação de billing").
DEFAULT_USER_AGENT_KEY: str = "user_agent_extra"


@dataclass(frozen=True)
class BillingGuardResult:
    """Outcome of the mandatory billing-annotation guard (Req 8.3, 8.5).

    Attributes:
        allowed: ``True`` **if and only if** the configuration carries the
            ``User_Agent_APN`` with a value exactly equal to
            :data:`~src.domain.config.USER_AGENT_APN`. When ``True`` the LLM call
            may proceed; when ``False`` the call MUST be blocked.
        error: The structured :class:`ErrorDetail` reporting the missing/incorrect
            annotation when the call is blocked, or ``None`` when allowed.
    """

    allowed: bool
    error: ErrorDetail | None


def billing_error() -> ErrorDetail:
    """Return the structured error reported when the annotation is missing (Req 8.5)."""
    return ErrorDetail(code=BILLING_ERROR_CODE, message=BILLING_ERROR_MESSAGE)


def guard_llm_call(
    config: Mapping[str, Any] | None,
    *,
    user_agent_key: str = DEFAULT_USER_AGENT_KEY,
) -> BillingGuardResult:
    """Guard a Bedrock LLM call against the mandatory billing annotation.

    The call is permitted to proceed **if and only if** ``config`` contains the
    ``user_agent_key`` entry with a value **exactly** equal to
    :data:`~src.domain.config.USER_AGENT_APN`
    (``APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$``). If the entry is absent, ``None``,
    or holds any other value, the call is blocked and a structured error
    reporting the missing/incorrect ``User_Agent_APN`` is returned
    (Req 8.3, 8.5, Property 17).

    The comparison is exact: partial matches, substrings, surrounding whitespace
    or a different case all fail the guard, since Bedrock billing attribution
    requires the annotation verbatim.

    Args:
        config: The resolved Bedrock client configuration mapping (or ``None``
            when no configuration was supplied). The value under
            ``user_agent_key`` is the user-agent annotation to be validated.
        user_agent_key: The configuration key that carries the annotation.
            Defaults to :data:`DEFAULT_USER_AGENT_KEY` (``"user_agent_extra"``).

    Returns:
        A :class:`BillingGuardResult` with ``allowed=True`` and no error when the
        annotation is present and exact; otherwise ``allowed=False`` with a
        populated ``error``.
    """
    if config is None:
        return BillingGuardResult(allowed=False, error=billing_error())

    value = config.get(user_agent_key)
    if isinstance(value, str) and value == USER_AGENT_APN:
        return BillingGuardResult(allowed=True, error=None)

    return BillingGuardResult(allowed=False, error=billing_error())
