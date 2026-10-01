"""Testes da ponte WhatsApp ↔ chat do Amazon Connect.

O streaming do Connect publica **tudo** o que acontece no chat — inclusive o eco
do que o próprio servidor mandou. Errar o filtro aqui devolve ao servidor cada
mensagem dele, ou manda avisos do sistema em inglês.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.adapters.connect_chat import (
    DURACAO_CHAT_MIN,
    EVENTO_ENTROU,
    EVENTO_FIM,
    EVENTO_SAIU,
    ChatEncerrado,
    ConnectChatBridge,
    parse_stream_event,
)

pytestmark = pytest.mark.unit

TOPICO = "arn:aws:sns:us-east-1:342016537070:progep-ifs-connect-chat-stream"


def _registro(**campos):
    corpo = {
        "AbsoluteTime": "2026-09-14T15:00:00.000Z",
        "Id": "m-1",
        "ContactId": "c-1",
        "InitialContactId": "c-1",
        **campos,
    }
    return {"Sns": {"Message": json.dumps(corpo)}}


def _evento(*registros):
    return {"Records": list(registros)}


# --- Leitura do streaming ------------------------------------------------------

def test_mensagem_do_atendente_e_reconhecida():
    [ev] = parse_stream_event(_evento(_registro(
        Type="MESSAGE", ContentType="text/plain", ParticipantRole="AGENT",
        Content="Bom dia! Vou verificar.", DisplayName="Julia",
    )))
    assert ev.mensagem_do_atendente
    assert ev.conteudo == "Bom dia! Vou verificar."
    assert ev.contato_id == "c-1"


@pytest.mark.parametrize("papel", ["CUSTOMER", "SYSTEM", "CUSTOM_BOT"])
def test_eco_e_mensagens_do_sistema_nao_vao_ao_servidor(papel):
    [ev] = parse_stream_event(_evento(_registro(
        Type="MESSAGE", ContentType="text/plain", ParticipantRole=papel, Content="x",
    )))
    assert not ev.mensagem_do_atendente


def test_entrada_do_atendente():
    [ev] = parse_stream_event(_evento(_registro(
        Type="EVENT", ContentType=EVENTO_ENTROU, ParticipantRole="AGENT", DisplayName="Julia",
    )))
    assert ev.atendente_entrou and ev.nome == "Julia"


def test_entrada_do_proprio_servidor_nao_e_anunciada():
    [ev] = parse_stream_event(_evento(_registro(
        Type="EVENT", ContentType=EVENTO_ENTROU, ParticipantRole="CUSTOMER",
    )))
    assert not ev.atendente_entrou


@pytest.mark.parametrize(
    "conteudo,papel,encerra",
    [
        (EVENTO_FIM, "", True),
        (EVENTO_SAIU, "AGENT", True),
        # O servidor "sair" é a própria Lambda desconectando; não é o fim do
        # atendimento do ponto de vista dele.
        (EVENTO_SAIU, "CUSTOMER", False),
        ("application/vnd.amazonaws.connect.event.typing", "AGENT", False),
    ],
)
def test_fim_do_atendimento(conteudo, papel, encerra):
    [ev] = parse_stream_event(_evento(_registro(
        Type="EVENT", ContentType=conteudo, ParticipantRole=papel,
    )))
    assert ev.encerra_atendimento is encerra


def test_usa_o_contato_inicial_que_nao_muda_na_transferencia():
    """Numa transferência entre filas o ``ContactId`` muda; o gravado ao abrir
    o atendimento foi o inicial."""
    [ev] = parse_stream_event(_evento(_registro(
        ContactId="c-2", InitialContactId="c-1", Type="MESSAGE",
        ParticipantRole="AGENT", Content="oi",
    )))
    assert ev.contato_id == "c-1"


def test_registro_corrompido_nao_derruba_o_lote():
    evento = _evento(
        _registro(Type="MESSAGE", ParticipantRole="AGENT", Content="primeira"),
        {"Sns": {"Message": "não é json"}},
        {"Sns": {"Message": json.dumps(["lista"])}},
        _registro(Type="MESSAGE", ParticipantRole="AGENT", Content="segunda"),
    )
    assert [e.conteudo for e in parse_stream_event(evento)] == ["primeira", "segunda"]


@pytest.mark.parametrize("evento", [{}, {"Records": []}, {"Records": [{"Sns": {}}]}])
def test_evento_vazio(evento):
    assert parse_stream_event(evento) == []


# --- Abertura ------------------------------------------------------------------

def _ponte():
    connect = MagicMock()
    connect.start_chat_contact.return_value = {
        "ContactId": "c-1", "ParticipantId": "p-1", "ParticipantToken": "PT",
    }
    participant = MagicMock()
    participant.create_participant_connection.return_value = {
        "ConnectionCredentials": {"ConnectionToken": "CT", "Expiry": "..."},
    }
    ponte = ConnectChatBridge(
        "inst-1", "flow-1", TOPICO, connect_client=connect, participant_client=participant
    )
    return ponte, connect, participant


def test_abre_chat_liga_streaming_e_conecta_nesta_ordem():
    """Streaming depois de o atendente aceitar perderia a primeira fala dele."""
    ponte, connect, participant = _ponte()
    ordem: list[str] = []
    connect.start_chat_contact.side_effect = lambda **k: (
        ordem.append("chat") or {"ContactId": "c-1", "ParticipantToken": "PT"}
    )
    connect.start_contact_streaming.side_effect = lambda **k: ordem.append("stream")
    participant.create_participant_connection.side_effect = lambda **k: (
        ordem.append("conexao") or {"ConnectionCredentials": {"ConnectionToken": "CT"}}
    )
    participant.send_message.side_effect = lambda **k: ordem.append("contexto")

    aberto = ponte.abrir(nome="Servidor", atributos={"pergunta": "p"}, contexto="p")

    assert ordem == ["chat", "stream", "conexao", "contexto"]
    assert (aberto.contato_id, aberto.participant_token, aberto.connection_token) == (
        "c-1", "PT", "CT",
    )


def test_abertura_usa_fluxo_de_transferencia_e_topico():
    ponte, connect, _ = _ponte()
    ponte.abrir(nome="Servidor", atributos={"whatsapp": "+55"}, contexto="", client_token="wamid.1")

    chat = connect.start_chat_contact.call_args.kwargs
    assert chat["InstanceId"] == "inst-1"
    assert chat["ContactFlowId"] == "flow-1"
    assert chat["ChatDurationInMinutes"] == DURACAO_CHAT_MIN
    assert chat["ClientToken"] == "wamid.1"
    stream = connect.start_contact_streaming.call_args.kwargs
    assert stream["ContactId"] == "c-1"
    assert stream["ChatStreamingConfiguration"] == {"StreamingEndpointArn": TOPICO}


def test_sem_contexto_nao_manda_mensagem_vazia():
    ponte, _, participant = _ponte()
    ponte.abrir(nome="S", atributos={}, contexto="   ")
    participant.send_message.assert_not_called()


def test_falha_no_contexto_nao_desfaz_o_atendimento():
    """O contato já está na fila e os atributos levam a pergunta."""
    ponte, _, participant = _ponte()
    participant.send_message.side_effect = RuntimeError("falhou")

    assert ponte.abrir(nome="S", atributos={}, contexto="p").contato_id == "c-1"


def test_falha_no_streaming_sobe():
    """Atendimento sem streaming: o atendente responderia para ninguém."""
    ponte, connect, _ = _ponte()
    connect.start_contact_streaming.side_effect = RuntimeError("negado")

    with pytest.raises(RuntimeError):
        ponte.abrir(nome="S", atributos={}, contexto="p")


# --- Envio durante o atendimento -----------------------------------------------

def test_envio_com_token_valido_mantem_o_token():
    ponte, _, participant = _ponte()

    assert ponte.enviar("PT", "CT", "minha dúvida") == "CT"
    kw = participant.send_message.call_args.kwargs
    assert kw["ConnectionToken"] == "CT" and kw["Content"] == "minha dúvida"
    participant.create_participant_connection.assert_not_called()


def test_token_vencido_e_renovado_e_devolvido():
    ponte, _, participant = _ponte()
    participant.create_participant_connection.return_value = {
        "ConnectionCredentials": {"ConnectionToken": "CT2"}
    }
    participant.send_message.side_effect = [RuntimeError("expirado"), None]

    assert ponte.enviar("PT", "CT", "oi") == "CT2"


def test_chat_encerrado_quando_nem_o_token_novo_serve():
    ponte, _, participant = _ponte()
    participant.send_message.side_effect = RuntimeError("expirado")
    participant.create_participant_connection.side_effect = RuntimeError("chat terminou")

    with pytest.raises(ChatEncerrado):
        ponte.enviar("PT", "CT", "oi")


def test_sem_participant_token_e_chat_encerrado():
    ponte, _, participant = _ponte()
    participant.send_message.side_effect = RuntimeError("expirado")

    with pytest.raises(ChatEncerrado):
        ponte.enviar("", "CT", "oi")


# --- Configuração --------------------------------------------------------------

def test_sem_configuracao_completa_nao_ha_ponte(monkeypatch):
    monkeypatch.setenv("CONNECT_INSTANCE_ID", "inst")
    monkeypatch.setenv("CONNECT_TRANSFER_FLOW_ID", "flow")
    monkeypatch.delenv("CONNECT_STREAM_TOPIC_ARN", raising=False)
    assert ConnectChatBridge.do_ambiente() is None

    monkeypatch.setenv("CONNECT_STREAM_TOPIC_ARN", TOPICO)
    assert ConnectChatBridge.do_ambiente() is not None
