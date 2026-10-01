"""Property-based test for fail-closed auditing of relevant actions.

Feature: assistente-progep-ifs, Property 20: Auditoria fail-closed de ações relevantes

For any ação executada sobre recursos AWS, a ação é classificada como relevante
para auditoria se e somente se pertence a uma das categorias definidas (auth,
data_read, data_write, config_change, secret_access); e quando uma ação
auditável é solicitada com o CloudTrail indisponível, a execução é bloqueada, o
estado dos dados é preservado e um aviso de bloqueio é retornado.

Validates: Requirements 10.7, 10.8
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.domain.audit import (
    AUDITABLE_CATEGORIES,
    AuditCategory,
    decide_audit,
    is_auditable,
)

pytestmark = pytest.mark.property


# Category inputs span the whole input space:
#   * AuditCategory enum members (auditable),
#   * their string values (auditable),
#   * arbitrary strings that are almost never a valid category (non-auditable),
#   * None (non-auditable).
_category_strategy = st.one_of(
    st.sampled_from(list(AuditCategory)),
    st.sampled_from([c.value for c in AuditCategory]),
    st.text(max_size=30),
    st.none(),
)


def _expected_auditable(category: object) -> bool:
    """Reference oracle for the auditable classification (Req 10.7)."""
    if isinstance(category, AuditCategory):
        return True
    if isinstance(category, str):
        return category in AUDITABLE_CATEGORIES
    return False


@given(category=_category_strategy)
def test_is_auditable_iff_defined_category(category: object) -> None:
    """is_auditable is True iff the category is one of the defined categories.

    Feature: assistente-progep-ifs, Property 20: Auditoria fail-closed de ações relevantes
    Validates: Requirements 10.7
    """
    assert is_auditable(category) == _expected_auditable(category)


@given(category=_category_strategy, cloudtrail_available=st.booleans())
def test_decide_audit_fail_closed(
    category: object,
    cloudtrail_available: bool,
) -> None:
    """An auditable action is blocked iff CloudTrail is unavailable.

    Feature: assistente-progep-ifs, Property 20: Auditoria fail-closed de ações relevantes
    Validates: Requirements 10.7, 10.8

    When blocked, the data state is preserved and a blocking warning is returned;
    in every other case the action is allowed to proceed.
    """
    decision = decide_audit(category, cloudtrail_available)
    auditable = _expected_auditable(category)

    # The decision reports the same classification as is_auditable (Req 10.7).
    assert decision.auditable == auditable

    should_block = auditable and not cloudtrail_available

    if should_block:
        # Fail-closed: blocked, state preserved, blocking warning present (Req 10.8).
        assert decision.allowed is False
        assert decision.state_preserved is True
        assert decision.warning is not None
        assert decision.warning != ""
    else:
        # Allowed to proceed; no blocking warning.
        assert decision.allowed is True
        assert decision.warning is None

    # The data state is never mutated by this pure decision.
    assert decision.state_preserved is True
