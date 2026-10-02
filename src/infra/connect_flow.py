"""Contact flow do Amazon Connect para o transbordo do WhatsApp.

Gera o documento JSON (linguagem de fluxo do Connect, versão ``2019-10-30``) do
fluxo que põe o chat do servidor na fila humana da PROGEP. Quem decide
transferir é a Lambda do WhatsApp; o Connect só enfileira.

Manter o fluxo como dado gerado por código (e não desenhado no console) atende ao
Req 9.1: todo recurso definido em IaC, reproduzível e auditável.

Até 02/10/2026 havia aqui também o fluxo ``assistente-progep`` do desenho
original, que chamava o assistente a cada mensagem pelo Connect. O WhatsApp
nunca passou por ele, e ele saiu junto com a Lambda ``progep-ifs-connect``.
"""

from __future__ import annotations

import json
from typing import Any

#: Versão da linguagem de fluxo do Amazon Connect.
FLOW_VERSION: str = "2019-10-30"

_T_SET_QUEUE = "b2000000-0000-4000-8000-000000000001"
_T_TRANSFER = "b2000000-0000-4000-8000-000000000002"
_T_DISCONNECT = "b2000000-0000-4000-8000-000000000003"


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


def build_transfer_flow(*, queue_arn: str) -> dict[str, Any]:
    """Fluxo que **só** põe o chat na fila humana.

    Não chama o assistente: o servidor acabou de pedir uma pessoa, e receber de
    novo "Faça sua pergunta" seria voltar ao começo.

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
                # Enfileira mesmo sem atendente disponível (Req 3.4). Este tipo
                # não aceita ``Parameters``.
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
