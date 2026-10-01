"""Least-privilege IAM policy generation and validation.

Pure-logic module implementing the guardrails required by Requirement 10.2 and
Correctness Property 21: any IAM policy statement generated for the solution's
components must grant **only** the actions and resources needed for an
operation, without administrator permissions and without broad wildcards
(e.g. ``"*"``).

The module offers two complementary halves:

* **Validator** — :func:`validate_statement` / :func:`validate_policy` inspect a
  statement (or a whole policy) and report whether it is compliant, returning a
  non-empty list of reasons whenever it is not.
* **Generator** — :func:`build_statement` / :func:`build_policy` construct
  ``Allow`` statements and raise :class:`ExcessivePrivilegeError` if the result
  would violate least-privilege, guaranteeing that every generated statement is
  compliant by construction. :func:`to_policy_document` renders a compliant
  policy to a standard AWS IAM policy document ``dict``.

Design notes
------------
* A **broad wildcard** in an ``Action`` is either the all-actions token ``"*"``
  or a service-wide token such as ``"s3:*"``. Narrower, scoped wildcards such as
  ``"s3:Get*"`` are permitted because they still restrict the action set.
* A **broad wildcard** in a ``Resource`` is the all-resources token ``"*"``.
  Scoped ARN patterns such as ``"arn:aws:s3:::my-bucket/*"`` are permitted
  because they target a specific, named resource.
* A statement **grants admin** when it allows all actions on all resources
  (``Action "*"`` and ``Resource "*"``), i.e. the ``AdministratorAccess``
  equivalent.
* Only ``Allow`` statements are subject to the least-privilege checks. ``Deny``
  statements may legitimately use wildcards because they *restrict* access, and
  a broad ``Deny`` improves security rather than weakening it.

This module performs no I/O so it can be exercised by property-based tests
(Property 21).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# --- Effects --------------------------------------------------------------------
#: IAM effect that grants access; subject to least-privilege checks.
EFFECT_ALLOW: str = "Allow"
#: IAM effect that restricts access; wildcards here are safe and allowed.
EFFECT_DENY: str = "Deny"

#: The all-actions / all-resources wildcard token.
FULL_WILDCARD: str = "*"

#: Matches a service-wide action wildcard such as ``"s3:*"`` or ``"iam:*"``.
_SERVICE_WILDCARD_RE = re.compile(r"^[^:\s]+:\*$")


@dataclass(frozen=True)
class IamStatement:
    """A single IAM policy statement (Req 10.2).

    ``actions`` and ``resources`` are stored as tuples to keep the statement
    immutable and hashable. Use :func:`normalize_tokens` when constructing from
    a single string or an arbitrary iterable.
    """

    effect: str                       # "Allow" | "Deny"
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    sid: str | None = None


@dataclass(frozen=True)
class IamPolicy:
    """A collection of IAM statements forming a policy."""

    statements: tuple[IamStatement, ...]
    version: str = "2012-10-17"


@dataclass(frozen=True)
class PolicyValidationResult:
    """Outcome of validating a statement or a policy.

    When ``compliant`` is ``False``, ``reasons`` MUST be a non-empty tuple of
    human-readable descriptions of every violation found.
    """

    compliant: bool
    reasons: tuple[str, ...] = ()


class ExcessivePrivilegeError(ValueError):
    """Raised by the generator when a statement would violate least-privilege."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__("; ".join(self.reasons) or "excessive privilege")


def normalize_tokens(value: str | Iterable[str]) -> tuple[str, ...]:
    """Normalize an ``Action``/``Resource`` field to a tuple of tokens.

    Accepts either a single string or any iterable of strings, mirroring the
    fact that IAM allows both scalar and array forms for these fields.
    """
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def is_broad_action_wildcard(action: str) -> bool:
    """Return ``True`` if ``action`` is a broad wildcard (Req 10.2, Property 21).

    Broad means the all-actions token ``"*"`` or a service-wide token such as
    ``"s3:*"``. Scoped wildcards like ``"s3:Get*"`` are **not** broad.
    """
    token = action.strip()
    if token == FULL_WILDCARD:
        return True
    return bool(_SERVICE_WILDCARD_RE.match(token))


def is_broad_resource_wildcard(resource: str) -> bool:
    """Return ``True`` if ``resource`` is the all-resources wildcard ``"*"``.

    Scoped ARN patterns such as ``"arn:aws:s3:::bucket/*"`` target a specific
    named resource and are therefore **not** considered broad.
    """
    return resource.strip() == FULL_WILDCARD


def grants_admin(statement: IamStatement) -> bool:
    """Return ``True`` if the statement grants administrator access.

    Admin is an ``Allow`` of all actions (``"*"``) on all resources (``"*"``),
    i.e. the ``AdministratorAccess`` equivalent (Req 10.2).
    """
    if statement.effect != EFFECT_ALLOW:
        return False
    has_all_actions = any(a.strip() == FULL_WILDCARD for a in statement.actions)
    has_all_resources = any(r.strip() == FULL_WILDCARD for r in statement.resources)
    return has_all_actions and has_all_resources


def validate_statement(statement: IamStatement) -> PolicyValidationResult:
    """Validate a single statement against least-privilege rules (Req 10.2).

    A statement is compliant when, for ``Allow`` statements, neither ``Action``
    nor ``Resource`` contains a broad wildcard and the statement does not grant
    admin. ``Deny`` statements are always compliant. The returned result carries
    a reason for every violation found so callers can report them precisely.
    """
    reasons: list[str] = []

    if not statement.actions:
        reasons.append("statement has no Action")
    if not statement.resources:
        reasons.append("statement has no Resource")

    # Only Allow statements can over-grant; Deny wildcards restrict access.
    if statement.effect == EFFECT_ALLOW:
        if grants_admin(statement):
            reasons.append(
                'statement grants administrator access (Action "*" on Resource "*")'
            )
        for action in statement.actions:
            if is_broad_action_wildcard(action):
                reasons.append(f"Action contains a broad wildcard: {action!r}")
        for resource in statement.resources:
            if is_broad_resource_wildcard(resource):
                reasons.append(f"Resource contains a broad wildcard: {resource!r}")
    elif statement.effect != EFFECT_DENY:
        reasons.append(f"unknown Effect: {statement.effect!r}")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique_reasons = tuple(r for r in reasons if not (r in seen or seen.add(r)))
    return PolicyValidationResult(compliant=not unique_reasons, reasons=unique_reasons)


def validate_policy(policy: IamPolicy) -> PolicyValidationResult:
    """Validate every statement in a policy (Req 10.2, Property 21).

    The policy is compliant only if all of its statements are compliant. Reasons
    from individual statements are aggregated and prefixed with the statement's
    ``Sid`` (or positional index) for traceability.
    """
    reasons: list[str] = []
    for index, statement in enumerate(policy.statements):
        result = validate_statement(statement)
        if not result.compliant:
            label = statement.sid if statement.sid else f"statement[{index}]"
            reasons.extend(f"{label}: {reason}" for reason in result.reasons)
    return PolicyValidationResult(compliant=not reasons, reasons=tuple(reasons))


def build_statement(
    actions: str | Iterable[str],
    resources: str | Iterable[str],
    *,
    effect: str = EFFECT_ALLOW,
    sid: str | None = None,
) -> IamStatement:
    """Build a least-privilege statement, rejecting excessive privilege.

    This is the generator half of the module: it normalizes the inputs, builds
    the :class:`IamStatement`, validates it and raises
    :class:`ExcessivePrivilegeError` if the result would violate least-privilege
    (Req 10.2). Consequently, every statement returned by this function is
    guaranteed compliant (Property 21).

    Raises:
        ExcessivePrivilegeError: if the resulting statement is not compliant.
    """
    statement = IamStatement(
        effect=effect,
        actions=normalize_tokens(actions),
        resources=normalize_tokens(resources),
        sid=sid,
    )
    result = validate_statement(statement)
    if not result.compliant:
        raise ExcessivePrivilegeError(result.reasons)
    return statement


def build_policy(statements: Iterable[IamStatement]) -> IamPolicy:
    """Assemble a policy from statements, rejecting any excessive privilege.

    Raises:
        ExcessivePrivilegeError: if any statement is not compliant.
    """
    policy = IamPolicy(statements=tuple(statements))
    result = validate_policy(policy)
    if not result.compliant:
        raise ExcessivePrivilegeError(result.reasons)
    return policy


def to_policy_document(policy: IamPolicy) -> dict:
    """Render a policy to a standard AWS IAM policy document ``dict``.

    Scalar ``Action``/``Resource`` fields are emitted as strings and multi-value
    fields as lists, matching AWS conventions. The policy is validated first so
    only compliant documents are produced (Req 10.2).

    Raises:
        ExcessivePrivilegeError: if the policy is not compliant.
    """
    result = validate_policy(policy)
    if not result.compliant:
        raise ExcessivePrivilegeError(result.reasons)

    def _field(tokens: tuple[str, ...]) -> str | list[str]:
        return tokens[0] if len(tokens) == 1 else list(tokens)

    statements: list[dict] = []
    for statement in policy.statements:
        entry: dict = {
            "Effect": statement.effect,
            "Action": _field(statement.actions),
            "Resource": _field(statement.resources),
        }
        if statement.sid:
            entry["Sid"] = statement.sid
        statements.append(entry)

    return {"Version": policy.version, "Statement": statements}
