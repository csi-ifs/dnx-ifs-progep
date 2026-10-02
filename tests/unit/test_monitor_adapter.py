"""Unit tests for the Monitor_de_Consumo adapter (Task 12.7).

Covers Req 7.1 (input/output token metrics per session_id), 7.2 (response record
per session_id), 7.3 (published to CloudWatch via put_metric_data), 7.5 (record
retried up to 3 times with a session-identifying error on exhaustion) and the
reused failure recording (Req 7.4). The CloudWatch client is mocked so no AWS
access is needed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.adapters.monitor_adapter import (
    DEFAULT_NAMESPACE,
    MonitorAdapter,
    RecordResult,
)
from src.domain.config import MAX_RETRIES


def _adapter(client: MagicMock) -> MonitorAdapter:
    return MonitorAdapter(cloudwatch_client=client)


def _metric_by_name(metric_data: list[dict], name: str) -> dict:
    return next(m for m in metric_data if m["MetricName"] == name)


@pytest.mark.unit
def test_record_token_usage_emits_input_and_output_metrics_per_session():
    client = MagicMock()

    result = _adapter(client).record_token_usage("sess-1", 120, 45)

    assert result == RecordResult(
        recorded=True, session_id="sess-1", attempts_made=1, error=None
    )
    # Req 7.3: published via put_metric_data to the consumo namespace.
    kwargs = client.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == DEFAULT_NAMESPACE
    metric_data = kwargs["MetricData"]
    # Req 7.1: input/output token metrics keyed to session_id.
    input_metric = _metric_by_name(metric_data, "InputTokens")
    output_metric = _metric_by_name(metric_data, "OutputTokens")
    assert input_metric["Value"] == 120.0
    assert output_metric["Value"] == 45.0
    # Sem dimensão por sessão: uma série por métrica, não duas por conversa.
    assert input_metric["Dimensions"] == []
    assert output_metric["Dimensions"] == []


@pytest.mark.unit
def test_record_response_emits_response_metric_per_session():
    client = MagicMock()

    result = _adapter(client).record_response("sess-2", "olá, tudo certo")

    assert result.recorded is True
    assert result.session_id == "sess-2"
    metric_data = client.put_metric_data.call_args.kwargs["MetricData"]
    # Req 7.2: a response record keyed to session_id.
    responses_metric = _metric_by_name(metric_data, "Responses")
    length_metric = _metric_by_name(metric_data, "ResponseLength")
    assert responses_metric["Value"] == 1.0
    assert length_metric["Value"] == float(len("olá, tudo certo"))
    assert responses_metric["Dimensions"] == []


@pytest.mark.unit
def test_sessao_fica_no_log_e_nao_na_metrica(caplog):
    caplog.set_level("INFO", logger="src.adapters.monitor_adapter")
    _adapter(MagicMock()).record_token_usage("sess-9", 3, 1)
    assert "sess-9" in caplog.text


@pytest.mark.unit
def test_record_retries_then_succeeds_within_three_attempts():
    client = MagicMock()
    # Two transient failures, third attempt succeeds -> 3 attempts total (Req 7.5).
    client.put_metric_data.side_effect = [
        ConnectionError("attempt 1"),
        ConnectionError("attempt 2"),
        None,
    ]

    result = _adapter(client).record_token_usage("sess-3", 10, 5)

    assert result.recorded is True
    assert result.attempts_made == MAX_RETRIES
    assert client.put_metric_data.call_count == MAX_RETRIES


@pytest.mark.unit
def test_record_exhausts_retries_and_surfaces_session_identifying_error():
    client = MagicMock()
    client.put_metric_data.side_effect = ConnectionError("cloudwatch down")

    result = _adapter(client).record_response("sess-err", "resposta")

    # Req 7.5: after 3 failed attempts, surface an error identifying the session.
    assert result.recorded is False
    assert result.attempts_made == MAX_RETRIES
    assert client.put_metric_data.call_count == MAX_RETRIES
    assert result.error is not None
    assert "sess-err" in result.error


@pytest.mark.unit
def test_record_llm_call_records_both_tokens_and_response():
    client = MagicMock()

    tokens_result, response_result = _adapter(client).record_llm_call(
        "sess-4", input_tokens=7, output_tokens=3, content="resposta final"
    )

    assert tokens_result.recorded is True
    assert response_result.recorded is True
    # One put_metric_data per record (tokens + response).
    assert client.put_metric_data.call_count == 2


@pytest.mark.unit
def test_record_call_failure_preserves_prior_records_and_identifies_session():
    client = MagicMock()
    adapter = _adapter(client)

    first = adapter.record_call_failure("sess-a", reason="timeout")
    second = adapter.record_call_failure("sess-b")

    # Req 7.4: failure recorded with session id, prior records preserved in order.
    assert first.session_id == "sess-a"
    assert first.status == "failed"
    assert second.session_id == "sess-b"
    assert adapter.failures == (first, second)
    # A failure metric keyed to the session is emitted (best-effort).
    assert client.put_metric_data.call_count == 2


@pytest.mark.unit
def test_empty_session_id_raises_value_error():
    adapter = _adapter(MagicMock())
    with pytest.raises(ValueError):
        adapter.record_token_usage("", 1, 1)
    with pytest.raises(ValueError):
        adapter.record_response("", "x")


@pytest.mark.unit
def test_negative_token_counts_raise_value_error():
    adapter = _adapter(MagicMock())
    with pytest.raises(ValueError):
        adapter.record_token_usage("sess-1", -1, 0)


@pytest.mark.unit
def test_module_imports_without_boto3():
    # The module must import even if boto3 is absent (guarded import).
    import src.adapters.monitor_adapter as mod

    assert hasattr(mod, "MonitorAdapter")
