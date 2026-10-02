"""Coleta de custos do assistente: em tempo real (logs) e de hora em hora (métricas).

Duas Lambdas, um módulo:

* :func:`logs_handler` — assinatura do CloudWatch Logs em três log groups. Cada
  chamada ao modelo (log de invocações do Bedrock), mensagem do WhatsApp (linhas
  ``[entrada]``/``[entrega]``) e mensagem do chat do Connect (``[chat]``) vira
  custo **segundos depois** de acontecer. Soma (``ADD``), com marca por evento
  contra a reentrega.
* :func:`coleta_handler` — de hora em hora. O que só existe como métrica
  (CPU/memória do AgentCore, execuções de Lambda, leituras do DynamoDB, logs
  gravados) e os custos fixos rateados por dia. **Substitui** o valor do dia,
  então pode rodar quantas vezes for preciso. A cada 12 h também lê a fatura
  (Cost Explorer), que chega com até 24 h de atraso e serve de conferência.

Toda atribuição é pelo recurso da PROGEP que consumiu — ver
:mod:`src.domain.custos` sobre a conta ser compartilhada com outros projetos.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Mapping

from src.adapters.custos_store import CustosStore
from src.domain import custos as dom

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PREFIXO = "progep-ifs"
#: Log groups da PROGEP (Lambdas, runtime do agente, Monitor e invocações).
PREFIXOS_DE_LOG = ("/aws/lambda/progep-ifs", "/aws/bedrock-agentcore/runtimes/progep_ifs_agent",
                   "/progep-ifs")

#: Primeiro dia inteiro com o Monitor de Consumo publicando sem ``SessionId``
#: (src/adapters/monitor_adapter.py:METRIC_DIMENSIONS).
MONITOR_SEM_SESSAO_DESDE = "2026-10-03"

#: Intervalo entre leituras da fatura. O Cost Explorer cobra US$ 0,01 por
#: consulta e só atualiza algumas vezes ao dia: ler a cada hora custaria
#: US$ 7/mês para ver o mesmo número.
HORAS_ENTRE_FATURAS = 12


def _loja() -> CustosStore:
    return CustosStore(os.environ["CUSTOS_TABLE"], region_name=os.environ.get("AWS_REGION"))


def _usuarios_equipe() -> list[str]:
    return [u for u in os.environ.get("CUSTOS_EQUIPE_TECNICA", "").split(",") if u.strip()]


def _meta_isento() -> bool:
    return os.environ.get("CUSTOS_META_ISENTO", "").strip().lower() in ("1", "true", "sim")


# --- Tempo real ---------------------------------------------------------------------

def decodificar_assinatura(evento: Mapping[str, Any]) -> tuple[str, list[dict]]:
    """``(log_group, logEvents)`` do payload gzip+base64 da assinatura."""
    bruto = base64.b64decode(evento["awslogs"]["data"])
    corpo = json.loads(gzip.decompress(bruto))
    if corpo.get("messageType") != "DATA_MESSAGE":
        return "", []
    return str(corpo.get("logGroup", "")), list(corpo.get("logEvents") or [])


def processar_eventos(
    eventos: Iterable[Mapping[str, Any]],
    loja: CustosStore,
    *,
    usuarios_equipe: Iterable[str] = (),
    meta_isento: bool = False,
) -> dict[str, int]:
    """Converte linhas de log em custo. Usado ao vivo e no reprocessamento.

    Cada evento traz ``timestamp`` (ms) e ``message``. O tipo é reconhecido pelo
    conteúdo, não pelo log group, então a mesma função serve às três origens.
    """
    equipe = list(usuarios_equipe)
    contagem = {"modelo": 0, "whatsapp": 0, "chat": 0, "ignorados": 0, "repetidos": 0}
    for ev in eventos:
        mensagem = str(ev.get("message", ""))
        ts = int(ev.get("timestamp") or 0)
        dia = dom.dia_local(ts)

        inv = dom.interpretar_invocacao(mensagem) if mensagem.startswith("{") else None
        if inv is not None:
            item = dom.item_da_invocacao(inv.identidade, equipe)
            if item is None:
                contagem["ignorados"] += 1  # outro projeto da conta
                continue
            usd = dom.custo_tokens(inv.modelo, inv.entrada, inv.saida,
                                   inv.cache_leitura, inv.cache_escrita)
            if usd is None:
                logger.warning("Modelo sem preço cadastrado: %s", inv.modelo)
                usd = 0.0
            if inv.request_id and not loja.marcar(f"inv#{inv.request_id}"):
                contagem["repetidos"] += 1
                continue
            loja.somar(dia, item, usd, 1)
            contagem["modelo"] += 1
            continue

        chat = dom.interpretar_linha_chat(mensagem)
        if chat is not None:
            if not loja.marcar(f"chat#{chat}"):
                contagem["repetidos"] += 1
                continue
            loja.somar(dia, "assistente#atendimento", dom.PRECOS["connect_mensagem"], 1)
            contagem["chat"] += 1
            continue

        wa = dom.interpretar_linha_whatsapp(mensagem)
        if wa is not None:
            tipo, msg_id = wa
            if not loja.marcar(f"wa#{tipo}#{msg_id}"):
                contagem["repetidos"] += 1
                continue
            if tipo == "recebida":
                loja.somar(dia, "assistente#whatsapp_recebidas", dom.PRECOS["whatsapp_recebida"], 1)
            elif tipo == "enviada":
                loja.somar(dia, "assistente#whatsapp_enviadas", dom.PRECOS["whatsapp_enviada"], 1)
            else:
                entregues = loja.contar_mes(dom.mes_utc(ts), "meta_entregues")
                usd = dom.custo_meta(entregues, isento=meta_isento)
                if usd:
                    loja.somar(dia, "assistente#whatsapp_meta", usd, 1)
            contagem["whatsapp"] += 1
            continue

        contagem["ignorados"] += 1
    return contagem


def logs_handler(evento: Mapping[str, Any], contexto: Any = None) -> dict[str, Any]:
    """Assinatura do CloudWatch Logs. Falha sobe: a Lambda é reinvocada, e as
    marcas garantem que o que já foi contado não conte de novo."""
    grupo, eventos = decodificar_assinatura(evento)
    contagem = processar_eventos(
        eventos, _loja(), usuarios_equipe=_usuarios_equipe(), meta_isento=_meta_isento()
    )
    logger.info("[custos] grupo=%s %s", grupo, contagem)
    return contagem


# --- De hora em hora ----------------------------------------------------------------------

def _inicio_do_dia(dia: date) -> datetime:
    return datetime.combine(dia, dtime(0), dom.FUSO).astimezone(timezone.utc)


class Coletor:
    """Recalcula, para dias inteiros, os itens que vêm de métricas e os fixos."""

    def __init__(self, loja: CustosStore, *, cloudwatch: Any = None, lambda_: Any = None,
                 logs: Any = None, ecr: Any = None, s3vectors: Any = None, ce: Any = None,
                 runtime_arn: str = "", ecr_repo: str = "", vector_bucket: str = "",
                 vector_index: str = "", segredos: int = 0) -> None:
        self.loja = loja
        self.cw, self.lambda_, self.logs = cloudwatch, lambda_, logs
        self.ecr, self.s3vectors, self.ce = ecr, s3vectors, ce
        self.runtime_arn = runtime_arn
        self.ecr_repo, self.vector_bucket, self.vector_index = ecr_repo, vector_bucket, vector_index
        self.segredos = segredos
        self._memoria_lambda: dict[str, int] = {}
        self.horas_ativas: dict[str, dict[str, int]] = {}
        self.metricas_consultadas = 0
        self.consultas_fatura = 0
        #: Perguntas por dia, preenchido por :meth:`_agentcore`.
        self._perguntas: dict[str, float] = {}

    # -- métricas ---------------------------------------------------------------

    def _series(self, namespace: str, metrica: str, filtro) -> list[list[dict]]:
        series = []
        for p in self.cw.get_paginator("list_metrics").paginate(Namespace=namespace,
                                                                MetricName=metrica):
            for m in p["Metrics"]:
                if filtro(m["Dimensions"]):
                    series.append(m["Dimensions"])
        return series

    def _somas_por_dia(self, consultas: list[dict], dias: list[date],
                       periodo: int = 3600, estatistica: str = "Sum") -> dict[str, dict[str, float]]:
        """``{id_da_consulta: {dia_local: soma}}`` via GetMetricData.

        Também guarda em ``self.horas_ativas[id][dia]`` quantos pontos do dia
        vieram positivos — com período de 1 h, as horas em que houve uso.
        """
        resultado: dict[str, dict[str, float]] = {c["Id"]: {} for c in consultas}
        self.metricas_consultadas += len(consultas)
        if not consultas:
            return resultado
        inicio = _inicio_do_dia(min(dias))
        fim = _inicio_do_dia(max(dias) + timedelta(days=1))
        for lote in range(0, len(consultas), 400):
            parametros: dict[str, Any] = {
                "MetricDataQueries": [
                    {"Id": c["Id"], "ReturnData": True, "MetricStat": {
                        "Metric": {"Namespace": c["Namespace"], "MetricName": c["MetricName"],
                                   "Dimensions": c["Dimensions"]},
                        "Period": periodo, "Stat": estatistica}}
                    for c in consultas[lote:lote + 400]
                ],
                "StartTime": inicio, "EndTime": fim,
            }
            while True:
                r = self.cw.get_metric_data(**parametros)
                for serie in r.get("MetricDataResults", []):
                    alvo = resultado[serie["Id"]]
                    horas = self.horas_ativas.setdefault(serie["Id"], {})
                    for ts, valor in zip(serie.get("Timestamps", []), serie.get("Values", [])):
                        d = ts.astimezone(dom.FUSO).date().isoformat()
                        alvo[d] = alvo.get(d, 0.0) + float(valor)
                        if valor > 0:
                            horas[d] = horas.get(d, 0) + 1
                if not r.get("NextToken"):
                    break
                parametros["NextToken"] = r["NextToken"]
        return resultado

    def _agentcore(self, dias: list[date]) -> None:
        if not self.runtime_arn:
            return
        dims = [{"Name": "Resource", "Value": self.runtime_arn},
                {"Name": "Service", "Value": "AgentCore.Runtime"}]
        nome = self.runtime_arn.rsplit("/", 1)[-1].rsplit("-", 1)[0] + "::DEFAULT"
        consultas = [
            {"Id": "cpu", "Namespace": "AWS/Bedrock-AgentCore",
             "MetricName": "CPUUsed-vCPUHours", "Dimensions": dims},
            {"Id": "mem", "Namespace": "AWS/Bedrock-AgentCore",
             "MetricName": "MemoryUsed-GBHours", "Dimensions": dims},
            {"Id": "inv", "Namespace": "AWS/Bedrock-AgentCore", "MetricName": "Invocations",
             "Dimensions": [{"Name": "Resource", "Value": self.runtime_arn},
                            {"Name": "Operation", "Value": "InvokeAgentRuntime"},
                            {"Name": "Name", "Value": nome}]},
        ]
        s = self._somas_por_dia(consultas, dias)
        for d in dias:
            k = d.isoformat()
            usd = (s["cpu"].get(k, 0.0) * dom.PRECOS["agentcore_vcpu_hora"]
                   + s["mem"].get(k, 0.0) * dom.PRECOS["agentcore_gb_hora"])
            invocacoes = s["inv"].get(k, 0.0)
            self._perguntas[k] = invocacoes
            self.loja.definir(k, "assistente#execucao", usd, invocacoes)
            self.loja.definir(k, dom.PERGUNTAS, 0.0, invocacoes)

    def _memoria(self, funcao: str) -> int:
        if funcao not in self._memoria_lambda:
            try:
                cfg = self.lambda_.get_function_configuration(FunctionName=funcao)
                self._memoria_lambda[funcao] = int(cfg.get("MemorySize") or 128)
            except Exception:  # noqa: BLE001 - função removida: menor memória
                self._memoria_lambda[funcao] = 128
        return self._memoria_lambda[funcao]

    def _servicos(self, dias: list[date]) -> None:
        """Lambda, DynamoDB, API Gateway e ingestão de logs — tudo por métrica."""
        def nosso(chave: str):
            return lambda dims: any(d["Name"] == chave and d["Value"].startswith(PREFIXO)
                                    for d in dims) and len(dims) == 1

        consultas: list[dict] = []
        funcoes = [d[0]["Value"] for d in self._series("AWS/Lambda", "Invocations",
                                                        nosso("FunctionName"))]
        for i, f in enumerate(funcoes):
            dims = [{"Name": "FunctionName", "Value": f}]
            consultas += [
                {"Id": f"li{i}", "Namespace": "AWS/Lambda", "MetricName": "Invocations", "Dimensions": dims},
                {"Id": f"ld{i}", "Namespace": "AWS/Lambda", "MetricName": "Duration", "Dimensions": dims},
            ]
        tabelas = [d[0]["Value"] for d in self._series(
            "AWS/DynamoDB", "ConsumedWriteCapacityUnits", nosso("TableName"))]
        for i, t in enumerate(tabelas):
            dims = [{"Name": "TableName", "Value": t}]
            consultas += [
                {"Id": f"dw{i}", "Namespace": "AWS/DynamoDB",
                 "MetricName": "ConsumedWriteCapacityUnits", "Dimensions": dims},
                {"Id": f"dr{i}", "Namespace": "AWS/DynamoDB",
                 "MetricName": "ConsumedReadCapacityUnits", "Dimensions": dims},
            ]
        apis = [d[0]["Value"] for d in self._series("AWS/ApiGateway", "Count", nosso("ApiName"))]
        for i, a in enumerate(apis):
            consultas.append({"Id": f"ap{i}", "Namespace": "AWS/ApiGateway", "MetricName": "Count",
                              "Dimensions": [{"Name": "ApiName", "Value": a}]})

        def grupo_nosso(dims):
            return len(dims) == 1 and dims[0]["Name"] == "LogGroupName" and _grupo_da_progep(
                dims[0]["Value"])
        grupos = [d[0]["Value"] for d in self._series("AWS/Logs", "IncomingBytes", grupo_nosso)]
        for i, g in enumerate(grupos):
            consultas.append({"Id": f"lg{i}", "Namespace": "AWS/Logs", "MetricName": "IncomingBytes",
                              "Dimensions": [{"Name": "LogGroupName", "Value": g}]})

        s = self._somas_por_dia(consultas, dias)
        armazenado_gb = self._logs_armazenados() / dom.GB
        for d in dias:
            k = d.isoformat()
            execucoes = gb_s = 0.0
            for i, f in enumerate(funcoes):
                execucoes += s[f"li{i}"].get(k, 0.0)
                gb_s += s[f"ld{i}"].get(k, 0.0) / 1000 * self._memoria(f) / 1024
            self.loja.definir(
                k, "infraestrutura#automacoes",
                gb_s * dom.PRECOS["lambda_gb_segundo"] + execucoes * dom.PRECOS["lambda_requisicao"],
                execucoes,
            )
            escritas = sum(s[f"dw{i}"].get(k, 0.0) for i in range(len(tabelas)))
            leituras = sum(s[f"dr{i}"].get(k, 0.0) for i in range(len(tabelas)))
            self.loja.definir(
                k, "infraestrutura#banco",
                escritas * dom.PRECOS["dynamodb_escrita"] + leituras * dom.PRECOS["dynamodb_leitura"],
                escritas + leituras,
            )
            requisicoes = sum(s[f"ap{i}"].get(k, 0.0) for i in range(len(apis)))
            self.loja.definir(k, "infraestrutura#api", requisicoes * dom.PRECOS["api_requisicao"],
                              requisicoes)
            ingerido_gb = sum(s[f"lg{i}"].get(k, 0.0) for i in range(len(grupos))) / dom.GB
            self.loja.definir(
                k, "infraestrutura#registros",
                ingerido_gb * dom.PRECOS["logs_ingestao_gb"]
                + dom.rateio_diario(armazenado_gb * dom.PRECOS["logs_armazenamento_gb_mes"], d)
                + self._series_hora(k) * dom.PRECOS["metrica_hora"],
                0,
                origem="medido+rateio",
            )

    def _series_hora(self, dia: str) -> float:
        """Horas-série de métrica personalizada da PROGEP no dia.

        A métrica personalizada é cobrada **por hora em que recebe dado**. O
        Monitor de Consumo publica 2 séries por sessão de conversa (dimensão
        ``SessionId``) e os filtros do log de invocações publicam 2 séries fixas
        (tokens de entrada e saída) em toda hora com uso. Contar uma hora por
        pergunta para as séries do Monitor é o teto: perguntas da mesma conversa
        na mesma hora caem na mesma série.
        """
        perguntas = self._perguntas.get(dia, 0.0)
        horas_com_uso = self.horas_ativas.get("inv", {}).get(dia, 0)
        if dia < MONITOR_SEM_SESSAO_DESDE:
            return 2 * perguntas + 2 * horas_com_uso
        # Monitor sem dimensão de sessão: 2 séries fixas, como as dos filtros.
        return 4 * horas_com_uso

    def _logs_armazenados(self) -> int:
        total = 0
        for prefixo in PREFIXOS_DE_LOG:
            for p in self.logs.get_paginator("describe_log_groups").paginate(
                    logGroupNamePrefix=prefixo):
                total += sum(int(g.get("storedBytes") or 0) for g in p["logGroups"])
        return total

    # -- fixos ---------------------------------------------------------------------

    def _armazenamento(self, dias: list[date]) -> None:
        bytes_s3 = 0.0
        series = self._series("AWS/S3", "BucketSizeBytes", lambda dims: any(
            d["Name"] == "BucketName" and d["Value"].startswith(PREFIXO) for d in dims))
        if series:
            consultas = [{"Id": f"b{i}", "Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                          "Dimensions": dims} for i, dims in enumerate(series)]
            ultimo = max(dias)
            r = self._somas_por_dia(consultas, [ultimo - timedelta(days=3), ultimo],
                                    periodo=86400, estatistica="Average")
            # Métrica diária: vale o valor mais recente de cada bucket.
            for valores in r.values():
                if valores:
                    bytes_s3 += valores[max(valores)]
        bytes_ecr = 0
        if self.ecr_repo:
            for p in self.ecr.get_paginator("describe_images").paginate(repositoryName=self.ecr_repo):
                bytes_ecr += sum(int(i.get("imageSizeInBytes") or 0) for i in p["imageDetails"])
        vetores = 0
        if self.vector_bucket and self.vector_index:
            parametros = {"vectorBucketName": self.vector_bucket, "indexName": self.vector_index,
                          "maxResults": 1000}
            while True:
                r = self.s3vectors.list_vectors(**parametros)
                vetores += len(r.get("vectors", []))
                if not r.get("nextToken"):
                    break
                parametros["nextToken"] = r["nextToken"]
        mensal = (bytes_s3 / dom.GB * dom.PRECOS["s3_gb_mes"]
                  + bytes_ecr / dom.GB * dom.PRECOS["ecr_gb_mes"]
                  + vetores * dom.BYTES_POR_VETOR / dom.GB * dom.PRECOS["s3vectors_gb_mes"])
        for d in dias:
            self.loja.definir(d.isoformat(), "infraestrutura#armazenamento",
                              dom.rateio_diario(mensal, d), 0, origem="rateio")
            # Sem quantidade: somada por dia viraria "60 segredos" num mês.
            self.loja.definir(d.isoformat(), "infraestrutura#segredos",
                              dom.rateio_diario(self.segredos * dom.PRECOS["segredo_mes"], d),
                              0, origem="rateio")

    # -- fatura --------------------------------------------------------------------

    def fatura(self, hoje: date) -> None:
        """Cost Explorer, do 1º dia do mês anterior até hoje (dias UTC).

        Só entram serviços que, nesta conta, são **inteiramente** da PROGEP:
        CloudTrail (a única trilha paga é a ``progep-ifs-trail``; a da
        organização é a cópia gratuita) e o Claude Sonnet 4.5 (os outros
        projetos usam outros modelos). O CloudTrail vira custo do dia; o modelo
        fica como conferência do medido.
        """
        if self.ce is None:
            return
        primeiro = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
        parametros: dict[str, Any] = {
            "TimePeriod": {"Start": primeiro.isoformat(),
                           "End": (hoje + timedelta(days=1)).isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        while True:
            r = self.ce.get_cost_and_usage(**parametros)
            self.consultas_fatura += 1
            for dia in r.get("ResultsByTime", []):
                d = dia["TimePeriod"]["Start"]
                for g in dia.get("Groups", []):
                    servico = g["Keys"][0]
                    usd = float(g["Metrics"]["UnblendedCost"]["Amount"])
                    if servico == "AWS CloudTrail":
                        self.loja.definir(d, "infraestrutura#auditoria", usd, 0, origem="fatura")
                    elif servico.startswith("Claude Sonnet 4.5"):
                        self.loja.definir_fatura(d, "modelo", usd)
            if not r.get("NextPageToken"):
                break
            parametros["NextPageToken"] = r["NextPageToken"]

    # -- tudo ----------------------------------------------------------------------

    def coletar(self, dias: list[date]) -> None:
        self._agentcore(dias)
        self._servicos(dias)
        self._armazenamento(dias)

    def custo_das_consultas(self) -> float:
        """O que esta coleta custou (GetMetricData e Cost Explorer)."""
        return (self.metricas_consultadas * dom.PRECOS["getmetricdata_metrica"]
                + self.consultas_fatura * dom.PRECOS["cost_explorer_consulta"])


def _grupo_da_progep(nome: str) -> bool:
    return nome.startswith(PREFIXOS_DE_LOG)


def _coletor(loja: CustosStore) -> Coletor:
    import boto3

    regiao = os.environ.get("AWS_REGION")
    return Coletor(
        loja,
        cloudwatch=boto3.client("cloudwatch", region_name=regiao),
        lambda_=boto3.client("lambda", region_name=regiao),
        logs=boto3.client("logs", region_name=regiao),
        ecr=boto3.client("ecr", region_name=regiao),
        s3vectors=boto3.client("s3vectors", region_name=regiao),
        # Cost Explorer só existe em us-east-1.
        ce=boto3.client("ce", region_name="us-east-1"),
        runtime_arn=os.environ.get("AGENT_RUNTIME_ARN", ""),
        ecr_repo=os.environ.get("CUSTOS_ECR_REPO", ""),
        vector_bucket=os.environ.get("CUSTOS_VECTOR_BUCKET", ""),
        vector_index=os.environ.get("CUSTOS_VECTOR_INDEX", ""),
        segredos=int(os.environ.get("CUSTOS_SEGREDOS", "0") or 0),
    )


def coleta_handler(evento: Mapping[str, Any] | None = None, contexto: Any = None) -> dict:
    """Agendada de hora em hora: hoje e ontem (a métrica da última hora de ontem
    só fica completa depois da meia-noite)."""
    loja = _loja()
    coletor = _coletor(loja)
    hoje = dom.hoje_local()
    dias = [hoje - timedelta(days=1), hoje]
    coletor.coletar(dias)

    agora = datetime.now(timezone.utc)
    ultima = loja.ler_estado("fatura_em")
    if not ultima or agora - datetime.fromisoformat(ultima) >= timedelta(hours=HORAS_ENTRE_FATURAS):
        try:
            coletor.fatura(hoje)
            loja.salvar_estado("fatura_em", agora.isoformat(timespec="seconds"))
        except Exception:  # noqa: BLE001 - a fatura é conferência, não pode parar a coleta
            logger.exception("Falha ao ler a fatura (Cost Explorer)")
    # Soma (não substitui): cada execução tem o seu custo de consultas.
    loja.somar(hoje.isoformat(), "infraestrutura#controle_custos", coletor.custo_das_consultas(), 1)
    logger.info("[custos] coleta dias=%s", [d.isoformat() for d in dias])
    return {"dias": [d.isoformat() for d in dias]}
