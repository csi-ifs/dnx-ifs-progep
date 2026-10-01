"""Testes do canal WhatsApp pelo AWS End User Messaging Social.

O evento traz **duas camadas de JSON-dentro-de-string** entre a Lambda e o texto
do usuário, e é nelas que esse tipo de integração quebra. O formato aqui segue o
documentado pela AWS em "Message and event format in AWS End User Messaging
Social".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.adapters.whatsapp_social import (
    META_API_VERSION,
    WhatsAppSocialClient,
    parse_sns_event,
    phone_number_id_de_origem,
    registrar_status_de_entrega,
    status_de_entrega,
)

pytestmark = pytest.mark.unit

ARN_NUMERO = (
    "arn:aws:social-messaging:us-east-1:342016537070:phone-number-id/976c72a700ac43e"
)


def _entry(texto="Como solicito férias?", tipo="text", de="557900001234"):
    mensagem = {"from": de, "id": "wamid.ABC", "timestamp": "1736379042", "type": tipo}
    if tipo == "text":
        mensagem["text"] = {"body": texto}
    else:
        mensagem[tipo] = {"mime_type": "image/jpeg", "id": "530339869524171"}
    return {
        "id": "503131219501234",
        "changes": [
            {
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "14255550123",
                        "phone_number_id": "46271669example",
                    },
                    "messages": [mensagem],
                },
                "field": "messages",
            }
        ],
    }


def _evento(*entries, arn=ARN_NUMERO):
    registros = []
    for entrada in entries:
        corpo = {
            "context": {
                "MetaWabaIds": [{"wabaId": "1603375864906818", "arn": "arn:...waba/x"}],
                "MetaPhoneNumberIds": [
                    {"metaPhoneNumberId": "46271669example", "arn": arn}
                ],
            },
            "whatsAppWebhookEntry": json.dumps(entrada),
            "aws_account_id": "342016537070",
            "message_timestamp": "2026-09-11T12:00:00Z",
            "messageId": "6d69f07a-c317-4278-9d5c-6a84078419ec",
        }
        registros.append({"Sns": {"Message": json.dumps(corpo)}})
    return {"Records": registros}


# --- Entrada -------------------------------------------------------------------

def test_extrai_o_texto_atravessando_as_duas_camadas():
    mensagens = parse_sns_event(_evento(_entry("Quantos dias de férias?")))

    assert len(mensagens) == 1
    m = mensagens[0]
    assert m.text == "Quantos dias de férias?"
    assert m.from_number == "557900001234"
    assert m.message_id == "wamid.ABC"
    assert m.part_types == ("text",)


def test_midia_preserva_o_tipo_para_ser_descartada():
    """O domínio recusa não-texto (Req 6.3); ele precisa do tipo para isso."""
    mensagens = parse_sns_event(_evento(_entry(tipo="image")))
    assert mensagens[0].part_types == ("image",)
    assert mensagens[0].text == ""


def test_registro_corrompido_nao_derruba_o_lote():
    """Perder o lote inteiro faria o SNS reentregar, e a mensagem boa seria
    respondida duas vezes."""
    evento = _evento(_entry("primeira"), _entry("segunda"))
    evento["Records"].insert(1, {"Sns": {"Message": "isto não é json"}})

    mensagens = parse_sns_event(evento)

    assert [m.text for m in mensagens] == ["primeira", "segunda"]


@pytest.mark.parametrize(
    "evento",
    [
        {},
        {"Records": []},
        {"Records": [{"Sns": {}}]},
        {"Records": [{"Sns": {"Message": json.dumps({"context": {}})}}]},  # sem entry
    ],
)
def test_evento_sem_mensagem_devolve_vazio(evento):
    assert parse_sns_event(evento) == []


# --- Número de origem ----------------------------------------------------------

def test_usa_o_arn_da_aws_e_nao_o_id_da_meta():
    """`SendWhatsAppMessage` exige o identificador **da AWS**. Passar o
    `phone_number_id` da Meta é recusado por formato, e o erro não diz que o
    identificador veio do domínio errado."""
    assert phone_number_id_de_origem(_evento(_entry())) == ARN_NUMERO


def test_sem_contexto_devolve_vazio():
    assert phone_number_id_de_origem({"Records": []}) == ""


# --- Envio ---------------------------------------------------------------------

def _cliente():
    fake = MagicMock()
    fake.send_whatsapp_message.return_value = {"messageId": "wamid.ENVIADA"}
    return WhatsAppSocialClient(client=fake), fake


def test_envio_usa_o_payload_da_graph_api_com_destino_e164():
    """A AWS embrulha o payload da Graph API — com uma diferença que custou uma
    tarde: o destino precisa de `+`."""
    cliente, fake = _cliente()

    assert cliente.send_text(ARN_NUMERO, "557900001234", "Olá!") == "wamid.ENVIADA"

    kw = fake.send_whatsapp_message.call_args.kwargs
    assert kw["originationPhoneNumberId"] == ARN_NUMERO
    assert kw["metaApiVersion"] == META_API_VERSION
    assert json.loads(kw["message"].decode("utf-8")) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+557900001234",
        "type": "text",
        "text": {"preview_url": False, "body": "Olá!"},
    }


@pytest.mark.parametrize(
    "recebido,enviado",
    [
        # Como a Meta entrega o remetente: sem `+`. Reenviar assim é recusado
        # pela AWS com "Invalid destination phone number" (medido 14/09/2026).
        ("557900001234", "+557900001234"),
        # Já em E.164: não pode virar `++`.
        ("+557900001234", "+557900001234"),
        ("16465894168", "+16465894168"),
    ],
)
def test_destino_sempre_em_e164_sem_duplicar_o_mais(recebido, enviado):
    cliente, fake = _cliente()

    cliente.send_text(ARN_NUMERO, recebido, "oi")

    enviado_de_fato = json.loads(fake.send_whatsapp_message.call_args.kwargs["message"])
    assert enviado_de_fato["to"] == enviado


def test_indicador_de_digitando_sobrevive_a_migracao():
    """Era o maior risco da mudança: o indicador foi o que resolveu a queixa do
    "vácuo". Como é payload e não recurso da Graph, atravessa igual."""
    cliente, fake = _cliente()

    assert cliente.mark_read_and_typing(ARN_NUMERO, "wamid.ABC") is True

    enviado = json.loads(fake.send_whatsapp_message.call_args.kwargs["message"])
    assert enviado["status"] == "read"
    assert enviado["message_id"] == "wamid.ABC"
    assert enviado["typing_indicator"] == {"type": "text"}


def test_falha_no_indicador_nao_quebra_o_atendimento():
    cliente, fake = _cliente()
    fake.send_whatsapp_message.side_effect = RuntimeError("indisponível")

    assert cliente.mark_read_and_typing(ARN_NUMERO, "wamid.ABC") is False


def test_sem_message_id_nao_chama_a_aws():
    cliente, fake = _cliente()
    assert cliente.mark_read_and_typing(ARN_NUMERO, "") is False
    fake.send_whatsapp_message.assert_not_called()


def _evento_status(*statuses):
    entrada = {"id": "1", "changes": [{"field": "messages", "value": {"statuses": list(statuses)}}]}
    corpo = {"context": {}, "whatsAppWebhookEntry": json.dumps(entrada)}
    return {"Records": [{"Sns": {"Message": json.dumps(corpo)}}]}


def test_status_de_falha_traz_o_erro_da_meta_sem_o_numero_inteiro():
    """A AWS aceita e a Meta recusa depois: é o único rastro dessa recusa."""
    evento = _evento_status(
        {"id": "wamid.X", "status": "failed", "recipient_id": "557900001234",
         "errors": [{"code": 131047, "title": "Re-engagement message",
                     "error_data": {"details": "more than 24 hours"}}]},
        {"id": "wamid.Y", "status": "delivered", "recipient_id": "557900001234"},
    )

    falha, entregue = status_de_entrega(evento)

    assert falha["status"] == "failed"
    assert falha["erros"] == [
        {"code": 131047, "title": "Re-engagement message", "details": "more than 24 hours"}
    ]
    assert falha["destino_final"] == "1234"
    assert "557900001234" not in json.dumps(falha)
    assert entregue["status"] == "delivered" and entregue["erros"] == []


def test_registrar_conta_so_as_falhas(caplog):
    evento = _evento_status(
        {"id": "a", "status": "failed", "recipient_id": "1", "errors": [{"code": 1}]},
        {"id": "b", "status": "sent", "recipient_id": "1"},
    )
    with caplog.at_level("WARNING"):
        assert registrar_status_de_entrega(evento) == 1
    assert "FALHOU" in caplog.text


def test_mensagem_de_usuario_nao_tem_status():
    assert status_de_entrega(_evento(_entry())) == []


def test_falha_no_envio_sobe():
    """Ao contrário da cortesia, não entregar a resposta precisa ser visível."""
    cliente, fake = _cliente()
    fake.send_whatsapp_message.side_effect = RuntimeError("recusado")

    with pytest.raises(RuntimeError):
        cliente.send_text(ARN_NUMERO, "557900001234", "resposta")
