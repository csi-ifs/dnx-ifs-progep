"""Property-based test for the mandatory billing annotation on LLM calls.

Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM

For any parâmetros de chamada de LLM, a chamada ao Amazon Bedrock prossegue se e
somente se a configuração inclui o ``User_Agent_APN`` com valor exatamente igual
a ``APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$``; se ausente ou diferente, a chamada é
bloqueada e um erro indicando a ausência é reportado.

Validates: Requirements 8.3, 8.5
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.domain.billing import (
    BILLING_ERROR_CODE,
    DEFAULT_USER_AGENT_KEY,
    USER_AGENT_APN,
    guard_llm_call,
)

pytestmark = pytest.mark.property


# --- Value strategies -----------------------------------------------------------

# The one and only value that must let the call proceed.
_exact_value = st.just(USER_AGENT_APN)

# Values that resemble the target but are NOT exactly equal: substrings, wrong
# case, surrounding whitespace, empty string, plausible-but-wrong annotations,
# and arbitrary text/non-string junk. None of these may pass the guard.
_wrong_values = st.one_of(
    st.just(""),
    st.just(USER_AGENT_APN.lower()),
    st.just(USER_AGENT_APN.upper()),
    st.just(f" {USER_AGENT_APN}"),
    st.just(f"{USER_AGENT_APN} "),
    st.just(f"\t{USER_AGENT_APN}\n"),
    st.just(USER_AGENT_APN[:-1]),
    st.just(USER_AGENT_APN[1:]),
    st.just(USER_AGENT_APN + "extra"),
    st.just("APN_1.1/pc_wrongidentifier$"),
    st.none(),
    st.integers(),
    st.text(max_size=80).filter(lambda s: s != USER_AGENT_APN),
)


def _config_with_value(value: Any, key: str) -> dict[str, Any]:
    """Build a config mapping carrying ``value`` under ``key`` plus noise keys."""
    config: dict[str, Any] = {"model_id": "anthropic.claude", "region": "us-east-1"}
    # Assign the annotation key last so it is never shadowed by a noise key.
    config[key] = value
    return config


# --- Properties -----------------------------------------------------------------


@settings(max_examples=200)
@given(value=_exact_value)
def test_exact_annotation_allows_call(value: str) -> None:
    """A config with the exact User_Agent_APN lets the LLM call proceed.

    Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM
    Validates: Requirements 8.3, 8.5
    """
    config = _config_with_value(value, DEFAULT_USER_AGENT_KEY)
    result = guard_llm_call(config)

    assert result.allowed is True
    assert result.error is None


@settings(max_examples=200)
@given(value=_wrong_values)
def test_wrong_or_absent_annotation_blocks_call(value: Any) -> None:
    """Any value other than the exact annotation blocks the call with an error.

    Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM
    Validates: Requirements 8.3, 8.5
    """
    config = _config_with_value(value, DEFAULT_USER_AGENT_KEY)
    result = guard_llm_call(config)

    assert result.allowed is False
    assert result.error is not None
    assert result.error.code == BILLING_ERROR_CODE


@settings(max_examples=200)
@given(
    value=st.one_of(_exact_value, _wrong_values),
    include_key=st.booleans(),
)
def test_allowed_iff_value_matches_exactly(value: Any, include_key: bool) -> None:
    """allowed is True iff the config value equals USER_AGENT_APN exactly.

    Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM
    Validates: Requirements 8.3, 8.5

    Covers both present-key configs (with matching or wrong values) and the
    absent-key case, asserting the biconditional between an exact match and the
    call being permitted, and the reported error code otherwise.
    """
    if include_key:
        config: dict[str, Any] = _config_with_value(value, DEFAULT_USER_AGENT_KEY)
        expected_allowed = value == USER_AGENT_APN
    else:
        # Key absent entirely: annotation is missing -> must be blocked.
        config = {"model_id": "anthropic.claude", "region": "us-east-1"}
        expected_allowed = False

    result = guard_llm_call(config)

    assert result.allowed is expected_allowed
    if expected_allowed:
        assert result.error is None
    else:
        assert result.error is not None
        assert result.error.code == BILLING_ERROR_CODE


def test_none_config_blocks_call() -> None:
    """A None config carries no annotation and blocks the call.

    Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM
    Validates: Requirements 8.3, 8.5
    """
    result = guard_llm_call(None)

    assert result.allowed is False
    assert result.error is not None
    assert result.error.code == BILLING_ERROR_CODE


@settings(max_examples=100)
@given(value=st.one_of(_exact_value, _wrong_values), key=st.text(min_size=1, max_size=20))
def test_custom_user_agent_key_is_respected(value: Any, key: str) -> None:
    """The guard reads the annotation from the configured user_agent_key.

    Feature: assistente-progep-ifs, Property 17: Anotação obrigatória de billing nas chamadas de LLM
    Validates: Requirements 8.3, 8.5
    """
    config = _config_with_value(value, key)
    result = guard_llm_call(config, user_agent_key=key)

    expected_allowed = value == USER_AGENT_APN
    assert result.allowed is expected_allowed
    if not expected_allowed:
        assert result.error is not None
        assert result.error.code == BILLING_ERROR_CODE
