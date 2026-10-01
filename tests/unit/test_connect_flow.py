"""Testes do contact flow do Connect e do handler invocado por ele."""

from __future__ import annotations

import json

import pytest

from src.infra.connect_flow import (
    FLOW_VERSION,
    build_contact_flow,
    build_transfer_flow,
    contact_flow_content,
    transfer_flow_content,
)

pytestmark = pytest.mark.unit

LAMBDA_ARN = "arn:aws:lambda:us-east-1:123456789012:function:progep-ifs-connect"
QUEUE_ARN = "arn:aws:connect:us-east-1:123456789012:instance/i-1/queue/q-1"


def _flow():
    return build_contact_flow(lambda_arn=LAMBDA_ARN, queue_arn=QUEUE_ARN)


def _actions_by_id(flow):
    return {a["Identifier"]: a for a in flow["Actions"]}


def test_flow_has_version_and_valid_start_action():
    flow = _flow()
    assert flow["Version"] == FLOW_VERSION
    assert flow["StartAction"] in _actions_by_id(flow)


def test_every_transition_points_to_an_existing_action():
    """Um fluxo com transição órfã é aceito pelo Pulumi mas quebra no Connect."""
    flow = _flow()
    ids = set(_actions_by_id(flow))
    for action in flow["Actions"]:
        t = action.get("Transitions", {})
        targets = []
        if "NextAction" in t:
            targets.append(t["NextAction"])
        targets += [e["NextAction"] for e in t.get("Errors", [])]
        targets += [c["NextAction"] for c in t.get("Conditions", [])]
        for target in targets:
            assert target in ids, f"{action['Identifier']} aponta para {target} inexistente"


def test_every_transition_block_has_errors_and_conditions():
    """O Connect recusa o fluxo (InvalidContactFlowException sem mensagem) se
    ``Errors``/``Conditions`` faltarem — foi exatamente o que quebrou o deploy."""
    for action in _flow()["Actions"]:
        t = action["Transitions"]
        if not t:  # DisconnectParticipant tem Transitions vazio
            continue
        assert "Errors" in t, f"{action['Identifier']} sem Errors"
        assert "Conditions" in t, f"{action['Identifier']} sem Conditions"


def test_get_input_declares_input_validation_and_valid_error_types():
    """O serviço exige ``InputValidation`` e rejeita ``InputTimeLimitExceeded``
    como ErrorType — ambos derrubaram o CreateContactFlow."""
    get_input = next(a for a in _flow()["Actions"] if a["Type"] == "GetParticipantInput")
    assert "InputValidation" in get_input["Parameters"]
    assert get_input["Parameters"]["InputValidation"]["CustomValidation"]["MaximumLength"] == "4000"
    error_types = {e["ErrorType"] for e in get_input["Transitions"]["Errors"]}
    assert "InputTimeLimitExceeded" not in error_types


def test_transfer_to_queue_has_no_parameters_key():
    """``TransferContactToQueue`` não aceita ``Parameters`` (schema do serviço)."""
    transfer = next(a for a in _flow()["Actions"] if a["Type"] == "TransferContactToQueue")
    assert "Parameters" not in transfer


def test_flow_invokes_the_agent_lambda_with_captured_input():
    flow = _flow()
    invoke = next(a for a in flow["Actions"] if a["Type"] == "InvokeLambdaFunction")
    assert invoke["Parameters"]["LambdaFunctionARN"] == LAMBDA_ARN
    # O schema não permite mapear parâmetros arbitrários: a entrada vai por atributo.
    assert "LambdaInvocationAttributes" not in invoke["Parameters"]
    store = next(a for a in flow["Actions"] if a["Type"] == "UpdateContactAttributes")
    assert store["Parameters"]["Attributes"]["text"] == "$.StoredCustomerInput"
    # e o atributo é gravado antes da invocação
    assert store["Transitions"]["NextAction"] == invoke["Identifier"]


def test_answered_delivers_content_and_loops_back_for_next_question():
    flow = _flow()
    by_id = _actions_by_id(flow)
    check = next(a for a in flow["Actions"] if a["Type"] == "Compare")
    answered = next(
        c["NextAction"] for c in check["Transitions"]["Conditions"]
        if c["Condition"]["Operands"] == ["answered"]
    )
    answer_action = by_id[answered]
    assert answer_action["Parameters"]["Text"] == "$.External.content"
    # conversa contínua: volta a capturar entrada
    assert by_id[answer_action["Transitions"]["NextAction"]]["Type"] == "GetParticipantInput"


def test_non_answered_status_escalates_to_human_queue():
    flow = _flow()
    by_id = _actions_by_id(flow)
    check = next(a for a in flow["Actions"] if a["Type"] == "Compare")
    # default (não-answered) leva à mensagem de escalonamento -> fila -> transferência
    step = by_id[check["Transitions"]["NextAction"]]
    assert step["Type"] == "MessageParticipant"
    set_queue = by_id[step["Transitions"]["NextAction"]]
    assert set_queue["Type"] == "UpdateContactTargetQueue"
    assert set_queue["Parameters"]["QueueId"] == QUEUE_ARN
    assert by_id[set_queue["Transitions"]["NextAction"]]["Type"] == "TransferContactToQueue"


def test_lambda_failure_also_escalates_fail_safe():
    """Falha na invocação da Lambda não pode deixar o usuário sem atendimento."""
    flow = _flow()
    by_id = _actions_by_id(flow)
    invoke = next(a for a in flow["Actions"] if a["Type"] == "InvokeLambdaFunction")
    err_target = by_id[invoke["Transitions"]["Errors"][0]["NextAction"]]
    assert err_target["Type"] == "MessageParticipant"
    assert by_id[err_target["Transitions"]["NextAction"]]["Type"] == "UpdateContactTargetQueue"


def test_content_is_serializable_json():
    content = contact_flow_content(lambda_arn=LAMBDA_ARN, queue_arn=QUEUE_ARN)
    assert json.loads(content)["Version"] == FLOW_VERSION


# --- Fluxo de transbordo do WhatsApp --------------------------------------------

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
    flow = build_transfer_flow(queue_arn=QUEUE_ARN)
    ids = set(_actions_by_id(flow))
    for action in flow["Actions"]:
        t = action["Transitions"]
        if not t:
            continue
        assert "Errors" in t and "Conditions" in t
        alvos = [t["NextAction"]] + [e["NextAction"] for e in t["Errors"]]
        assert set(alvos) <= ids
    transfer = next(a for a in flow["Actions"] if a["Type"] == "TransferContactToQueue")
    assert "Parameters" not in transfer
    assert json.loads(transfer_flow_content(queue_arn=QUEUE_ARN))["Version"] == FLOW_VERSION


# --- connect_handler ------------------------------------------------------------

def _connect_event(text="Como peço férias?", contact_id="c-1"):
    return {
        "Name": "ContactFlowEvent",
        "Details": {
            "ContactData": {"ContactId": contact_id, "Channel": "CHAT", "Attributes": {}},
            "Parameters": {"text": text},
        },
    }


def test_connect_handler_returns_flat_string_dict(monkeypatch):
    """O Connect exige resposta plana com valores string — nada aninhado."""
    from src.adapters import lambda_entry
    from src.domain.models import AgentResponse, SourceRef

    monkeypatch.setattr(
        lambda_entry, "_invoke_agent",
        lambda req: AgentResponse(status="answered", content="Via SIGRH.",
                                  sources=(SourceRef(doc_id="d", score=0.9),), confidence=0.9),
    )
    out = lambda_entry.connect_handler(_connect_event())

    assert out["status"] == "answered"
    assert out["content"] == "Via SIGRH."
    assert all(isinstance(v, str) for v in out.values())
    assert all(not isinstance(v, (dict, list)) for v in out.values())


def test_connect_handler_not_found_returns_escalation_text(monkeypatch):
    from src.adapters import lambda_entry
    from src.domain.models import AgentResponse

    monkeypatch.setattr(
        lambda_entry, "_invoke_agent",
        lambda req: AgentResponse(status="not_found", content="", sources=(), confidence=0.0),
    )
    out = lambda_entry.connect_handler(_connect_event())
    assert out["status"] == "not_found"
    assert "atendente" in out["content"].lower()


def test_connect_handler_agent_failure_is_unavailable(monkeypatch):
    from src.adapters import lambda_entry

    def boom(req):
        raise RuntimeError("runtime fora do ar")

    monkeypatch.setattr(lambda_entry, "_invoke_agent", boom)
    out = lambda_entry.connect_handler(_connect_event())
    assert out["status"] == "unavailable"
    assert "atendente" in out["content"].lower()


def test_connect_handler_empty_input_asks_again():
    from src.adapters import lambda_entry

    out = lambda_entry.connect_handler(_connect_event(text=""))
    assert out["status"] == "unavailable"
    assert "repetir" in out["content"].lower()


def test_connect_handler_reads_text_from_contact_attributes():
    from src.adapters import lambda_entry

    event = {"Details": {"ContactData": {"ContactId": "c-9", "Attributes": {"text": "  oi  "}},
                         "Parameters": {}}}
    assert lambda_entry._connect_input_text(event) == "oi"
