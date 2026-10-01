"""Fail-closed audit classification and decision (pure-logic layer).

Implements the pure-logic that decides whether an action performed on AWS
resources is *relevant for auditing* and, when it is, enforces the **fail-closed**
behaviour required when AWS CloudTrail is unavailable.

Per the design (Property 20) and Requirements 10.6, 10.7 and 10.8:

* An action is auditable **if and only if** it belongs to one of the defined
  observable categories (Req 10.7):
    - authentication and access authorization (both success and failure),
    - reading/querying institutional or user data,
    - creation, alteration or deletion of data,
    - configuration or permission changes,
    - access to credentials in AWS Secrets Manager.
* When an auditable action is requested while CloudTrail is unavailable, the
  execution is **blocked**, the data state is **preserved unchanged**, and a
  structured **blocking warning** is returned to the caller (Req 10.8).
* Non-auditable actions are unaffected by CloudTrail availability and are allowed
  to proceed.

This module is intentionally free of any I/O so it can be exercised by
property-based tests (Property 20). The actual CloudTrail emission and IAM
permission enforcement live in separate adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AuditCategory(str, Enum):
    """Observable categories of actions relevant for auditing (Req 10.7)."""

    #: Autenticação e autorização de acesso (sucesso e falha).
    AUTH = "auth"
    #: Leitura ou consulta de dados institucionais ou de usuários.
    DATA_READ = "data_read"
    #: Criação, alteração ou exclusão de dados.
    DATA_WRITE = "data_write"
    #: Alterações de configuração ou de permissões.
    CONFIG_CHANGE = "config_change"
    #: Acesso a credenciais no AWS Secrets Manager.
    SECRET_ACCESS = "secret_access"


#: Set of all audit-relevant category values for quick membership checks (Req 10.7).
AUDITABLE_CATEGORIES: frozenset[str] = frozenset(c.value for c in AuditCategory)

#: User-facing warning returned when an auditable action is blocked because the
#: audit trail (CloudTrail) is unavailable (Req 10.8). Kept free of internal or
#: technical detail per the sanitization principle (Req 10.4).
AUDIT_BLOCKED_WARNING: str = (
    "A operação foi bloqueada por indisponibilidade do registro de auditoria. "
    "Nenhuma alteração foi realizada. Por favor, tente novamente mais tarde."
)


def is_auditable(category: Any) -> bool:
    """Return whether an action category is relevant for auditing (Req 10.7).

    An action is auditable **if and only if** its ``category`` belongs to one of
    the defined observable categories (:class:`AuditCategory`). Any other value
    (including ``None`` or an unknown string) is treated as non-auditable.

    Args:
        category: The action category. Accepts an :class:`AuditCategory` member
            or its string value; any other value is considered non-auditable.

    Returns:
        ``True`` if the category is one of the defined audit categories,
        otherwise ``False``.
    """
    if isinstance(category, AuditCategory):
        return True
    if isinstance(category, str):
        return category in AUDITABLE_CATEGORIES
    return False


@dataclass(frozen=True)
class AuditDecision:
    """Structured outcome of a fail-closed audit decision (Req 10.6, 10.8).

    Attributes:
        allowed: ``True`` when the action may proceed; ``False`` when it is
            blocked. An auditable action is blocked whenever CloudTrail is
            unavailable (Req 10.8).
        auditable: Whether the action was classified as relevant for auditing
            (Req 10.7).
        state_preserved: ``True`` when the data state is guaranteed unchanged.
            Always ``True`` for a blocked action (nothing is executed) and for a
            non-auditable action that this decision does not itself mutate.
        warning: A sanitized, user-facing blocking warning when
            ``allowed`` is ``False``; otherwise ``None`` (Req 10.8).
    """

    allowed: bool
    auditable: bool
    state_preserved: bool
    warning: str | None = None


def decide_audit(category: Any, cloudtrail_available: bool) -> AuditDecision:
    """Decide whether an action may proceed under fail-closed auditing (Req 10.8).

    The action is **blocked if and only if** it is auditable (Req 10.7) **and**
    CloudTrail is unavailable. When blocked, the data state is preserved unchanged
    and a structured blocking warning is returned (Req 10.8). In every other case
    the action is allowed to proceed:

    * a non-auditable action is unaffected by CloudTrail availability;
    * an auditable action proceeds when CloudTrail is available.

    Args:
        category: The action category (see :func:`is_auditable`).
        cloudtrail_available: ``True`` when AWS CloudTrail is available to record
            the audit event; ``False`` when it is unavailable.

    Returns:
        An :class:`AuditDecision` describing whether the action is allowed,
        whether it was auditable, whether the data state is preserved, and the
        blocking warning when applicable.
    """
    auditable = is_auditable(category)

    if auditable and not cloudtrail_available:
        # Fail-closed: block execution, preserve state, return a blocking warning.
        return AuditDecision(
            allowed=False,
            auditable=True,
            state_preserved=True,
            warning=AUDIT_BLOCKED_WARNING,
        )

    return AuditDecision(
        allowed=True,
        auditable=auditable,
        state_preserved=True,
        warning=None,
    )
