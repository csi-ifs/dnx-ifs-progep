"""Contact flow do Amazon Connect para o Roteador_de_Atendimento.

Gera o documento JSON (linguagem de fluxo do Connect, versão ``2019-10-30``) do
fluxo de atendimento por chat:

1. saudação;
2. captura da pergunta do usuário (*GetParticipantInput*);
3. invocação da Lambda do agente (*InvokeLambdaFunction*), que chama o AgentCore
   Runtime e devolve ``status``/``content``;
4. ramificação pelo ``status`` (*Compare*):
   - ``answered`` → entrega a resposta e volta ao passo 2 (conversa contínua);
   - qualquer outro (``not_found``/``unavailable``) → informa e **transfere para
     a fila de atendimento humano** (Req 2.4, 3.1, 3.2);
5. falha na Lambda → mesma rota de escalonamento (fail-safe).

Manter o fluxo como dado gerado por código (e não desenhado no console) atende ao
Req 9.1: todo recurso definido em IaC, reproduzível e auditável.
"""

from __future__ import annotations

import json
from typing import Any

#: Versão da linguagem de fluxo do Amazon Connect.
FLOW_VERSION: str = "2019-10-30"

# Identificadores estáveis das ações (o Connect exige ids únicos por ação).
_GREETING = "a1000000-0000-4000-8000-000000000001"
_GET_INPUT = "a1000000-0000-4000-8000-000000000002"
_STORE_ATTR = "a1000000-0000-4000-8000-00000000000b"
_INVOKE = "a1000000-0000-4000-8000-000000000003"
_CHECK = "a1000000-0000-4000-8000-000000000004"
_ANSWER = "a1000000-0000-4000-8000-000000000005"
_ESCALATE_MSG = "a1000000-0000-4000-8000-000000000006"
_SET_QUEUE = "a1000000-0000-4000-8000-000000000007"
_TRANSFER = "a1000000-0000-4000-8000-000000000008"
_DISCONNECT = "a1000000-0000-4000-8000-000000000009"
_ERROR_MSG = "a1000000-0000-4000-8000-00000000000a"

#: Prompt exibido ao capturar a pergunta do usuário.
INPUT_PROMPT: str = "Digite sua pergunta:"


def _transitions(
    next_action: str | None = None,
    *,
    errors: list[dict[str, str]] | None = None,
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Monta um bloco ``Transitions`` no formato aceito pelo Connect.

    O serviço espera **sempre** as chaves ``Errors`` e ``Conditions`` (mesmo
    vazias); omiti-las faz o ``CreateContactFlow`` recusar o fluxo com um
    ``InvalidContactFlowException`` sem mensagem.
    """
    block: dict[str, Any] = {}
    if next_action is not None:
        block["NextAction"] = next_action
    block["Errors"] = errors or []
    block["Conditions"] = conditions or []
    return block

#: Texto da saudação inicial do assistente.
GREETING_TEXT: str = (
    "Olá! Sou o assistente da PROGEP/IFS. "
    "Faça sua pergunta sobre editais, concursos ou gestão de pessoas."
)
#: Aviso antes de transferir para um atendente humano (Req 3.1).
ESCALATION_TEXT: str = "Aguarde um momento, vou transferir você para um atendente."
#: Aviso quando o próprio fluxo falha (fail-safe).
ERROR_TEXT: str = (
    "Tivemos um problema no atendimento automático. "
    "Vou transferir você para um atendente."
)


def build_contact_flow(*, lambda_arn: str, queue_arn: str) -> dict[str, Any]:
    """Monta o documento do contact flow.

    Args:
        lambda_arn: ARN da Lambda que executa o agente (``connect_handler``).
        queue_arn: ARN da fila para onde escalar o atendimento humano.

    Returns:
        O documento do fluxo pronto para ``aws.connect.ContactFlow(content=...)``.
    """
    return {
        "Version": FLOW_VERSION,
        "StartAction": _GREETING,
        "Actions": [
            {
                "Identifier": _GREETING,
                "Type": "MessageParticipant",
                "Parameters": {"Text": GREETING_TEXT},
                "Transitions": _transitions(
                    _GET_INPUT,
                    errors=[{"NextAction": _ERROR_MSG, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                # Captura a pergunta do usuário e guarda em $.StoredCustomerInput.
                "Identifier": _GET_INPUT,
                "Type": "GetParticipantInput",
                "Parameters": {
                    "Text": INPUT_PROMPT,
                    "StoreInput": "True",
                    "InputTimeLimitSeconds": "300",
                    # Obrigatório pelo serviço; limita a pergunta a 4000 chars,
                    # o mesmo teto validado na camada de domínio (Req 2.6).
                    "InputValidation": {"CustomValidation": {"MaximumLength": "4000"}},
                },
                # ``InputTimeLimitExceeded`` não é um ErrorType válido aqui.
                "Transitions": _transitions(
                    _STORE_ATTR,
                    errors=[{"NextAction": _DISCONNECT, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                # Copia a entrada para um atributo do contato; a Lambda a lê em
                # ``ContactData.Attributes.text`` (o InvokeLambdaFunction não
                # aceita mapear parâmetros arbitrários neste schema).
                "Identifier": _STORE_ATTR,
                "Type": "UpdateContactAttributes",
                "Parameters": {"Attributes": {"text": "$.StoredCustomerInput"}},
                "Transitions": _transitions(
                    _INVOKE,
                    errors=[{"NextAction": _ERROR_MSG, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                "Identifier": _INVOKE,
                "Type": "InvokeLambdaFunction",
                "Parameters": {
                    "LambdaFunctionARN": lambda_arn,
                    "InvocationTimeLimitSeconds": "8",
                },
                "Transitions": _transitions(
                    _CHECK,
                    errors=[{"NextAction": _ERROR_MSG, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                # Ramifica no status devolvido pela Lambda.
                "Identifier": _CHECK,
                "Type": "Compare",
                "Parameters": {"ComparisonValue": "$.External.status"},
                "Transitions": _transitions(
                    _ESCALATE_MSG,
                    conditions=[
                        {
                            "NextAction": _ANSWER,
                            "Condition": {"Operator": "Equals", "Operands": ["answered"]},
                        }
                    ],
                    errors=[
                        {"NextAction": _ESCALATE_MSG, "ErrorType": "NoMatchingCondition"}
                    ],
                ),
            },
            {
                # Entrega a resposta fundamentada e volta a ouvir (conversa contínua).
                "Identifier": _ANSWER,
                "Type": "MessageParticipant",
                "Parameters": {"Text": "$.External.content"},
                "Transitions": _transitions(
                    _GET_INPUT,
                    errors=[{"NextAction": _ERROR_MSG, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                "Identifier": _ESCALATE_MSG,
                "Type": "MessageParticipant",
                "Parameters": {"Text": ESCALATION_TEXT},
                "Transitions": _transitions(
                    _SET_QUEUE,
                    errors=[{"NextAction": _SET_QUEUE, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                "Identifier": _SET_QUEUE,
                "Type": "UpdateContactTargetQueue",
                "Parameters": {"QueueId": queue_arn},
                "Transitions": _transitions(
                    _TRANSFER,
                    errors=[{"NextAction": _DISCONNECT, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                # Enfileira para o atendente humano (Req 3.4: enfileira mesmo sem
                # atendente disponível). Este tipo não aceita ``Parameters``.
                "Identifier": _TRANSFER,
                "Type": "TransferContactToQueue",
                "Transitions": _transitions(
                    _DISCONNECT,
                    errors=[
                        {"NextAction": _DISCONNECT, "ErrorType": "QueueAtCapacity"},
                        {"NextAction": _DISCONNECT, "ErrorType": "NoMatchingError"},
                    ],
                ),
            },
            {
                "Identifier": _ERROR_MSG,
                "Type": "MessageParticipant",
                "Parameters": {"Text": ERROR_TEXT},
                "Transitions": _transitions(
                    _SET_QUEUE,
                    errors=[{"NextAction": _DISCONNECT, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                "Identifier": _DISCONNECT,
                "Type": "DisconnectParticipant",
                "Parameters": {},
                "Transitions": {},
            },
        ],
    }


def contact_flow_content(*, lambda_arn: str, queue_arn: str) -> str:
    """Serializa o contact flow como JSON para o recurso Pulumi."""
    return json.dumps(build_contact_flow(lambda_arn=lambda_arn, queue_arn=queue_arn))


# --- Transbordo do WhatsApp -----------------------------------------------------

_T_SET_QUEUE = "b2000000-0000-4000-8000-000000000001"
_T_TRANSFER = "b2000000-0000-4000-8000-000000000002"
_T_DISCONNECT = "b2000000-0000-4000-8000-000000000003"


def build_transfer_flow(*, queue_arn: str) -> dict[str, Any]:
    """Fluxo que **só** põe o chat na fila humana.

    O transbordo do WhatsApp não pode usar o fluxo acima: ele chama o
    assistente, e o servidor que acabou de pedir uma pessoa receberia de novo
    "Faça sua pergunta". Quem decide transferir aqui é a Lambda do WhatsApp; o
    Connect só enfileira.

    **Nenhuma mensagem sai deste fluxo.** Tudo que o fluxo escreve vai pelo
    streaming com papel ``SYSTEM``, que a ponte descarta — os avisos ao servidor
    são enviados pela Lambda, que sabe o que já foi dito.
    """
    return {
        "Version": FLOW_VERSION,
        "StartAction": _T_SET_QUEUE,
        "Actions": [
            {
                "Identifier": _T_SET_QUEUE,
                "Type": "UpdateContactTargetQueue",
                "Parameters": {"QueueId": queue_arn},
                "Transitions": _transitions(
                    _T_TRANSFER,
                    errors=[{"NextAction": _T_DISCONNECT, "ErrorType": "NoMatchingError"}],
                ),
            },
            {
                "Identifier": _T_TRANSFER,
                "Type": "TransferContactToQueue",
                "Transitions": _transitions(
                    _T_DISCONNECT,
                    errors=[
                        {"NextAction": _T_DISCONNECT, "ErrorType": "QueueAtCapacity"},
                        {"NextAction": _T_DISCONNECT, "ErrorType": "NoMatchingError"},
                    ],
                ),
            },
            {
                "Identifier": _T_DISCONNECT,
                "Type": "DisconnectParticipant",
                "Parameters": {},
                "Transitions": {},
            },
        ],
    }


def transfer_flow_content(*, queue_arn: str) -> str:
    """Serializa o fluxo de transbordo como JSON para o recurso Pulumi."""
    return json.dumps(build_transfer_flow(queue_arn=queue_arn))
