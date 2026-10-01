"""Testes do ciclo de vida da conversa (Req 4.1, 4.3).

Dois princípios sustentam a maioria dos casos aqui:

* perder o vínculo custa **continuidade**, nunca a resposta — toda falha de I/O
  é absorvida;
* contexto velho não deve ser retomado, e o encerramento não deve ser repetido.
"""

from __future__ import annotations

import time

import pytest

from src.adapters.conversation_store import (
    JANELA_MIN,
    TRANSBORDO_HUMANO,
    TRANSBORDO_LISTA,
    TRANSBORDO_OFERECIDO,
    ConversationStore,
    EstadoTransbordo,
    atendimento_key,
    conversation_key,
)

pytestmark = pytest.mark.unit


def _item(
    *, sessao="sess-abc", minutos_atras=0, fechada=False, pnid="PNID-1", salva_min_atras=None
):
    salva = minutos_atras if salva_min_atras is None else salva_min_atras
    return {
        "session_id": {"S": conversation_key("whatsapp", "5579999")},
        "provider_session_id": {"S": sessao},
        "session_saved_at": {"N": str(int(time.time()) - salva * 60)},
        "last_activity": {"N": str(int(time.time()) - minutos_atras * 60)},
        "closed": {"BOOL": fechada},
        "phone_number_id": {"S": pnid},
        "channel": {"S": "whatsapp"},
    }


class FakeDynamo:
    def __init__(self, item=None, *, boom: bool = False, scan_items=None):
        self.item = item
        self.boom = boom
        self.scan_items = scan_items or []
        self.updates: list[dict] = []
        self.puts: list[dict] = []
        self.condicional_falha = False

    def get_item(self, **kwargs):
        if self.boom:
            raise RuntimeError("dynamo indisponível")
        return {"Item": self.item} if self.item else {}

    def put_item(self, **kwargs):
        if self.boom:
            raise RuntimeError("dynamo indisponível")
        if self.condicional_falha and "ConditionExpression" in kwargs:
            erro = RuntimeError("condição")
            erro.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise erro
        self.puts.append(kwargs)

    def update_item(self, **kwargs):
        if self.boom:
            raise RuntimeError("dynamo indisponível")
        if self.condicional_falha and "ConditionExpression" in kwargs:
            erro = RuntimeError("condição")
            erro.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise erro
        self.updates.append(kwargs)

    def get_paginator(self, nome):
        if self.boom:
            raise RuntimeError("dynamo indisponível")
        itens = self.scan_items
        outer = self

        class _P:
            def paginate(self, **kwargs):
                outer.scan_kwargs = kwargs
                return [{"Items": itens}]

        return _P()


# --- chave --------------------------------------------------------------------

def test_key_isolates_from_domain_sessions():
    assert conversation_key("whatsapp", "5579999") == "conv#whatsapp#5579999"


# --- retomada de contexto -----------------------------------------------------

def test_get_returns_session_within_window():
    store = ConversationStore("t", client=FakeDynamo(_item(minutos_atras=2)))
    assert store.get("whatsapp", "5579999") == "sess-abc"


def test_get_ignores_session_past_the_window():
    """O item ainda existe (TTL é longo), mas o assunto já esfriou."""
    store = ConversationStore("t", client=FakeDynamo(_item(minutos_atras=JANELA_MIN + 5)))
    assert store.get("whatsapp", "5579999") == ""


def test_get_nao_retoma_sessao_velha_mesmo_com_atividade_recente():
    """A Lambda grava a atividade **antes** de chamar o runtime. Medir a janela
    por ela reenviava ao Bedrock uma sessão de dois dias atrás (17/09/2026)."""
    item = _item(minutos_atras=0, salva_min_atras=48 * 60)
    assert ConversationStore("t", client=FakeDynamo(item)).get("whatsapp", "5579999") == ""


def test_get_nao_retoma_item_sem_marca_de_quando_a_sessao_foi_salva():
    item = _item(minutos_atras=0)
    item.pop("session_saved_at")
    assert ConversationStore("t", client=FakeDynamo(item)).get("whatsapp", "5579999") == ""


def test_salvar_sessao_marca_quando_foi_salva():
    dynamo = FakeDynamo()
    ConversationStore("t", client=dynamo).salvar_sessao("whatsapp", "5579999", "sess-x")
    valores = dynamo.updates[0]["ExpressionAttributeValues"]
    assert abs(int(valores[":agora"]["N"]) - time.time()) < 5
    assert "session_saved_at" in dynamo.updates[0]["UpdateExpression"]


def test_get_absorbs_failure():
    store = ConversationStore("t", client=FakeDynamo(boom=True))
    assert store.get("whatsapp", "5579999") == ""


# --- saudação -----------------------------------------------------------------

def test_conversa_aberta_true_when_recent():
    store = ConversationStore("t", client=FakeDynamo(_item(minutos_atras=1)))
    assert store.conversa_aberta("whatsapp", "5579999") is True


def test_conversa_aberta_false_when_never_talked():
    store = ConversationStore("t", client=FakeDynamo(None))
    assert store.conversa_aberta("whatsapp", "5579999") is False


def test_conversa_aberta_false_after_closing():
    """Quem foi encerrado é cumprimentado de novo ao voltar."""
    store = ConversationStore("t", client=FakeDynamo(_item(fechada=True, minutos_atras=1)))
    assert store.conversa_aberta("whatsapp", "5579999") is False


def test_conversa_aberta_false_past_the_window():
    store = ConversationStore(
        "t", client=FakeDynamo(_item(minutos_atras=JANELA_MIN + 1))
    )
    assert store.conversa_aberta("whatsapp", "5579999") is False


# --- escrita ------------------------------------------------------------------

def test_registrar_atividade_reopens_and_stores_origin():
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)

    assert store.registrar_atividade("whatsapp", "5579999", "PNID-9") is True
    kwargs = dynamo.updates[0]
    assert kwargs["ExpressionAttributeValues"][":aberta"]["BOOL"] is False
    assert kwargs["ExpressionAttributeValues"][":pnid"]["S"] == "PNID-9"
    # ``ttl`` é palavra reservada e precisa de alias.
    assert kwargs["ExpressionAttributeNames"] == {"#t": "ttl"}


def test_salvar_sessao_only_touches_the_session():
    """Escritas disjuntas: o runtime não pode apagar o que a Lambda gravou."""
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)

    store.salvar_sessao("whatsapp", "5579999", "sess-xyz")
    expr = dynamo.updates[0]["UpdateExpression"]
    assert "provider_session_id" in expr
    assert "last_activity" not in expr and "closed" not in expr


def test_writes_absorb_failure():
    store = ConversationStore("t", client=FakeDynamo(boom=True))
    assert store.registrar_atividade("whatsapp", "5579999") is False
    assert store.salvar_sessao("whatsapp", "5579999", "s") is False


@pytest.mark.parametrize("user_key,sessao", [("", "s"), ("5579999", ""), ("", "")])
def test_salvar_sessao_skips_incomplete(user_key, sessao):
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)
    assert store.salvar_sessao("whatsapp", user_key, sessao) is False
    assert dynamo.updates == []


# --- varredura de encerramento ------------------------------------------------

def test_listar_inativas_extracts_user_and_origin():
    dynamo = FakeDynamo(scan_items=[_item(minutos_atras=JANELA_MIN + 3, pnid="PNID-7")])
    store = ConversationStore("t", client=dynamo)

    inativas = store.listar_inativas("whatsapp")

    assert len(inativas) == 1
    assert inativas[0].user_key == "5579999"
    assert inativas[0].phone_number_id == "PNID-7"
    assert inativas[0].channel == "whatsapp"
    # O filtro pega só conversa aberta e parada além da janela.
    valores = dynamo.scan_kwargs["ExpressionAttributeValues"]
    assert valores[":aberta"]["BOOL"] is False
    assert int(valores[":corte"]["N"]) <= int(time.time()) - JANELA_MIN * 60


def test_listar_inativas_absorbs_failure():
    store = ConversationStore("t", client=FakeDynamo(boom=True))
    assert store.listar_inativas("whatsapp") == []


def test_marcar_encerrada_sets_the_flag():
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)

    assert store.marcar_encerrada("whatsapp", "5579999") is True
    assert dynamo.updates[0]["ExpressionAttributeValues"][":fechada"]["BOOL"] is True


# --- transbordo humano --------------------------------------------------------

def test_varredura_nao_encerra_conversa_com_atendente():
    """Silêncio com atendente é a fila andando, não o servidor que foi embora."""
    dynamo = FakeDynamo(scan_items=[])
    ConversationStore("t", client=dynamo).listar_inativas("whatsapp")

    filtro = dynamo.scan_kwargs["FilterExpression"]
    assert "transbordo <> :humano" in filtro
    assert "attribute_not_exists(transbordo)" in filtro
    assert dynamo.scan_kwargs["ExpressionAttributeValues"][":humano"]["S"] == TRANSBORDO_HUMANO


def test_sem_transbordo_o_estado_e_vazio():
    store = ConversationStore("t", client=FakeDynamo(_item()))
    estado = store.estado_transbordo("whatsapp", "5579999")
    assert estado == EstadoTransbordo()
    assert not estado.em_atendimento_humano


def test_oferta_recente_vale():
    item = _item() | {
        "transbordo": {"S": TRANSBORDO_OFERECIDO},
        "transbordo_em": {"N": str(int(time.time()) - 60)},
        "pergunta_pendente": {"S": "cardápio do restaurante?"},
    }
    estado = ConversationStore("t", client=FakeDynamo(item)).estado_transbordo("whatsapp", "5579999")
    assert estado.estado == TRANSBORDO_OFERECIDO
    assert estado.pergunta == "cardápio do restaurante?"


def test_oferta_vence_com_a_janela():
    """Um "sim" uma hora depois responde a outra coisa."""
    item = _item() | {
        "transbordo": {"S": TRANSBORDO_OFERECIDO},
        "transbordo_em": {"N": str(int(time.time()) - (JANELA_MIN + 1) * 60)},
    }
    estado = ConversationStore("t", client=FakeDynamo(item)).estado_transbordo("whatsapp", "5579999")
    assert estado.estado == ""


def test_oferta_de_lista_tambem_vence_com_a_janela():
    item = _item() | {
        "transbordo": {"S": TRANSBORDO_LISTA},
        "transbordo_em": {"N": str(int(time.time()) - (JANELA_MIN + 1) * 60)},
    }
    estado = ConversationStore("t", client=FakeDynamo(item)).estado_transbordo("whatsapp", "5579999")
    assert estado.estado == ""


def test_oferecer_lista_grava_o_estado_pedido():
    dynamo = FakeDynamo()
    ConversationStore("t", client=dynamo).oferecer_transbordo(
        "whatsapp", "5579999", "cardápio?", estado=TRANSBORDO_LISTA
    )
    assert dynamo.updates[0]["ExpressionAttributeValues"][":estado"]["S"] == TRANSBORDO_LISTA


def test_atendimento_humano_nao_vence_com_a_janela():
    item = _item() | {
        "transbordo": {"S": TRANSBORDO_HUMANO},
        "transbordo_em": {"N": str(int(time.time()) - 3 * 3600)},
        "contato_id": {"S": "c-1"},
        "participant_token": {"S": "PT"},
        "connection_token": {"S": "CT"},
    }
    estado = ConversationStore("t", client=FakeDynamo(item)).estado_transbordo("whatsapp", "5579999")
    assert estado.em_atendimento_humano
    assert (estado.participant_token, estado.connection_token) == ("PT", "CT")


def test_iniciar_atendimento_grava_indice_reverso_e_estado():
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)

    assert store.iniciar_atendimento_humano(
        "whatsapp", "5579999", "arn:pnid",
        contato_id="c-1", participant_token="PT", connection_token="CT",
    ) is True

    indice = dynamo.puts[0]["Item"]
    assert indice["session_id"]["S"] == atendimento_key("c-1")
    assert indice["user_key"]["S"] == "5579999"
    assert indice["phone_number_id"]["S"] == "arn:pnid"
    valores = dynamo.updates[0]["ExpressionAttributeValues"]
    assert valores[":estado"]["S"] == TRANSBORDO_HUMANO
    assert valores[":cid"]["S"] == "c-1"


def test_sem_indice_nao_marca_atendimento():
    """Sem o índice, a fala do atendente não teria para onde ir."""
    dynamo = FakeDynamo(boom=True)
    store = ConversationStore("t", client=dynamo)
    assert store.iniciar_atendimento_humano(
        "whatsapp", "5579999", "p", contato_id="c-1", participant_token="PT",
        connection_token="CT",
    ) is False


def test_conversa_do_atendimento_pelo_indice():
    indice = {
        "session_id": {"S": atendimento_key("c-1")},
        "user_key": {"S": "5579999"},
        "channel": {"S": "whatsapp"},
        "phone_number_id": {"S": "arn:pnid"},
    }
    conversa = ConversationStore("t", client=FakeDynamo(indice)).conversa_do_atendimento("c-1")
    assert (conversa.user_key, conversa.phone_number_id) == ("5579999", "arn:pnid")


def test_contato_desconhecido_nao_tem_conversa():
    assert ConversationStore("t", client=FakeDynamo(None)).conversa_do_atendimento("c-9") is None


def test_finalizar_so_vence_uma_vez():
    """O fim chega em dois eventos; o aviso não pode sair em dobro."""
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)
    assert store.finalizar_atendimento("whatsapp", "5579999", "c-1") is True
    assert dynamo.updates[0]["ConditionExpression"] == "contato_id = :cid"

    dynamo.condicional_falha = True
    assert store.finalizar_atendimento("whatsapp", "5579999", "c-1") is False


def test_mensagem_nova_e_marcada_uma_vez():
    dynamo = FakeDynamo()
    store = ConversationStore("t", client=dynamo)

    assert store.marcar_mensagem_processada("wamid.ABC") is True
    put = dynamo.puts[0]
    assert put["Item"]["session_id"]["S"].endswith("wamid.ABC")
    # Condicional: duas execuções simultâneas, só uma vence.
    assert put["ConditionExpression"] == "attribute_not_exists(session_id)"


def test_mensagem_repetida_devolve_falso():
    dynamo = FakeDynamo()
    dynamo.condicional_falha = True
    store = ConversationStore("t", client=dynamo)

    assert store.marcar_mensagem_processada("wamid.ABC") is False


def test_falha_de_io_na_marca_nao_impede_a_resposta():
    """Responder duas vezes é ruim; não responder é pior."""
    store = ConversationStore("t", client=FakeDynamo(boom=True))
    assert store.marcar_mensagem_processada("wamid.ABC") is True


def test_limpar_remove_tokens():
    dynamo = FakeDynamo()
    ConversationStore("t", client=dynamo).limpar_transbordo("whatsapp", "5579999")
    expr = dynamo.updates[0]["UpdateExpression"]
    assert expr.startswith("REMOVE")
    assert "participant_token" in expr and "connection_token" in expr
    assert "ExpressionAttributeValues" not in dynamo.updates[0]
