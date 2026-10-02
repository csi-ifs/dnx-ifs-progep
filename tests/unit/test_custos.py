"""Controle de custos: preços, atribuição, linhas de log e resumo (lógica pura)."""

import json
from datetime import date

import pytest

from src.domain import custos as dom

AGENTE = "arn:aws:sts::1:assumed-role/progep-ifs-agentcore-exec/BedrockAgentCore-abc"
BUSCA = "arn:aws:sts::1:assumed-role/progep-ifs-kb-920de21/BKB-Retrieve-1AACEWPJNC-x"
INDEXACAO = "arn:aws:sts::1:assumed-role/progep-ifs-kb-920de21/EmbeddingTask-7WRXEDZ9HA"
DEN = "arn:aws:sts::1:assumed-role/den-ifs-agent-core-execution-role/BedrockAgentCore-1"
EQUIPE = "arn:aws:sts::1:assumed-role/DNXAccess/julia@x.com"
OUTRA_PESSOA = "arn:aws:sts::1:assumed-role/DNXAccess/mateus@x.com"


def test_custo_do_sonnet_confere_com_a_fatura():
    # 278.796 tokens de entrada e 31.372 de saída custaram US$ 1,31 em setembro.
    usd = dom.custo_tokens("global.anthropic.claude-sonnet-4-5-20250929-v1:0", 278_796, 31_372)
    assert usd == pytest.approx(1.3069, abs=1e-4)


def test_custo_do_cache_e_do_embedding():
    assert dom.custo_tokens("x.claude-sonnet-4-5", 0, 0, 1_000_000, 0) == pytest.approx(0.30)
    assert dom.custo_tokens("x.claude-sonnet-4-5", 0, 0, 0, 1_000_000) == pytest.approx(3.75)
    assert dom.custo_tokens("amazon.titan-embed-text-v2:0", 1_000_000, 0) == pytest.approx(0.02)


def test_modelo_sem_preco_devolve_none():
    assert dom.custo_tokens("modelo-desconhecido", 10, 10) is None


@pytest.mark.parametrize(
    "identidade, esperado",
    [
        (AGENTE, "assistente#modelo"),
        (BUSCA, "assistente#busca"),
        (INDEXACAO, "documentos#indexacao"),
        ("arn:aws:sts::1:assumed-role/progep-ifs-kb-920de21/BKB-CP-1AACEWPJNC-E", "documentos#indexacao"),
        (EQUIPE, "documentos#equipe_tecnica"),
        (DEN, None),
        (OUTRA_PESSOA, None),
        ("arn:aws:iam::1:user/alguem", None),
        ("", None),
    ],
)
def test_atribuicao_so_conta_o_que_e_da_progep(identidade, esperado):
    assert dom.item_da_invocacao(identidade, ["Julia@x.com"]) == esperado


def test_interpretar_invocacao():
    linha = json.dumps({
        "schemaType": "ModelInvocationLog", "requestId": "r1", "modelId": "m",
        "identity": {"arn": AGENTE},
        "input": {"inputTokenCount": 10, "cacheReadInputTokenCount": 2},
        "output": {"outputTokenCount": 3},
    })
    inv = dom.interpretar_invocacao(linha)
    assert (inv.request_id, inv.entrada, inv.saida, inv.cache_leitura) == ("r1", 10, 3, 2)
    assert dom.interpretar_invocacao('{"outro": 1}') is None
    assert dom.interpretar_invocacao("texto") is None


@pytest.mark.parametrize(
    "linha, esperado",
    [
        ("[INFO]\t2026\treq\t[entrada] msg=wamid.A tipos=text", ("recebida", "wamid.A")),
        ("[WARNING]\t...\t[entrega] sent msg=wamid.B destino=…1234", ("enviada", "wamid.B")),
        ("[WARNING]\t...\t[entrega] delivered msg=wamid.B destino=…1234", ("entregue", "wamid.B")),
        ("[WARNING]\t...\t[entrega] read msg=wamid.B destino=…1234", None),
        ("[WARNING]\t...\t[entrega] FALHOU msg=wamid.B destino=…1234 erros=[]", None),
        # Reentrega da mesma mensagem: não é cobrança nova.
        ("[INFO]\t...\t[entrada] repetida msg=wamid.A ignorada", None),
    ],
)
def test_linhas_do_whatsapp(linha, esperado):
    assert dom.interpretar_linha_whatsapp(linha) == esperado


def test_linha_do_chat():
    assert dom.interpretar_linha_chat("[INFO] x [chat] mensagem papel=AGENT id=abc-1") == "abc-1"
    assert dom.interpretar_linha_chat("[INFO] x Chat encerrado") is None


def test_tarifa_da_meta_so_acima_da_cota():
    assert dom.custo_meta(1000) == 0.0
    assert dom.custo_meta(1001) == pytest.approx(0.0068)
    assert dom.custo_meta(5000, isento=True) == 0.0


def test_dia_local_vira_a_meia_noite_de_aracaju():
    # 02:59 UTC de 02/10 ainda é 01/10 em UTC−3.
    assert dom.dia_local(1_759_373_940_000) == "2025-10-01"
    assert dom.mes_utc(1_759_373_940_000) == "2025-10"


def test_rateio_diario():
    assert dom.rateio_diario(3.0, date(2026, 9, 10)) == pytest.approx(0.1)
    assert dom.rateio_diario(3.1, date(2026, 10, 10)) == pytest.approx(0.1)


def _linha(dia, item, usd, qtd=0):
    return {"dia": dia, "item": item, "usd": usd, "quantidade": qtd, "atualizado_em": "t"}


def test_resumo_soma_por_dia_categoria_e_item():
    linhas = [
        _linha("2026-10-01", "assistente#modelo", 0.30, 10),
        _linha("2026-10-01", "infraestrutura#auditoria", 0.10),
        _linha("2026-10-02", "assistente#modelo", 0.20, 5),
        _linha("2026-10-02", "documentos#indexacao", 0.01, 100),
        _linha("2026-10-02", dom.PERGUNTAS, 0, 8),
        _linha("2026-10-02", "item#que_nao_existe", 99),
        _linha("2026-09-30", "assistente#modelo", 50),  # fora do período
    ]
    r = dom.resumir(linhas, inicio=date(2026, 10, 1), fim=date(2026, 10, 2),
                    hoje=date(2026, 10, 2))
    assert r["total_usd"] == pytest.approx(0.61)
    assert r["por_categoria"] == pytest.approx(
        {"assistente": 0.50, "documentos": 0.01, "infraestrutura": 0.10})
    assert [d["dia"] for d in r["dias"]] == ["2026-10-01", "2026-10-02"]
    assert r["perguntas"] == 8
    assert r["custo_por_pergunta_usd"] == pytest.approx(0.50 / 8)
    modelo = next(i for i in r["itens"] if i["item"] == "assistente#modelo")
    assert (modelo["usd"], modelo["quantidade"]) == (pytest.approx(0.5), 15)
    # Assistente primeiro, infraestrutura por último.
    assert [i["categoria"] for i in r["itens"]] == ["assistente", "documentos", "infraestrutura"]


def test_projecao_do_mes_usa_a_media_informada():
    linhas = [_linha("2026-10-01", "assistente#modelo", 1.0),
              _linha("2026-10-02", "assistente#modelo", 0.5)]
    r = dom.resumir(linhas, inicio=date(2026, 10, 1), fim=date(2026, 10, 2),
                    hoje=date(2026, 10, 2), media_diaria=0.3)
    # 1,50 até agora + 0,30 × 29 dias restantes.
    assert r["projecao_mes_usd"] == pytest.approx(1.5 + 0.3 * 29)
    sem_media = dom.resumir(linhas, inicio=date(2026, 10, 1), fim=date(2026, 10, 2),
                            hoje=date(2026, 10, 2))
    assert sem_media["projecao_mes_usd"] is None
    outro = dom.resumir(linhas, inicio=date(2026, 9, 26), fim=date(2026, 10, 2),
                        hoje=date(2026, 10, 2), media_diaria=0.3)
    assert outro["projecao_mes_usd"] is None


def test_media_diaria_ignora_dias_sem_registro():
    linhas = [_linha("2026-09-29", "assistente#modelo", 0.2),
              _linha("2026-09-29", "infraestrutura#auditoria", 0.1),
              _linha("2026-09-30", "assistente#modelo", 0.1),
              _linha("2026-09-30", dom.PERGUNTAS, 0, 9)]
    assert dom.media_diaria(linhas) == pytest.approx(0.2)
    assert dom.media_diaria([]) is None


def test_conferencia_do_modelo_deixa_o_ultimo_dia_da_fatura_de_fora():
    linhas = [_linha("2026-09-01", "assistente#modelo", 0.40),
              _linha("2026-09-01", "documentos#equipe_tecnica", 0.06),
              _linha("2026-09-02", "assistente#modelo", 0.10),
              _linha("2026-09-03", "assistente#modelo", 0.50)]
    faturas = [{"dia": "2026-09-01", "item": "modelo", "usd": 0.46},
               {"dia": "2026-09-02", "item": "modelo", "usd": 0.10},
               {"dia": "2026-09-03", "item": "modelo", "usd": 0.20}]
    c = dom.conferir_modelo(linhas, faturas)
    assert c == {"ate": "2026-09-02", "medido_usd": 0.56, "faturado_usd": 0.56}
    assert dom.conferir_modelo(linhas, faturas[:1]) is None
