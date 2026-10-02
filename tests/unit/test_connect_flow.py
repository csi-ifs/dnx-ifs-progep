"""Testes do contact flow de transbordo do WhatsApp."""

from __future__ import annotations

import json

import pytest

from src.infra.connect_flow import FLOW_VERSION, build_transfer_flow, transfer_flow_content

pytestmark = pytest.mark.unit

QUEUE_ARN = "arn:aws:connect:us-east-1:123456789012:instance/i-1/queue/q-1"


def _actions_by_id(flow):
    return {a["Identifier"]: a for a in flow["Actions"]}


def test_transfer_flow_only_queues_without_calling_the_agent():
    """Quem pediu uma pessoa não pode ouvir de novo "Faça sua pergunta"."""
    flow = build_transfer_flow(queue_arn=QUEUE_ARN)
    tipos = [a["Type"] for a in flow["Actions"]]

    assert "InvokeLambdaFunction" not in tipos
    assert "GetParticipantInput" not in tipos
    # Mensagens do fluxo iriam pelo streaming como SYSTEM, que a ponte descarta.
    assert "MessageParticipant" not in tipos

    by_id = _actions_by_id(flow)
    inicio = by_id[flow["StartAction"]]
    assert inicio["Type"] == "UpdateContactTargetQueue"
    assert inicio["Parameters"]["QueueId"] == QUEUE_ARN
    assert by_id[inicio["Transitions"]["NextAction"]]["Type"] == "TransferContactToQueue"


def test_transfer_flow_transitions_are_valid():
    """Transição órfã é aceita pelo Pulumi mas quebra no Connect; e o serviço
    recusa o fluxo (InvalidContactFlowException sem mensagem) se ``Errors`` ou
    ``Conditions`` faltarem — foi exatamente o que quebrou o primeiro deploy."""
    flow = build_transfer_flow(queue_arn=QUEUE_ARN)
    ids = set(_actions_by_id(flow))
    for action in flow["Actions"]:
        t = action["Transitions"]
        if not t:  # DisconnectParticipant tem Transitions vazio
            continue
        assert "Errors" in t and "Conditions" in t
        alvos = [t["NextAction"]] + [e["NextAction"] for e in t["Errors"]]
        assert set(alvos) <= ids


def test_transfer_to_queue_has_no_parameters_key():
    """``TransferContactToQueue`` não aceita ``Parameters`` (schema do serviço)."""
    flow = build_transfer_flow(queue_arn=QUEUE_ARN)
    transfer = next(a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue")
    assert "Parameters" not in transfer


def test_content_is_serializable_json():
    assert json.loads(transfer_flow_content(queue_arn=QUEUE_ARN))["Version"] == FLOW_VERSION
