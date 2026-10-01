"""Property-based test for least-privilege IAM policies.

Feature: assistente-progep-ifs, Property 21: Políticas IAM sem privilégios
excessivos.

**Validates: Requirements 10.2**

Property 21 states: *For any* statement de política IAM gerado para os
componentes da solução, nem ``Action`` nem ``Resource`` contêm curinga amplo
(``*``) e nenhuma política concede permissões de administrador.

Two complementary angles are exercised:

(a) **Validator semantics** — over arbitrary ``Allow`` statements built from a
    mix of broad (``"*"`` / ``"service:*"``) and scoped tokens,
    :func:`~src.domain.iam.validate_statement` reports the statement compliant
    **iff** no ``Action``/``Resource`` is a broad wildcard and the statement does
    not grant admin.

(b) **Generated solution policies** — every policy returned by
    :func:`src.infra.resources.build_component_policies`, across arbitrary
    :class:`~src.infra.resources.ArnContext` values, is compliant: no broad
    wildcards in any ``Action`` or ``Resource`` and no admin grant.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.iam import (
    EFFECT_ALLOW,
    FULL_WILDCARD,
    IamStatement,
    grants_admin,
    is_broad_action_wildcard,
    is_broad_resource_wildcard,
    validate_statement,
)
from src.infra.resources import ArnContext, build_component_policies

pytestmark = pytest.mark.property


# --- Token strategies -----------------------------------------------------------

# Scoped actions restrict the action set: either a fully qualified action or a
# narrow prefix wildcard such as "s3:Get*". Neither is a *broad* wildcard.
_SERVICE_NAMES = st.sampled_from(
    ["s3", "dynamodb", "bedrock", "logs", "lambda", "secretsmanager", "connect"]
)
_ACTION_VERBS = st.sampled_from(
    ["GetObject", "PutItem", "InvokeModel", "Retrieve", "CreateLogStream", "Get*"]
)

_scoped_actions = st.builds(lambda s, v: f"{s}:{v}", _SERVICE_NAMES, _ACTION_VERBS)
# Broad actions: the all-actions token or a service-wide wildcard.
_broad_actions = st.one_of(
    st.just(FULL_WILDCARD),
    st.builds(lambda s: f"{s}:*", _SERVICE_NAMES),
)
_actions = st.one_of(_scoped_actions, _broad_actions)

# Scoped resources target a named ARN (possibly with an object-level suffix
# wildcard). The all-resources "*" is the only broad resource token.
_scoped_resources = st.sampled_from(
    [
        "arn:aws:s3:::progep-ifs-documents",
        "arn:aws:s3:::progep-ifs-documents/*",
        "arn:aws:dynamodb:us-east-1:123456789012:table/sessions",
        "arn:aws:bedrock:us-east-1:123456789012:knowledge-base/KB123",
        "arn:aws:logs:us-east-1:123456789012:log-group:/progep-ifs/*:*",
    ]
)
_broad_resources = st.just(FULL_WILDCARD)
_resources = st.one_of(_scoped_resources, _broad_resources)


@st.composite
def _allow_statements(draw: st.DrawFn) -> IamStatement:
    """Generate an arbitrary ``Allow`` statement with a non-empty action/resource set."""
    actions = tuple(draw(st.lists(_actions, min_size=1, max_size=4)))
    resources = tuple(draw(st.lists(_resources, min_size=1, max_size=4)))
    sid = draw(st.one_of(st.none(), st.text(alphabet=st.characters(min_codepoint=65, max_codepoint=90), min_size=1, max_size=6)))
    return IamStatement(effect=EFFECT_ALLOW, actions=actions, resources=resources, sid=sid)


# --- Angle (a): validator semantics --------------------------------------------


@given(statement=_allow_statements())
def test_validate_statement_compliant_iff_no_broad_wildcard_and_not_admin(
    statement: IamStatement,
) -> None:
    """validate_statement is compliant iff no broad wildcard and not admin (Req 10.2)."""
    has_broad_action = any(is_broad_action_wildcard(a) for a in statement.actions)
    has_broad_resource = any(is_broad_resource_wildcard(r) for r in statement.resources)
    expected_compliant = not (has_broad_action or has_broad_resource or grants_admin(statement))

    result = validate_statement(statement)

    assert result.compliant is expected_compliant
    if expected_compliant:
        # A compliant statement must carry no violation reasons.
        assert result.reasons == ()
    else:
        # A non-compliant statement must justify itself (Req 10.2 traceability).
        assert result.reasons

    # A broad wildcard, when present, must never be reported as compliant.
    if has_broad_action or has_broad_resource:
        assert not result.compliant
    # Admin grants must never be reported as compliant.
    if grants_admin(statement):
        assert not result.compliant


# --- Angle (b): generated solution policies ------------------------------------

_arn_segment = st.sampled_from(["*", "us-east-1", "sa-east-1", "eu-west-1"])
_account_ids = st.sampled_from(["*", "123456789012", "000000000000", "999999999999"])
_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-"),
    min_size=1,
    max_size=20,
)


@st.composite
def _arn_contexts(draw: st.DrawFn) -> ArnContext:
    """Generate arbitrary ArnContext values covering wildcard and named segments."""
    return ArnContext(
        region=draw(_arn_segment),
        account_id=draw(_account_ids),
        documents_bucket=draw(_names),
        session_table=draw(_names),
        knowledge_base_id=draw(st.one_of(st.just("*"), _names)),
        connect_instance_id=draw(st.one_of(st.just("*"), _names)),
    )


@given(ctx=_arn_contexts())
def test_generated_component_policies_are_least_privilege(ctx: ArnContext) -> None:
    """Every generated component policy has no broad wildcards and no admin (Req 10.2)."""
    policies = build_component_policies(ctx)
    assert policies, "expected at least one component policy"

    for key, policy in policies.items():
        for index, statement in enumerate(policy.statements):
            result = validate_statement(statement)
            assert result.compliant, (
                f"{key} statement[{index}] not least-privilege: {result.reasons}"
            )
            # No Action is a broad wildcard.
            for action in statement.actions:
                assert not is_broad_action_wildcard(action), (
                    f"{key} statement[{index}] has broad Action {action!r}"
                )
            # No Resource is a broad wildcard.
            for resource in statement.resources:
                assert not is_broad_resource_wildcard(resource), (
                    f"{key} statement[{index}] has broad Resource {resource!r}"
                )
            # No statement grants administrator access.
            assert not grants_admin(statement), (
                f"{key} statement[{index}] grants admin"
            )
