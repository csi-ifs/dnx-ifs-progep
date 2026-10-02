"""Coleta de custos: logs em tempo real, coletor horário, tabela e rota do painel."""

import base64
import gzip
import json
from datetime import date, datetime, timezone

import pytest

from src.adapters import acervo_handler
from src.adapters.custos_handler import Coletor, decodificar_assinatura, processar_eventos
from src.adapters.custos_store import CustosStore, _meses
from src.domain import custos as dom

AGENTE = "arn:aws:sts::1:assumed-role/progep-ifs-agentcore-exec/BedrockAgentCore-abc"
DEN = "arn:aws:sts::1:assumed-role/den-ifs-agent-core-execution-role/BedrockAgentCore-1"
# 12:00 UTC de 02/10/2026 = 09:00 em Aracaju.
TS = int(datetime(2026, 10, 2, 12, tzinfo=timezone.utc).timestamp() * 1000)


class LojaFalsa:
    def __init__(self, entregues_iniciais=0):
        self.somas = {}
        self.definidos = {}
        self.marcas = set()
        self.contador = entregues_iniciais

    def marcar(self, marca, agora=None):
        if marca in self.marcas:
            return False
        self.marcas.add(marca)
        return True

    def somar(self, dia, item, usd, quantidade=0):
        u, q = self.somas.get((dia, item), (0.0, 0.0))
        self.somas[(dia, item)] = (u + usd, q + quantidade)

    def definir(self, dia, item, usd, quantidade=0, origem="medido"):
        self.definidos[(dia, item)] = (usd, quantidade)

    def definir_fatura(self, dia, item, usd):
        self.definidos[(dia, f"fatura#{item}")] = (usd, 0)

    def contar_mes(self, mes, nome, n=1):
        self.contador += n
        return self.contador


def _invocacao(request_id, identidade=AGENTE, entrada=1000, saida=100):
    return {"timestamp": TS, "message": json.dumps({
        "schemaType": "ModelInvocationLog", "requestId": request_id,
        "modelId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "identity": {"arn": identidade},
        "input": {"inputTokenCount": entrada}, "output": {"outputTokenCount": saida},
    })}


def test_invocacoes_somam_e_ignoram_outros_projetos_e_repeticoes():
    loja = LojaFalsa()
    contagem = processar_eventos(
        [_invocacao("a"), _invocacao("a"), _invocacao("b"), _invocacao("c", identidade=DEN)],
        loja,
    )
    assert contagem["modelo"] == 2 and contagem["repetidos"] == 1 and contagem["ignorados"] == 1
    usd, qtd = loja.somas[("2026-10-02", "assistente#modelo")]
    assert qtd == 2
    assert usd == pytest.approx(2 * (1000 * 3 + 100 * 15) / 1e6)


def test_mensagens_do_whatsapp_e_do_chat():
    loja = LojaFalsa(entregues_iniciais=999)
    eventos = [
        {"timestamp": TS, "message": "[INFO]\tt\tr\t[entrada] msg=wamid.1 tipos=text"},
        {"timestamp": TS, "message": "[WARNING]\tt\tr\t[entrega] sent msg=wamid.2 destino=…1"},
        {"timestamp": TS, "message": "[WARNING]\tt\tr\t[entrega] sent msg=wamid.2 destino=…1"},
        # 1000ª entregue: dentro da cota. 1001ª: cobrada.
        {"timestamp": TS, "message": "[WARNING]\tt\tr\t[entrega] delivered msg=wamid.2 destino=…1"},
        {"timestamp": TS, "message": "[WARNING]\tt\tr\t[entrega] delivered msg=wamid.3 destino=…1"},
        {"timestamp": TS, "message": "[INFO]\tt\tr\t[chat] mensagem papel=AGENT id=c1"},
        {"timestamp": TS, "message": "[INFO]\tt\tr\t[chat] mensagem papel=CUSTOMER id=c1"},
        {"timestamp": TS, "message": "START RequestId: x"},
    ]
    processar_eventos(eventos, loja)
    dia = "2026-10-02"
    assert loja.somas[(dia, "assistente#whatsapp_recebidas")] == (pytest.approx(0.001), 1)
    assert loja.somas[(dia, "assistente#whatsapp_enviadas")] == (pytest.approx(0.005), 1)
    assert loja.somas[(dia, "assistente#whatsapp_meta")] == (pytest.approx(0.0068), 1)
    # O mesmo id do chat volta pelo streaming: conta uma vez só.
    assert loja.somas[(dia, "assistente#atendimento")] == (pytest.approx(0.01), 1)


def test_meta_isenta_nao_cobra():
    loja = LojaFalsa(entregues_iniciais=5000)
    processar_eventos(
        [{"timestamp": TS, "message": "[entrega] delivered msg=wamid.9 destino=…1"}],
        loja, meta_isento=True,
    )
    assert (("2026-10-02", "assistente#whatsapp_meta")) not in loja.somas


def test_decodificar_assinatura():
    corpo = {"messageType": "DATA_MESSAGE", "logGroup": "/g", "logEvents": [{"id": "1"}]}
    dados = base64.b64encode(gzip.compress(json.dumps(corpo).encode())).decode()
    assert decodificar_assinatura({"awslogs": {"data": dados}}) == ("/g", [{"id": "1"}])
    controle = base64.b64encode(gzip.compress(b'{"messageType": "CONTROL_MESSAGE"}')).decode()
    assert decodificar_assinatura({"awslogs": {"data": controle}}) == ("", [])


class CloudWatchFalso:
    """Devolve, para cada consulta, um ponto às 12:00 UTC de cada dia pedido."""

    def __init__(self, valores, series=None):
        self.valores = valores
        self.series = series or {}

    def get_paginator(self, nome):
        assert nome == "list_metrics"
        cw = self

        class P:
            def paginate(self, Namespace, MetricName):
                yield {"Metrics": [{"Dimensions": d}
                                   for d in cw.series.get((Namespace, MetricName), [])]}
        return P()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime, **_):
        resultados = []
        for q in MetricDataQueries:
            ts = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
            resultados.append({"Id": q["Id"], "Timestamps": [ts],
                               "Values": [self.valores.get(q["Id"], 0.0)]})
        return {"MetricDataResults": resultados}


class LogsFalso:
    def get_paginator(self, nome):
        class P:
            def paginate(self, logGroupNamePrefix):
                yield {"logGroups": [{"storedBytes": 0}]}
        return P()


def test_coletor_calcula_agentcore_e_lambda():
    funcao = [{"Name": "FunctionName", "Value": "progep-ifs-whatsapp-social"}]
    outra = [{"Name": "FunctionName", "Value": "den-ifs-handler"}]
    cw = CloudWatchFalso(
        {"cpu": 0.1, "mem": 10.0, "inv": 4, "li0": 100, "ld0": 50_000},
        series={("AWS/Lambda", "Invocations"): [funcao, outra]},
    )

    class LambdaFalso:
        def get_function_configuration(self, FunctionName):
            return {"MemorySize": 1024}

    loja = LojaFalsa()
    c = Coletor(loja, cloudwatch=cw, lambda_=LambdaFalso(), logs=LogsFalso(),
                runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/progep_ifs_agent-X")
    c._agentcore([date(2026, 10, 2)])
    c._servicos([date(2026, 10, 2)])
    dia = "2026-10-02"
    usd, perguntas = loja.definidos[(dia, "assistente#execucao")]
    assert usd == pytest.approx(0.1 * 0.0895 + 10 * 0.00945)
    assert perguntas == 4 and loja.definidos[(dia, dom.PERGUNTAS)] == (0.0, 4)
    usd, execucoes = loja.definidos[(dia, "infraestrutura#automacoes")]
    # 50 s × 1 GB + 100 requisições; a função do outro projeto fica de fora.
    assert execucoes == 100
    assert usd == pytest.approx(50 * 0.0000166667 + 100 * 0.2 / 1e6)
    # Métrica personalizada: 2 séries por pergunta + 2 por hora com uso (1 hora).
    usd, _ = loja.definidos[(dia, "infraestrutura#registros")]
    assert usd == pytest.approx((2 * 4 + 2 * 1) * 0.30 / 730)
    assert c.metricas_consultadas == 3 + 2


def test_fatura_separa_cloudtrail_e_modelo():
    class CeFalso:
        def get_cost_and_usage(self, **_):
            return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-30"}, "Groups": [
                {"Keys": ["AWS CloudTrail"], "Metrics": {"UnblendedCost": {"Amount": "0.27"}}},
                {"Keys": ["Claude Sonnet 4.5 (Amazon Bedrock Edition)"],
                 "Metrics": {"UnblendedCost": {"Amount": "0.15"}}},
                {"Keys": ["Amazon Bedrock AgentCore"],  # compartilhado: fica de fora
                 "Metrics": {"UnblendedCost": {"Amount": "9"}}},
            ]}]}

    loja = LojaFalsa()
    c = Coletor(loja, ce=CeFalso())
    c.fatura(date(2026, 10, 2))
    assert loja.definidos == {
        ("2026-09-30", "infraestrutura#auditoria"): (0.27, 0),
        ("2026-09-30", "fatura#modelo"): (0.15, 0),
    }
    assert c.custo_das_consultas() == pytest.approx(0.01)


# --- Tabela -------------------------------------------------------------------------

class DynamoFalso:
    def __init__(self):
        self.chamadas = []
        self.itens = {}

    def update_item(self, **kw):
        self.chamadas.append(("update", kw))
        return {"Attributes": {"valor": {"N": "7"}}}

    def put_item(self, **kw):
        self.chamadas.append(("put", kw))
        chave = (kw["Item"]["pk"]["S"], kw["Item"]["sk"]["S"])
        if "ConditionExpression" in kw and chave in self.itens:
            erro = Exception("condicional")
            erro.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
            raise erro
        self.itens[chave] = kw["Item"]

    def query(self, **kw):
        self.chamadas.append(("query", kw))
        return {"Items": [{"sk": {"S": "2026-10-02#assistente#modelo"}, "usd": {"N": "0.5"},
                           "quantidade": {"N": "3"}, "atualizado_em": {"S": "t"}}]}


def test_tabela_marca_soma_e_consulta():
    d = DynamoFalso()
    loja = CustosStore("t", client=d)
    assert loja.marcar("inv#1") is True
    assert loja.marcar("inv#1") is False
    loja.somar("2026-10-02", "assistente#modelo", 1e-7, 1)
    _, kw = d.chamadas[-1]
    assert kw["Key"] == {"pk": {"S": "custo#2026-10"}, "sk": {"S": "2026-10-02#assistente#modelo"}}
    assert kw["ExpressionAttributeValues"][":u"]["N"] == "0.0000001"
    assert loja.contar_mes("2026-10", "meta_entregues") == 7
    linhas = loja.custos(date(2026, 9, 28), date(2026, 10, 2))
    assert linhas[0] == {"dia": "2026-10-02", "item": "assistente#modelo", "usd": 0.5,
                         "quantidade": 3.0, "atualizado_em": "t"}
    consultas = [kw for tipo, kw in d.chamadas if tipo == "query"]
    assert [q["ExpressionAttributeValues"][":pk"]["S"] for q in consultas] == [
        "custo#2026-09", "custo#2026-10"]


def test_meses_atravessa_o_ano():
    assert _meses(date(2026, 12, 20), date(2027, 1, 5)) == ["2026-12", "2027-01"]


# --- Rota do painel ---------------------------------------------------------------------

def _evento(periodo, grupos="administradores"):
    return {
        "httpMethod": "GET", "path": "/acervo/custos",
        "queryStringParameters": {"periodo": periodo},
        "requestContext": {"authorizer": {"claims": {"sub": "u", "cognito:groups": grupos}}},
    }


class LojaDoPainel:
    def custos(self, inicio, fim):
        return [{"dia": inicio.isoformat(), "item": "assistente#modelo", "usd": 0.25,
                 "quantidade": 2, "atualizado_em": "t"}]

    def faturas(self, inicio, fim):
        return []

    def ler_contador(self, mes, nome):
        return 12


def test_rota_de_custos_exige_administrador():
    r = acervo_handler.handler(_evento("mes", grupos="outro"), custos_store=LojaDoPainel())
    assert r["statusCode"] == 403


def test_rota_de_custos_devolve_o_resumo():
    r = acervo_handler.handler(_evento("7d"), custos_store=LojaDoPainel())
    assert r["statusCode"] == 200
    corpo = json.loads(r["body"])
    assert corpo["periodo"] == "7d"
    assert corpo["total_usd"] == pytest.approx(0.25)
    assert corpo["meta"]["entregues_no_mes"] == 12
    assert len(corpo["dias"]) == 7


def test_periodo_invalido_e_400():
    r = acervo_handler.handler(_evento("ano"), custos_store=LojaDoPainel())
    assert r["statusCode"] == 400


@pytest.mark.parametrize(
    "periodo, esperado",
    [
        ("hoje", (date(2026, 10, 2), date(2026, 10, 2))),
        ("7d", (date(2026, 9, 26), date(2026, 10, 2))),
        ("30d", (date(2026, 9, 3), date(2026, 10, 2))),
        ("mes", (date(2026, 10, 1), date(2026, 10, 2))),
        ("mes_anterior", (date(2026, 9, 1), date(2026, 9, 30))),
    ],
)
def test_intervalos(periodo, esperado):
    assert acervo_handler.intervalo_do_periodo(periodo, date(2026, 10, 2)) == esperado


def test_series_de_metrica_depois_do_monitor_sem_sessao():
    from src.adapters import custos_handler as h

    c = Coletor(LojaFalsa())
    c._perguntas = {"2026-10-02": 10, "2026-10-05": 10}
    c.horas_ativas = {"inv": {"2026-10-02": 3, "2026-10-05": 3}}
    assert c._series_hora("2026-10-02") == 2 * 10 + 2 * 3
    assert h.MONITOR_SEM_SESSAO_DESDE == "2026-10-03"
    assert c._series_hora("2026-10-05") == 4 * 3
