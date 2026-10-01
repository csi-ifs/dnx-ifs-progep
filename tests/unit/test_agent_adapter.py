"""Unit tests for the Bedrock Agente_IA adapter (Task 12.3).

Verify request forwarding, Knowledge Base retrieval with score/reference
recovery (Req 2.1, 2.2) and the mandatory billing guard applied before any
Bedrock call, including ``User_Agent_APN`` injection via
``Config(user_agent_extra=...)`` (Req 8.3, 8.5).

The Knowledge Base client is a lightweight fake, and boto3 is patched so no AWS
access is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.adapters import agent as agent_module
from src.adapters.agent import (
    BedrockAgentAdapter,
    BedrockUnavailableError,
    BillingGuardError,
    KnowledgeBaseResult,
    parse_retrieval_results,
)
from src.domain.billing import BILLING_ERROR_CODE
from src.domain.config import USER_AGENT_APN, AgentStatus
from src.domain.models import AgentRequest


pytestmark = pytest.mark.unit


class FakeKbClient:
    """A stand-in for the boto3 ``bedrock-agent-runtime`` client."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _kb_response(*scored_uris):
    return {
        "retrievalResults": [
            {
                "content": {"text": f"trecho {i}"},
                "location": {"s3Location": {"uri": uri}},
                "score": score,
            }
            for i, (uri, score) in enumerate(scored_uris)
        ]
    }


def _request(text="Como solicito ferias?"):
    return AgentRequest(session_id="s-1", channel="site", text=text)


# --- Retrieval parsing ----------------------------------------------------------

def test_parse_retrieval_results_extracts_doc_id_and_score():
    results = _kb_response(("s3://kb/doc-a.pdf", 0.91), ("s3://kb/doc-b.pdf", 0.42))[
        "retrievalResults"
    ]
    refs = parse_retrieval_results(results)
    assert [(r.doc_id, r.score) for r in refs] == [
        ("s3://kb/doc-a.pdf", 0.91),
        ("s3://kb/doc-b.pdf", 0.42),
    ]


def test_parse_retrieval_results_missing_score_defaults_to_zero():
    refs = parse_retrieval_results([{"location": {"s3Location": {"uri": "u"}}}])
    assert refs[0].score == 0.0
    assert refs[0].doc_id == "u"


def test_parse_retrieval_results_uses_metadata_source_uri_fallback():
    refs = parse_retrieval_results(
        [{"metadata": {"x-amz-bedrock-kb-source-uri": "s3://kb/meta.pdf"}, "score": 0.5}]
    )
    assert refs[0].doc_id == "s3://kb/meta.pdf"


# --- Knowledge Base query -------------------------------------------------------

def test_query_knowledge_base_recovers_scores_and_refs():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.8), ("s3://kb/b.pdf", 0.6)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client)

    result = adapter.query_knowledge_base(_request(), threshold=0.5)

    assert isinstance(result, KnowledgeBaseResult)
    assert result.scores == (0.8, 0.6)
    assert [r.doc_id for r in result.sources] == ["s3://kb/a.pdf", "s3://kb/b.pdf"]
    assert result.relevance.relevant is True
    # The KB id and query text are forwarded to the client (Req 2.1).
    assert client.calls[0]["knowledgeBaseId"] == "kb-123"
    assert client.calls[0]["retrievalQuery"]["text"] == "Como solicito ferias?"


def test_query_knowledge_base_no_relevant_docs_triggers_escalation():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.2)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client)

    result = adapter.query_knowledge_base(_request(), threshold=0.7)

    assert result.relevance.relevant is False
    assert result.relevance.escalate is True


# --- forward_request ------------------------------------------------------------

def test_forward_request_answered_includes_one_ref_per_document():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.9), ("s3://kb/b.pdf", 0.75)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client)

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert resp.status == AgentStatus.ANSWERED.value
    assert {s.doc_id for s in resp.sources} == {"s3://kb/a.pdf", "s3://kb/b.pdf"}


def test_forward_request_not_found_when_no_document_meets_threshold():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.3)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client)

    resp = adapter.forward_request(_request(), threshold=0.9)

    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.sources == ()


def test_forward_request_grounds_answer_only_on_relevant_documents():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.9), ("s3://kb/b.pdf", 0.1)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client)

    resp = adapter.forward_request(_request(), threshold=0.5)

    doc_ids = {s.doc_id for s in resp.sources}
    assert doc_ids == {"s3://kb/a.pdf"}
    assert "s3://kb/b.pdf" not in resp.content


# --- Billing guard (Req 8.3, 8.5) -----------------------------------------------

def test_billing_guard_blocks_call_when_user_agent_missing():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.9)))
    adapter = BedrockAgentAdapter("kb-123", kb_client=client, user_agent_extra=None)

    with pytest.raises(BillingGuardError) as exc:
        adapter.query_knowledge_base(_request(), threshold=0.5)

    assert exc.value.error.code == BILLING_ERROR_CODE
    # The Bedrock call must NOT have been dispatched.
    assert client.calls == []


def test_billing_guard_blocks_call_when_user_agent_incorrect():
    client = FakeKbClient(_kb_response(("s3://kb/a.pdf", 0.9)))
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, user_agent_extra="WRONG/value"
    )

    with pytest.raises(BillingGuardError):
        adapter.forward_request(_request(), threshold=0.5)
    assert client.calls == []


def test_billing_allowed_with_default_user_agent():
    adapter = BedrockAgentAdapter("kb-123", kb_client=FakeKbClient(_kb_response()))
    result = adapter.check_billing()
    assert result.allowed is True
    assert adapter.billing_config["user_agent_extra"] == USER_AGENT_APN


# --- Client construction / User_Agent_APN injection -----------------------------

def test_build_client_injects_user_agent_apn_via_config():
    fake_boto3 = MagicMock()
    fake_config_cls = MagicMock()
    with patch.object(agent_module, "boto3", fake_boto3), patch.object(
        agent_module, "Config", fake_config_cls
    ), patch.object(agent_module, "_BOTO3_AVAILABLE", True):
        adapter = BedrockAgentAdapter("kb-123", region_name="us-east-1")
        client = adapter._get_kb_client()

    fake_config_cls.assert_called_once_with(user_agent_extra=USER_AGENT_APN)
    fake_boto3.client.assert_called_once()
    args, kwargs = fake_boto3.client.call_args
    assert args[0] == "bedrock-agent-runtime"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["config"] is fake_config_cls.return_value
    assert client is fake_boto3.client.return_value


def test_missing_boto3_without_injected_client_raises():
    with patch.object(agent_module, "_BOTO3_AVAILABLE", False):
        adapter = BedrockAgentAdapter("kb-123")
        with pytest.raises(BedrockUnavailableError):
            adapter._get_kb_client()


# --- Geração LLM fundamentada (retrieve_and_generate) ---------------------------

class FakeRagClient(FakeKbClient):
    """Fake com suporte a retrieve_and_generate além de retrieve."""

    def __init__(self, retrieve_response, rag_response):
        super().__init__(retrieve_response)
        self._rag_response = rag_response
        self.rag_calls: list[dict] = []

    def retrieve_and_generate(self, **kwargs):
        self.rag_calls.append(kwargs)
        return self._rag_response


def _rag_response(text, *cited_uris, session_id="sess-bedrock-1"):
    return {
        "sessionId": session_id,
        "output": {"text": text},
        "citations": [
            {
                "retrievedReferences": [
                    {"location": {"s3Location": {"uri": uri}}} for uri in cited_uris
                ]
            }
        ],
    }


def test_parse_citations_dedupes_and_preserves_order():
    citations = [
        {"retrievedReferences": [
            {"location": {"s3Location": {"uri": "s3://kb/a.pdf"}}},
            {"location": {"s3Location": {"uri": "s3://kb/b.pdf"}}},
        ]},
        {"retrievedReferences": [
            {"location": {"s3Location": {"uri": "s3://kb/a.pdf"}}},
        ]},
    ]
    assert agent_module.parse_citations(citations) == (
        "s3://kb/a.pdf", "s3://kb/b.pdf",
    )


def test_forward_request_with_model_generates_grounded_answer():
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8), ("s3://kb/b.pdf", 0.6)),
        _rag_response("As férias são solicitadas via SIGRH.", "s3://kb/a.pdf"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert resp.status == AgentStatus.ANSWERED.value
    assert resp.content == "As férias são solicitadas via SIGRH."
    # Uma referência por documento citado, com score vindo do retrieve (Req 2.3).
    assert [(s.doc_id, s.score) for s in resp.sources] == [("s3://kb/a.pdf", 0.8)]
    # A configuração da chamada aponta para a KB e o modelo corretos.
    rag_cfg = client.rag_calls[0]["retrieveAndGenerateConfiguration"]
    assert rag_cfg["knowledgeBaseConfiguration"]["knowledgeBaseId"] == "kb-123"


def test_forward_request_ungrounded_generation_becomes_not_found():
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        {"output": {"text": "Desculpe, não sei."}, "citations": []},
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.sources == ()


def test_generation_uses_progep_prompt_template():
    """A geração leva o template da persona (senão o modelo comenta o retrieval)."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("As férias são solicitadas via SIGRH.", "s3://kb/a.pdf"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    adapter.forward_request(_request(), threshold=0.5)

    kb_cfg = client.rag_calls[0]["retrieveAndGenerateConfiguration"][
        "knowledgeBaseConfiguration"
    ]
    template = kb_cfg["generationConfiguration"]["promptTemplate"]["textPromptTemplate"]
    # Os placeholders do Bedrock precisam sobreviver a qualquer edição do prompt.
    assert "$search_results$" in template
    assert "$output_format_instructions$" in template
    assert agent_module.NO_ANSWER_SENTINEL in template


@pytest.mark.parametrize(
    "text",
    [
        agent_module.NO_ANSWER_SENTINEL,
        f"{agent_module.NO_ANSWER_SENTINEL}.",
        "Os resultados da busca fornecidos não contêm informações sobre isso.",
        "Não foi possível encontrar informações nos documentos fornecidos.",
        "Os documentos fornecidos não trazem informações sobre como consultar a "
        "nova data de uma perícia remarcada.",
        "Os documentos apresentados não mencionam o atendimento aos sábados.",
        "Infelizmente, os documentos não trazem informações sobre como verificar "
        "a nova data de uma perícia remarcada. Recomendo confirmar com a PROGEP.",
        "   ",
    ],
)
def test_is_no_answer_detects_negatives(text):
    assert agent_module.is_no_answer(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "A licença capacitação é solicitada via SIGEPE, com 30 dias de antecedência.",
        "Não há limite de participantes; o prazo consta na IN 01-2019.",
        "A portaria não traz exceção para docentes: o prazo é de 30 dias.",
        "Os documentos não precisam ser autenticados em cartório.",
        "Envie os documentos pelo SouGov; a cópia não contém dados sigilosos.",
    ],
)
def test_is_no_answer_keeps_real_answers(text):
    assert agent_module.is_no_answer(text) is False


def test_forward_request_sentinel_with_citations_becomes_not_found():
    """Negativa *com* citações também escala: nada respondeu à pergunta."""
    client = FakeRagClient(
        _kb_response(("s3://kb/edital.pdf", 0.8)),
        _rag_response(agent_module.NO_ANSWER_SENTINEL, "s3://kb/edital.pdf"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.content == ""
    assert resp.sources == ()


def test_forward_request_below_threshold_is_not_found():
    """Sem documento acima do threshold, a resposta é ``not_found`` (Req 2.4).

    A geração roda **em paralelo** com o ``retrieve`` (é o que elimina a busca
    dupla), então o texto gerado pode existir — mas é descartado. A garantia
    aqui é sobre o que chega ao usuário, não sobre economizar a chamada: medido
    no corpus atual, o threshold nunca antecipa a saída, então não há economia
    a preservar.
    """
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.2)),
        _rag_response("não deve ser entregue", "s3://kb/a.pdf"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert resp.status == AgentStatus.NOT_FOUND.value
    assert resp.content == ""
    assert resp.sources == ()


def test_forward_request_runs_retrieve_and_generation_in_parallel():
    """Ambas as chamadas partem, e a resposta usa os scores do ``retrieve``."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("As férias são solicitadas via SIGRH.", "s3://kb/a.pdf"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    resp = adapter.forward_request(_request(), threshold=0.5)

    assert len(client.calls) == 1  # um retrieve
    assert len(client.rag_calls) == 1  # uma geração
    # O score exibido vem do retrieve, que o retrieve_and_generate não devolve.
    assert [(s.doc_id, s.score) for s in resp.sources] == [("s3://kb/a.pdf", 0.8)]


def test_generate_grounded_answer_requires_model_arn():
    adapter = BedrockAgentAdapter("kb-123", kb_client=FakeKbClient({}))
    with pytest.raises(ValueError):
        adapter.generate_grounded_answer(_request())


def test_generate_grounded_answer_blocked_without_billing_annotation():
    client = FakeRagClient(_kb_response(), _rag_response("x", "s3://kb/a.pdf"))
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, user_agent_extra=None,
        model_arn="arn:aws:bedrock:...:ip/global.model",
    )
    with pytest.raises(BillingGuardError):
        adapter.generate_grounded_answer(_request())
    assert client.rag_calls == []  # chamada bloqueada antes do Bedrock (Req 8.5)


# --- Conversa com contexto (sessionId do Bedrock) -----------------------------

def test_forward_turn_sends_session_id_and_returns_the_new_one():
    """A alça de conversa vai na chamada e volta para o próximo turno."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("Com 90 dias de antecedência.", "s3://kb/a.pdf",
                      session_id="sess-bedrock-9"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    turno = adapter.forward_turn(
        _request(), threshold=0.5, provider_session_id="sess-bedrock-9"
    )

    assert client.rag_calls[0]["sessionId"] == "sess-bedrock-9"
    assert turno.provider_session_id == "sess-bedrock-9"
    assert turno.response.status == AgentStatus.ANSWERED.value


def test_forward_turn_without_session_omits_the_parameter():
    """No primeiro turno o Bedrock gera o id — não se manda campo vazio.

    Mandar um id inventado é recusado com ValidationException ("Session with Id
    ... is not valid"), então a ausência precisa ser ausência mesmo.
    """
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("Resposta.", "s3://kb/a.pdf", session_id="sess-novo"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    turno = adapter.forward_turn(_request(), threshold=0.5)

    assert "sessionId" not in client.rag_calls[0]
    assert turno.provider_session_id == "sess-novo"


class FakeRagSessaoVencida(FakeRagClient):
    """Recusa a primeira chamada **com** sessão, como o Bedrock faz ao expirá-la."""

    def __init__(self, retrieve_response, rag_response, erro=None):
        super().__init__(retrieve_response, rag_response)
        self._erro = erro

    def retrieve_and_generate(self, **kwargs):
        if "sessionId" in kwargs:
            self.rag_calls.append(kwargs)
            raise self._erro
        return super().retrieve_and_generate(**kwargs)


def _erro_bedrock(codigo, mensagem):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": codigo, "Message": mensagem}}, "RetrieveAndGenerate")


def test_sessao_vencida_repete_sem_sessao_e_devolve_a_nova():
    """Em 17/09/2026 uma sessão do dia anterior virou "temporariamente
    indisponível". Perder o contexto é melhor que perder a resposta."""
    client = FakeRagSessaoVencida(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("São 30 dias.", "s3://kb/a.pdf", session_id="sess-nova"),
        erro=_erro_bedrock(
            "ValidationException", "Session with Id sess-velha is not valid. Please check."
        ),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    turno = adapter.forward_turn(_request(), threshold=0.5, provider_session_id="sess-velha")

    assert [c.get("sessionId") for c in client.rag_calls] == ["sess-velha", None]
    assert turno.response.status == AgentStatus.ANSWERED.value
    assert turno.provider_session_id == "sess-nova"


def test_outra_validacao_nao_e_mascarada_como_sessao():
    """Só a recusa de sessão é repetida; qualquer outro erro precisa aparecer."""
    client = FakeRagSessaoVencida(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("x", "s3://kb/a.pdf"),
        erro=_erro_bedrock("ValidationException", "numberOfResults must be <= 100"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    with pytest.raises(Exception, match="numberOfResults"):
        adapter.generate_grounded_answer(_request(), provider_session_id="sess-velha")
    assert len(client.rag_calls) == 1


class FakeConverse:
    def __init__(self, texto="DAP", boom=False):
        self.texto = texto
        self.boom = boom
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError("bedrock fora")
        return {"output": {"message": {"content": [{"text": self.texto}]}}}


def test_classificar_setor_usa_o_modelo_e_o_catalogo():
    llm = FakeConverse("SAUDE")
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=FakeKbClient({}), llm_client=llm,
        model_arn="arn:aws:bedrock:...:ip/global.model",
    )

    assert adapter.classificar_setor("Como marco a perícia?") == ("SIASS", "DAP")
    kw = llm.calls[0]
    assert kw["modelId"] == "arn:aws:bedrock:...:ip/global.model"
    assert "DSDPES" in kw["system"][0]["text"]
    assert kw["messages"][0]["content"][0]["text"] == "Como marco a perícia?"


def test_classificar_setor_bloqueado_sem_anotacao_de_billing():
    llm = FakeConverse()
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=FakeKbClient({}), llm_client=llm, user_agent_extra=None,
        model_arn="arn:aws:bedrock:...:ip/global.model",
    )
    with pytest.raises(BillingGuardError):
        adapter.classificar_setor("x")
    assert llm.calls == []  # Req 8.5: nada sai sem a anotação


def test_forward_turn_keeps_session_when_escalating():
    """Escalonar não pode perder o fio da conversa."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response(agent_module.NO_ANSWER_SENTINEL, "s3://kb/a.pdf",
                      session_id="sess-mantida"),
    )
    adapter = BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )

    turno = adapter.forward_turn(_request(), threshold=0.5)

    assert turno.response.status == AgentStatus.NOT_FOUND.value
    assert turno.provider_session_id == "sess-mantida"


# --- Motivo da decisão (depuração) --------------------------------------------
# Um ``not_found`` sai por três caminhos com correções diferentes, e o status
# sozinho não os separa. Um relato de "não respondeu" que não se reproduz só é
# investigável se o motivo tiver sido registrado na hora.

def _adapter(client):
    return BedrockAgentAdapter(
        "kb-123", kb_client=client, model_arn="arn:aws:bedrock:...:ip/global.model"
    )


def test_motivo_ok_conta_trechos_e_citacoes():
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8), ("s3://kb/b.pdf", 0.7)),
        _rag_response("As férias são de 30 dias.", "s3://kb/a.pdf"),
    )

    turno = _adapter(client).forward_turn(_request(), threshold=0.5)

    assert turno.motivo == "ok"
    assert (turno.trechos, turno.citacoes) == (2, 1)


def test_motivo_sem_relevancia_quando_nada_passa_do_corte():
    """Hoje este caminho é raro a ponto de indicar falha de busca, não pergunta
    fora de escopo — daí valer a pena distingui-lo dos demais."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.2)),
        _rag_response("texto que será descartado", "s3://kb/a.pdf"),
    )

    turno = _adapter(client).forward_turn(_request(), threshold=0.5)

    assert turno.response.status == AgentStatus.NOT_FOUND.value
    assert turno.motivo == "sem_relevancia"


def test_motivo_sem_citacao_quando_a_resposta_nao_tem_referencia():
    """Resposta plausível sem referência nenhuma: descartada por não ter como
    ser sustentada — problema diferente do modelo dizer que não sabe."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response("Resposta sem nenhuma referência."),
    )

    turno = _adapter(client).forward_turn(_request(), threshold=0.5)

    assert turno.response.status == AgentStatus.NOT_FOUND.value
    assert turno.motivo == "sem_citacao"
    assert turno.citacoes == 0


def test_motivo_sentinela_quando_o_modelo_diz_que_nao_encontrou():
    """Aqui a correção é de corpus ou de prompt, nunca de infraestrutura."""
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response(agent_module.NO_ANSWER_SENTINEL, "s3://kb/a.pdf"),
    )

    turno = _adapter(client).forward_turn(_request(), threshold=0.5)

    assert turno.response.status == AgentStatus.NOT_FOUND.value
    assert turno.motivo == "sentinela"
    assert turno.citacoes == 1  # citou, mas não respondeu


def test_sentinela_sem_citacao_ainda_e_sentinela():
    """A forma real de uma recusa: quem diz "não encontrei" não cita nada.

    Regressão: com o teste de citação vindo primeiro, **toda** recusa legítima
    era rotulada ``sem_citacao`` e o motivo deixava de distinguir o que existe
    para distinguir. Verificado ao vivo com "quem ganhou o brasileirão".
    """
    client = FakeRagClient(
        _kb_response(("s3://kb/a.pdf", 0.8)),
        _rag_response(agent_module.NO_ANSWER_SENTINEL),  # sem referência alguma
    )

    turno = _adapter(client).forward_turn(_request(), threshold=0.5)

    assert turno.response.status == AgentStatus.NOT_FOUND.value
    assert turno.motivo == "sentinela"
    assert turno.citacoes == 0


def test_motivos_sao_distinguiveis_entre_si():
    """A razão de existir do campo: os três not_found não podem colidir."""
    casos = {
        "sem_relevancia": FakeRagClient(
            _kb_response(("s3://kb/a.pdf", 0.1)),
            _rag_response("x", "s3://kb/a.pdf"),
        ),
        "sem_citacao": FakeRagClient(
            _kb_response(("s3://kb/a.pdf", 0.9)), _rag_response("x")
        ),
        "sentinela": FakeRagClient(
            _kb_response(("s3://kb/a.pdf", 0.9)),
            _rag_response(agent_module.NO_ANSWER_SENTINEL, "s3://kb/a.pdf"),
        ),
    }
    obtidos = {
        esperado: _adapter(c).forward_turn(_request(), threshold=0.5).motivo
        for esperado, c in casos.items()
    }

    assert obtidos == {k: k for k in casos}
    assert all(m in agent_module.MOTIVOS for m in obtidos.values())
