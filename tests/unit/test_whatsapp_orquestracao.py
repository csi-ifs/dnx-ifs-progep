"""Testes da orquestração do canal WhatsApp e das suas duas entradas.

O ``whatsapp_handler`` **não tinha teste nenhum** quando a orquestração foi
extraída para servir também ao End User Messaging Social. Estes testes fixam o
comportamento que o usuário vê — e que não pode mudar na migração: mídia
recusada, limite de tamanho, "digitando", cortesias, saudação só na abertura,
sessão por remetente e fontes citadas.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.adapters import lambda_entry
from src.adapters.connect_chat import AtendimentoAberto, ChatEncerrado
from src.adapters.conversation_store import (
    TRANSBORDO_HUMANO,
    TRANSBORDO_LISTA,
    TRANSBORDO_OFERECIDO,
    ConversaDoAtendimento,
    EstadoTransbordo,
)
from src.adapters.whatsapp_connector import IncomingWhatsAppMessage
from src.domain.config import AgentStatus
from src.domain.models import AgentResponse
from src.domain.social import TipoSocial

pytestmark = pytest.mark.unit

ORIGEM = "arn:aws:social-messaging:us-east-1:342016537070:phone-number-id/abc123"
USUARIO = "557900001234"


def _msg(texto="Como solicito férias?", tipos=("text",), origem=ORIGEM, mid="wamid.1"):
    return IncomingWhatsAppMessage(
        from_number=USUARIO,
        message_id=mid,
        text=texto,
        part_types=tipos,
        phone_number_id=origem,
    )


def _resposta(status=AgentStatus.ANSWERED.value, conteudo="São 30 dias.", fontes=()):
    return AgentResponse(
        status=status,
        content=conteudo,
        sources=tuple(SimpleNamespace(doc_id=f) for f in fontes),
        confidence=0.9,
    )


@pytest.fixture
def cliente():
    return MagicMock()


@pytest.fixture
def loja(monkeypatch):
    fake = MagicMock()
    fake.conversa_aberta.return_value = False
    fake.estado_transbordo.return_value = EstadoTransbordo()
    monkeypatch.setattr(lambda_entry, "_get_conversas", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def sem_ponte(monkeypatch):
    """Por padrão o transbordo não está configurado — é o comportamento que os
    testes anteriores à ponte fixam."""
    monkeypatch.setattr(lambda_entry, "_get_ponte", lambda: None)


@pytest.fixture
def agente(monkeypatch):
    chamadas: list = []

    def invocar(request):
        chamadas.append(request)
        return _resposta(fontes=("s3://b/cliente/DNAGS/L8112compilado.pdf",))

    monkeypatch.setattr(lambda_entry, "_invoke_agent", invocar)
    return chamadas


def _textos_enviados(cliente):
    return [c.args[2] for c in cliente.send_text.call_args_list]


# --- Recusas que não consultam o agente ----------------------------------------

def test_midia_e_recusada_sem_chamar_o_agente(cliente, loja, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg("", tipos=("image",))], cliente)

    assert agente == []
    assert len(_textos_enviados(cliente)) == 1


def test_mensagem_longa_demais_e_recusada(cliente, loja, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg("a" * 4097)], cliente)

    assert agente == []
    assert len(_textos_enviados(cliente)) == 1


# --- Fluxo de pergunta ---------------------------------------------------------

def test_digitando_vem_antes_de_chamar_o_agente(cliente, loja, monkeypatch):
    """O indicador foi o que resolveu a queixa do "vácuo": precisa aparecer
    enquanto o agente pensa, não depois."""
    ordem: list[str] = []
    cliente.mark_read_and_typing.side_effect = lambda *a: ordem.append("digitando")

    def invocar(request):
        ordem.append("agente")
        return _resposta()

    monkeypatch.setattr(lambda_entry, "_invoke_agent", invocar)
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)

    assert ordem == ["digitando", "agente"]
    cliente.mark_read_and_typing.assert_called_once_with(ORIGEM, "wamid.1")


def test_saudacao_so_na_abertura(cliente, loja, agente):
    loja.conversa_aberta.return_value = False
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)
    assert _textos_enviados(cliente)[0] == lambda_entry.WHATSAPP_SAUDACAO

    cliente.reset_mock()
    loja.conversa_aberta.return_value = True
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)
    assert lambda_entry.WHATSAPP_SAUDACAO not in _textos_enviados(cliente)


def test_sessao_e_do_remetente_e_nao_da_mensagem(cliente, loja, agente):
    """Com o message_id cada turno viraria conversa nova e o assistente nunca
    entenderia um "e quanto tempo antes?"."""
    lambda_entry.processar_mensagens_whatsapp(
        [_msg(mid="wamid.1"), _msg(mid="wamid.2")], cliente
    )
    assert {r.session_id for r in agente} == {USUARIO}


def test_resposta_leva_as_fontes(cliente, loja, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)
    assert "Lei 8112" in _textos_enviados(cliente)[-1]


def test_origem_gravada_e_a_do_cliente_de_envio(cliente, loja, agente):
    """O encerramento por inatividade responde depois com o que foi gravado
    aqui. Gravar o id da Meta quando o envio é pela AWS faria o aviso de
    encerramento falhar 20 minutos depois, longe de qualquer pista."""
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)

    loja.registrar_atividade.assert_called_with("whatsapp", USUARIO, ORIGEM)
    assert all(c.args[0] == ORIGEM for c in cliente.send_text.call_args_list)


def test_falha_do_agente_vira_aviso_de_indisponibilidade(cliente, loja, monkeypatch):
    def quebra(_):
        raise RuntimeError("runtime fora")

    monkeypatch.setattr(lambda_entry, "_invoke_agent", quebra)
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)

    assert "temporariamente indisponível" in _textos_enviados(cliente)[-1]


# --- Cortesias -----------------------------------------------------------------

def test_cortesia_responde_sem_chamar_o_agente(cliente, loja, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg("Entendi, obrigada!")], cliente)

    assert agente == []
    assert _textos_enviados(cliente) == [
        lambda_entry.WHATSAPP_SOCIAIS[TipoSocial.AGRADECIMENTO]
    ]


def test_ola_em_conversa_nova_traz_a_apresentacao(cliente, loja, agente):
    """Quem abre com "Olá" precisa saber com quem fala e o que o assistente faz."""
    loja.conversa_aberta.return_value = False
    lambda_entry.processar_mensagens_whatsapp([_msg("Olá")], cliente)

    assert agente == []
    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_APRESENTACAO]
    assert "documentos oficiais" in lambda_entry.WHATSAPP_APRESENTACAO
    assert "Vou consultar" not in lambda_entry.WHATSAPP_APRESENTACAO


def test_ola_no_meio_da_conversa_nao_repete_a_apresentacao(cliente, loja, agente):
    loja.conversa_aberta.return_value = True
    lambda_entry.processar_mensagens_whatsapp([_msg("Olá")], cliente)

    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_SOCIAIS[TipoSocial.SAUDACAO]]


def test_despedida_encerra_na_hora(cliente, loja, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg("obrigado, tchau")], cliente)

    loja.marcar_encerrada.assert_called_once_with("whatsapp", USUARIO)
    loja.registrar_atividade.assert_not_called()


def test_mensagem_repetida_e_ignorada(cliente, loja, agente):
    """A entrega é "pelo menos uma vez". Em 25/09/2026 duas mensagens do
    atendimento humano voltaram dois minutos depois e o assistente respondeu a
    elas com o atendimento já encerrado."""
    loja.marcar_mensagem_processada.side_effect = [True, False]

    n = lambda_entry.processar_mensagens_whatsapp(
        [_msg("Quantos dias de férias?", mid="wamid.1"),
         _msg("Quantos dias de férias?", mid="wamid.1")],
        cliente,
    )

    assert n == 1
    assert len(agente) == 1
    assert len(cliente.send_text.call_args_list) == 2  # saudação + resposta, uma vez


def test_conta_as_mensagens_atendidas(cliente, loja, agente):
    n = lambda_entry.processar_mensagens_whatsapp(
        [_msg(), _msg("", tipos=("image",)), _msg("ok")], cliente
    )
    assert n == 3


# --- Entrada pelo End User Messaging Social ------------------------------------

def _evento_sns(texto="Como solicito férias?", arn=ORIGEM):
    entry = {
        "id": "1",
        "changes": [
            {
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "id-da-meta"},
                    "messages": [
                        {"from": USUARIO, "id": "wamid.9", "type": "text",
                         "text": {"body": texto}}
                    ],
                },
            }
        ],
    }
    corpo = {
        "context": {"MetaPhoneNumberIds": [{"metaPhoneNumberId": "id-da-meta", "arn": arn}]},
        "whatsAppWebhookEntry": json.dumps(entry),
    }
    return {"Records": [{"Sns": {"Message": json.dumps(corpo)}}]}


@pytest.fixture
def cliente_social(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(
        "src.adapters.whatsapp_social.WhatsAppSocialClient", lambda *a, **k: fake
    )
    return fake


def test_handler_sns_responde_pelo_arn_da_aws(cliente_social, loja, agente):
    """O payload traz o id **da Meta**; enviar com ele é recusado por formato.
    A troca pelo ARN do cabeçalho é o que faz a resposta sair."""
    saida = lambda_entry.whatsapp_social_handler(_evento_sns())

    assert saida == {"processadas": 1}
    assert all(c.args[0] == ORIGEM for c in cliente_social.send_text.call_args_list)
    assert "id-da-meta" not in {c.args[0] for c in cliente_social.send_text.call_args_list}


def test_evento_de_status_nao_gera_resposta(cliente_social, loja, agente):
    """Entregue/lido chegam no mesmo tópico e não são mensagens de usuário."""
    entry = {"id": "1", "changes": [{"field": "messages", "value": {"statuses": [{}]}}]}
    corpo = {"context": {}, "whatsAppWebhookEntry": json.dumps(entry)}
    evento = {"Records": [{"Sns": {"Message": json.dumps(corpo)}}]}

    assert lambda_entry.whatsapp_social_handler(evento) == {"processadas": 0}
    cliente_social.send_text.assert_not_called()
    assert agente == []


def test_sem_origem_nao_tenta_responder(cliente_social, loja, agente):
    """Sem o ARN não há como enviar; tentar com o id da Meta falharia calado."""
    saida = lambda_entry.whatsapp_social_handler(_evento_sns(arn=""))

    assert saida["processadas"] == 0
    cliente_social.send_text.assert_not_called()


# --- Encerramento na transição -------------------------------------------------

def test_encerramento_escolhe_o_cliente_pela_origem_gravada(monkeypatch):
    """Na virada, há conversas gravadas com id da Meta (webhook antigo) e com
    ARN da AWS (novo caminho). Cada uma precisa ser encerrada pelo canal por
    onde começou."""
    from src.adapters.whatsapp_cloud import WhatsAppCloudClient
    from src.adapters.whatsapp_social import WhatsAppSocialClient

    monkeypatch.setattr(lambda_entry, "_get_whatsapp_secret", lambda: {"access_token": "t"})

    assert isinstance(lambda_entry._cliente_para_origem(ORIGEM), WhatsAppSocialClient)
    assert isinstance(lambda_entry._cliente_para_origem("1176961198827673"), WhatsAppCloudClient)


def test_encerramento_pelo_caminho_novo_nao_le_o_segredo_da_meta(monkeypatch):
    """Depois da migração o segredo da Meta pode nem existir mais."""
    def sem_segredo():
        raise AssertionError("não deveria ler o segredo da Meta")

    monkeypatch.setattr(lambda_entry, "_get_whatsapp_secret", sem_segredo)
    lambda_entry._cliente_para_origem(ORIGEM)


# --- Transbordo: oferta ---------------------------------------------------------

@pytest.fixture
def ponte(monkeypatch):
    fake = MagicMock()
    fake.abrir.return_value = AtendimentoAberto("c-1", "PT", "CT")
    fake.enviar.return_value = "CT"
    monkeypatch.setattr(lambda_entry, "_get_ponte", lambda: fake)
    return fake


@pytest.fixture
def expediente(monkeypatch):
    estado = {"aberto": True}
    monkeypatch.setattr(lambda_entry, "dentro_do_expediente", lambda *a: estado["aberto"])
    return estado


@pytest.fixture
def nao_encontra(monkeypatch):
    """Agente sem resposta; ``setores`` controla a classificação devolvida."""
    estado = {"setores": ("DAP",), "chamadas": []}

    def invocar(request):
        estado["chamadas"].append(request)
        return AgentResponse(
            status=AgentStatus.NOT_FOUND.value, content="", sources=(),
            confidence=0.0, setores=estado["setores"],
        )

    monkeypatch.setattr(lambda_entry, "_invoke_agent", invocar)
    return estado


def test_sem_resposta_indica_o_contato_do_setor(cliente, loja, nao_encontra):
    """Pedido do cliente (28/09/2026): o contato vem sempre, mesmo sem atendente."""
    lambda_entry.processar_mensagens_whatsapp([_msg("Como averbo tempo?")], cliente)

    texto = _textos_enviados(cliente)[-1]
    assert "dap.progep@ifs.edu.br" in texto
    assert "ramal 1401 ou 1445" in texto
    assert lambda_entry.WHATSAPP_OFERTA_ATENDENTE not in texto  # sem ponte
    loja.oferecer_transbordo.assert_not_called()


def test_saude_indica_siass_e_dap(cliente, loja, nao_encontra):
    nao_encontra["setores"] = ("SIASS", "DAP")
    lambda_entry.processar_mensagens_whatsapp([_msg("Como marco perícia?")], cliente)

    texto = _textos_enviados(cliente)[-1]
    assert "siass@ifs.edu.br" in texto and "dap.progep@ifs.edu.br" in texto


def test_sem_classificacao_usa_o_contato_geral(cliente, loja, nao_encontra):
    """Runtime antigo ou classificação que falhou: nunca fica sem contato."""
    nao_encontra["setores"] = ()
    lambda_entry.processar_mensagens_whatsapp([_msg("Pergunta sem documento")], cliente)

    assert "progep@ifs.edu.br" in _textos_enviados(cliente)[-1]


def test_no_expediente_contato_e_oferta_de_atendente(
    cliente, loja, ponte, expediente, nao_encontra
):
    lambda_entry.processar_mensagens_whatsapp([_msg("Pergunta sem documento")], cliente)

    texto = _textos_enviados(cliente)[-1]
    assert "dap.progep@ifs.edu.br" in texto
    assert texto.endswith(lambda_entry.WHATSAPP_OFERTA_ATENDENTE)
    loja.oferecer_transbordo.assert_called_once_with("whatsapp", USUARIO, "Pergunta sem documento")
    ponte.abrir.assert_not_called()  # só abre depois do "sim"


def test_fora_do_expediente_so_o_contato(cliente, loja, ponte, expediente, nao_encontra):
    expediente["aberto"] = False
    lambda_entry.processar_mensagens_whatsapp([_msg("Pergunta sem documento")], cliente)

    texto = _textos_enviados(cliente)[-1]
    assert "dap.progep@ifs.edu.br" in texto
    assert lambda_entry.WHATSAPP_OFERTA_ATENDENTE not in texto
    loja.oferecer_transbordo.assert_not_called()


def test_fora_da_progep_oferece_a_lista_e_nao_o_atendente(
    cliente, loja, ponte, expediente, nao_encontra
):
    """"Cardápio do restaurante" não é de nenhum setor da PROGEP: nem setor,
    nem atendente — só a lista, se a pessoa quiser."""
    nao_encontra["setores"] = ("FORA",)
    lambda_entry.processar_mensagens_whatsapp([_msg("Qual o cardápio?")], cliente)

    texto = _textos_enviados(cliente)[-1]
    assert texto.startswith(lambda_entry.WHATSAPP_FORA_DA_PROGEP)
    assert texto.endswith(lambda_entry.WHATSAPP_OFERTA_LISTA)
    assert "@ifs.edu.br" not in texto
    loja.oferecer_transbordo.assert_called_once_with(
        "whatsapp", USUARIO, "Qual o cardápio?", estado=TRANSBORDO_LISTA
    )


def _lista_oferecida(loja):
    loja.estado_transbordo.return_value = EstadoTransbordo(estado=TRANSBORDO_LISTA)


def test_sim_para_a_lista_envia_todos_os_contatos(cliente, loja, agente):
    _lista_oferecida(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("sim")], cliente)

    texto = _textos_enviados(cliente)[-1]
    for email in ("progep@", "cpspp.progep@", "programadegestao@", "dap.progep@",
                  "dsdpes.progep@", "dnags.progep@", "siass@"):
        assert email in texto
    assert agente == []
    loja.limpar_transbordo.assert_called_once_with("whatsapp", USUARIO)


def test_nao_para_a_lista_encerra_sem_consultar(cliente, loja, agente):
    _lista_oferecida(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("não")], cliente)

    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_OFERTA_RECUSADA]
    assert agente == []


def test_pergunta_nova_descarta_a_oferta_de_lista(cliente, loja, agente):
    _lista_oferecida(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("Quantos dias de férias?")], cliente)

    loja.limpar_transbordo.assert_called_once_with("whatsapp", USUARIO)
    assert [r.text for r in agente] == ["Quantos dias de férias?"]


def test_resposta_respondida_nao_oferece(cliente, loja, ponte, expediente, agente):
    lambda_entry.processar_mensagens_whatsapp([_msg()], cliente)
    loja.oferecer_transbordo.assert_not_called()


# --- Transbordo: resposta à oferta ---------------------------------------------

def _oferecido(loja, pergunta="Pergunta sem documento"):
    loja.estado_transbordo.return_value = EstadoTransbordo(
        estado=TRANSBORDO_OFERECIDO, pergunta=pergunta
    )


def test_sim_abre_o_atendimento_com_a_pergunta(cliente, loja, ponte, expediente, agente):
    _oferecido(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("sim", mid="wamid.sim")], cliente)

    assert agente == []
    kw = ponte.abrir.call_args.kwargs
    assert kw["atributos"]["pergunta"] == "Pergunta sem documento"
    assert kw["atributos"]["whatsapp"] == f"+{USUARIO}"
    assert "Pergunta sem documento" in kw["contexto"]
    assert USUARIO not in kw["nome"]  # o nome exibido não expõe o número todo
    assert kw["client_token"] == "wamid.sim"
    loja.iniciar_atendimento_humano.assert_called_once_with(
        "whatsapp", USUARIO, ORIGEM,
        contato_id="c-1", participant_token="PT", connection_token="CT",
    )
    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_TRANSFERIDO]


def test_ok_na_oferta_e_aceite_e_nao_cortesia(cliente, loja, ponte, expediente, agente):
    """Fora da oferta, "ok" é cortesia; aqui é "quero"."""
    _oferecido(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("ok")], cliente)

    ponte.abrir.assert_called_once()
    assert lambda_entry.WHATSAPP_SOCIAIS[TipoSocial.CONFIRMACAO] not in _textos_enviados(cliente)


def test_nao_recusa_sem_chamar_o_agente(cliente, loja, ponte, expediente, agente):
    _oferecido(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("Não, obrigado")], cliente)

    assert agente == []
    ponte.abrir.assert_not_called()
    loja.limpar_transbordo.assert_called_once_with("whatsapp", USUARIO)
    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_OFERTA_RECUSADA]


def test_pergunta_nova_descarta_a_oferta_e_vai_ao_agente(
    cliente, loja, ponte, expediente, agente
):
    _oferecido(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("Quantos dias de férias?")], cliente)

    ponte.abrir.assert_not_called()
    loja.limpar_transbordo.assert_called_once_with("whatsapp", USUARIO)
    assert [r.text for r in agente] == ["Quantos dias de férias?"]


def test_aceite_depois_do_expediente_nao_abre(cliente, loja, ponte, expediente, agente):
    """Oferta às 17h55, "sim" às 18h05."""
    _oferecido(loja)
    expediente["aberto"] = False
    lambda_entry.processar_mensagens_whatsapp([_msg("sim")], cliente)

    ponte.abrir.assert_not_called()
    assert "9h às 18h" in _textos_enviados(cliente)[-1]
    assert not _textos_enviados(cliente)[-1].startswith("Não encontrei")


def test_falha_ao_abrir_avisa_o_servidor(cliente, loja, ponte, expediente, agente):
    _oferecido(loja)
    ponte.abrir.side_effect = RuntimeError("AccessDenied")
    lambda_entry.processar_mensagens_whatsapp([_msg("sim")], cliente)

    loja.iniciar_atendimento_humano.assert_not_called()
    loja.limpar_transbordo.assert_called_once()
    assert _textos_enviados(cliente) == [lambda_entry.WHATSAPP_TRANSBORDO_FALHOU]


# --- Transbordo: conversa com o atendente --------------------------------------

def _em_atendimento(loja):
    loja.estado_transbordo.return_value = EstadoTransbordo(
        estado=TRANSBORDO_HUMANO, contato_id="c-1",
        participant_token="PT", connection_token="CT",
    )


def test_com_atendente_a_mensagem_vai_ao_chat(cliente, loja, ponte, agente):
    _em_atendimento(loja)
    lambda_entry.processar_mensagens_whatsapp([_msg("Obrigado, aguardo")], cliente)

    ponte.enviar.assert_called_once_with("PT", "CT", "Obrigado, aguardo")
    assert agente == []
    cliente.send_text.assert_not_called()  # nem cortesia do assistente
    cliente.mark_read_and_typing.assert_not_called()  # quem digita é a pessoa
    loja.atualizar_conexao.assert_not_called()


def test_token_renovado_e_gravado(cliente, loja, ponte, agente):
    _em_atendimento(loja)
    ponte.enviar.return_value = "CT2"
    lambda_entry.processar_mensagens_whatsapp([_msg("oi")], cliente)

    loja.atualizar_conexao.assert_called_once_with("whatsapp", USUARIO, "CT2")


def test_chat_que_acabou_sem_aviso_volta_ao_assistente(cliente, loja, ponte, agente):
    _em_atendimento(loja)
    ponte.enviar.side_effect = ChatEncerrado("fim")
    lambda_entry.processar_mensagens_whatsapp([_msg("Quantos dias de férias?")], cliente)

    loja.limpar_transbordo.assert_called_once_with("whatsapp", USUARIO)
    assert [r.text for r in agente] == ["Quantos dias de férias?"]


# --- Transbordo: streaming do Connect → WhatsApp -------------------------------

def _stream(*corpos):
    registros = [
        {"Sns": {"Message": json.dumps({"ContactId": "c-1", "InitialContactId": "c-1", **c})}}
        for c in corpos
    ]
    return {"Records": registros}


@pytest.fixture
def envio_stream(monkeypatch, loja):
    fake = MagicMock()
    monkeypatch.setattr(lambda_entry, "_cliente_para_origem", lambda origem: fake)
    loja.conversa_do_atendimento.return_value = ConversaDoAtendimento(
        "whatsapp", USUARIO, ORIGEM
    )
    loja.finalizar_atendimento.return_value = True
    return fake


def test_fala_do_atendente_chega_ao_servidor(envio_stream, loja):
    saida = lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "MESSAGE", "ContentType": "text/plain", "ParticipantRole": "AGENT",
         "Content": "Bom dia, vou verificar."},
    ))

    assert saida["entregues"] == 1
    envio_stream.send_text.assert_called_once_with(ORIGEM, USUARIO, "Bom dia, vou verificar.")


def test_eco_do_servidor_e_sistema_nao_voltam(envio_stream, loja):
    lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "MESSAGE", "ParticipantRole": "CUSTOMER", "Content": "minha pergunta"},
        {"Type": "MESSAGE", "ParticipantRole": "SYSTEM", "Content": "Thank you for waiting"},
        {"Type": "EVENT", "ContentType": "application/vnd.amazonaws.connect.event.typing",
         "ParticipantRole": "AGENT"},
    ))

    envio_stream.send_text.assert_not_called()
    loja.conversa_do_atendimento.assert_not_called()


def test_entrada_do_atendente_e_anunciada_com_o_nome(envio_stream, loja):
    lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "EVENT", "ContentType": "application/vnd.amazonaws.connect.event.participant.joined",
         "ParticipantRole": "AGENT", "DisplayName": "Julia"},
    ))
    assert "Julia" in envio_stream.send_text.call_args.args[2]


def test_fim_em_dobro_avisa_uma_vez(envio_stream, loja):
    loja.finalizar_atendimento.side_effect = [True, False]
    lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "EVENT", "ContentType": "application/vnd.amazonaws.connect.event.participant.left",
         "ParticipantRole": "AGENT"},
        {"Type": "EVENT", "ContentType": "application/vnd.amazonaws.connect.event.chat.ended"},
    ))

    textos = [c.args[2] for c in envio_stream.send_text.call_args_list]
    assert textos == [lambda_entry.WHATSAPP_ATENDIMENTO_ENCERRADO]
    loja.finalizar_atendimento.assert_called_with("whatsapp", USUARIO, "c-1")


def test_contato_sem_conversa_ligada_e_ignorado(envio_stream, loja):
    loja.conversa_do_atendimento.return_value = None
    saida = lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "MESSAGE", "ParticipantRole": "AGENT", "Content": "oi"},
    ))
    assert saida["entregues"] == 0
    envio_stream.send_text.assert_not_called()


def test_falha_num_envio_nao_para_os_demais(envio_stream, loja):
    envio_stream.send_text.side_effect = [RuntimeError("fora da janela"), {"ok": 1}]
    saida = lambda_entry.connect_chat_stream_handler(_stream(
        {"Type": "MESSAGE", "ParticipantRole": "AGENT", "Content": "um"},
        {"Type": "MESSAGE", "ParticipantRole": "AGENT", "Content": "dois"},
    ))
    assert saida["entregues"] == 1
