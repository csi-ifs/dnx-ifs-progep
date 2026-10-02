"""Controle de custos do assistente: preços, atribuição e resumo (lógica pura).

O painel mostra o custo **medido**: cada consumo real (tokens de uma chamada ao
modelo, uma mensagem do WhatsApp, uma hora de CPU do runtime) multiplicado pelo
preço público do serviço. Não é estimativa nem projeção — é o mesmo cálculo que
a fatura faz, só que minutos depois do uso em vez de até um dia depois.

**Só o que é da PROGEP.** A conta AWS do IFS também hospeda os projetos ``den``
e ``diae``, e a fatura junta os serviços que eles usam em comum com a gente
(AgentCore, S3, CloudWatch, o próprio Bedrock). Por isso a atribuição é feita
pelo **recurso que consumiu** — a role do nosso runtime, a da nossa base, o
nosso número de WhatsApp —, nunca pelo total do serviço na fatura.

Os preços foram conferidos em 02/10/2026 nas páginas de preço da AWS e, para os
itens que já apareciam na fatura, contra a própria fatura de setembro (modelo,
embeddings, AgentCore). Preço muda: quando mudar, o lugar é :data:`PRECOS`.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

#: Fuso do IFS. Fixo em UTC−3: o Brasil não tem horário de verão desde 2019, e
#: "o gasto de hoje" tem de virar à meia-noite de Aracaju, não à de Greenwich.
FUSO = timezone(timedelta(hours=-3))

#: Cota de mensagens de serviço grátis por número e por mês (Meta, desde
#: 01/10/2026). Acima dela a Meta cobra cada mensagem entregue.
COTA_META_MENSAL = 1000

# --- Catálogo -------------------------------------------------------------------

CATEGORIAS: dict[str, str] = {
    "assistente": "Assistente",
    "documentos": "Documentos",
    "infraestrutura": "Infraestrutura",
}


@dataclass(frozen=True)
class Item:
    categoria: str
    chave: str
    rotulo: str
    #: Como a quantidade é lida na tela ("mensagens", "chamadas"...).
    unidade: str = ""

    @property
    def id(self) -> str:
        return f"{self.categoria}#{self.chave}"


ITENS: dict[str, Item] = {
    i.id: i
    for i in (
        Item("assistente", "modelo", "Modelo Claude (respostas e classificação)", "chamadas"),
        Item("assistente", "busca", "Busca nos documentos", "buscas"),
        Item("assistente", "execucao", "Execução do assistente (AgentCore)", "perguntas"),
        Item("assistente", "whatsapp_enviadas", "WhatsApp: mensagens enviadas (AWS)", "mensagens"),
        Item("assistente", "whatsapp_recebidas", "WhatsApp: mensagens recebidas (AWS)", "mensagens"),
        Item("assistente", "whatsapp_meta", "WhatsApp: tarifa da Meta", "mensagens cobradas"),
        Item("assistente", "atendimento", "Atendimento humano (Connect)", "mensagens"),
        Item("documentos", "indexacao", "Preparação de documentos para a busca", "trechos"),
        Item("documentos", "equipe_tecnica", "Equipe técnica: transcrições e testes", "chamadas"),
        Item("infraestrutura", "auditoria", "Registro de auditoria (CloudTrail)"),
        Item("infraestrutura", "segredos", "Guarda de credenciais (Secrets Manager)"),
        Item("infraestrutura", "registros", "Logs e métricas (CloudWatch)"),
        Item("infraestrutura", "armazenamento", "Armazenamento (documentos, índice e imagens)"),
        Item("infraestrutura", "automacoes", "Automações (Lambda)", "execuções"),
        Item("infraestrutura", "banco", "Banco de conversas (DynamoDB)"),
        Item("infraestrutura", "api", "API do site e do painel (API Gateway)", "requisições"),
        Item("infraestrutura", "controle_custos", "Este controle de custos (consultas)", "coletas"),
    )
}

#: Contagem sem custo próprio, mostrada ao lado (custo por pergunta).
PERGUNTAS = "assistente#perguntas"

# --- Preços (USD) -----------------------------------------------------------------

#: USD por milhão de tokens: entrada, saída, leitura de cache, escrita de cache.
#: Casado por trecho do ``modelId``. Sonnet 4.5 acima de 200 mil tokens de
#: entrada custa o dobro, mas nenhuma chamada nossa chega perto (2–10 mil).
PRECO_MODELOS: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
    "titan-embed-text-v2": (0.02, 0.0, 0.0, 0.0),
}

PRECOS: dict[str, float] = {
    # End User Messaging Social (WhatsApp)
    "whatsapp_enviada": 0.005,
    "whatsapp_recebida": 0.001,
    # Meta, mensagem de serviço no Brasil acima da cota (US$ 0,0068).
    "meta_servico_br": 0.0068,
    # Amazon Connect, chat: por mensagem enviada ou recebida.
    "connect_mensagem": 0.010,
    # AgentCore Runtime: consumo ativo.
    "agentcore_vcpu_hora": 0.0895,
    "agentcore_gb_hora": 0.00945,
    # Lambda x86_64
    "lambda_gb_segundo": 0.0000166667,
    "lambda_requisicao": 0.20 / 1_000_000,
    # DynamoDB sob demanda
    "dynamodb_escrita": 0.625 / 1_000_000,
    "dynamodb_leitura": 0.125 / 1_000_000,
    # CloudWatch
    "logs_ingestao_gb": 0.50,
    "logs_armazenamento_gb_mes": 0.03,
    # Métrica personalizada: US$ 0,30 por mês, rateado por hora com dado.
    "metrica_hora": 0.30 / 730,
    # Consultas do próprio controle de custos.
    "getmetricdata_metrica": 0.01 / 1000,
    "cost_explorer_consulta": 0.01,
    # Armazenamento
    "s3_gb_mes": 0.023,
    "s3vectors_gb_mes": 0.06,
    "ecr_gb_mes": 0.10,
    # Secrets Manager e API Gateway (REST)
    "segredo_mes": 0.40,
    "api_requisicao": 3.50 / 1_000_000,
}

#: Bytes por vetor no S3 Vectors: 1024 floats (4 KB) + texto do trecho e
#: metadados. Calibrado na fatura de setembro: 12.648 vetores ≈ US$ 0,005/mês.
BYTES_POR_VETOR = 6_000

GB = 1024**3


def preco_tokens(modelo: str) -> tuple[float, float, float, float] | None:
    for trecho, preco in PRECO_MODELOS.items():
        if trecho in modelo:
            return preco
    return None


def custo_tokens(
    modelo: str, entrada: int, saida: int, cache_leitura: int = 0, cache_escrita: int = 0
) -> float | None:
    """Custo de uma chamada; ``None`` para modelo sem preço cadastrado."""
    preco = preco_tokens(modelo)
    if preco is None:
        return None
    p_in, p_out, p_cr, p_cw = preco
    return (entrada * p_in + saida * p_out + cache_leitura * p_cr + cache_escrita * p_cw) / 1e6


# --- Datas --------------------------------------------------------------------------

def dia_local(ts_ms: int | float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, FUSO).date().isoformat()


def mes_utc(ts_ms: int | float) -> str:
    """Mês da cota da Meta, que conta em UTC."""
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m")


def hoje_local(agora: datetime | None = None) -> date:
    return (agora or datetime.now(timezone.utc)).astimezone(FUSO).date()


def rateio_diario(valor_mensal: float, dia: date) -> float:
    """Parte de um custo mensal que cabe a um dia do mês."""
    return valor_mensal / calendar.monthrange(dia.year, dia.month)[1]


# --- Chamadas ao modelo ----------------------------------------------------------------

@dataclass(frozen=True)
class Invocacao:
    request_id: str
    modelo: str
    identidade: str
    entrada: int
    saida: int
    cache_leitura: int = 0
    cache_escrita: int = 0


def interpretar_invocacao(mensagem: str) -> Invocacao | None:
    """Uma linha do log de invocações do Bedrock; ``None`` se não for uma."""
    try:
        d = json.loads(mensagem)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("schemaType") != "ModelInvocationLog":
        return None
    entrada = d.get("input") or {}
    saida = d.get("output") or {}
    return Invocacao(
        request_id=str(d.get("requestId") or ""),
        modelo=str(d.get("modelId") or ""),
        identidade=str((d.get("identity") or {}).get("arn") or ""),
        entrada=int(entrada.get("inputTokenCount") or 0),
        saida=int(saida.get("outputTokenCount") or 0),
        cache_leitura=int(entrada.get("cacheReadInputTokenCount") or 0),
        cache_escrita=int(entrada.get("cacheWriteInputTokenCount") or 0),
    )


def item_da_invocacao(identidade: str, usuarios_equipe: Iterable[str] = ()) -> str | None:
    """A quem cabe a chamada — ou ``None`` se for de outro projeto.

    ``assumed-role/<role>/<sessão>``:

    * ``progep-ifs-agentcore-exec`` → o assistente gerando a resposta ou
      classificando o setor;
    * ``progep-ifs-kb-*`` com sessão ``BKB-Retrieve-*`` → o embedding da
      pergunta na busca; qualquer outra sessão da role da base (``EmbeddingTask``,
      ``BKB-CP``) → indexação de documentos;
    * ``DNXAccess/<e-mail>`` da equipe técnica → transcrições e testes rodados à
      mão. Só os e-mails listados: a mesma role federada é usada pelas equipes
      dos outros projetos.
    """
    partes = identidade.split("/")
    if len(partes) < 3 or ":assumed-role" not in partes[0]:
        return None
    role, sessao = partes[1], partes[2]
    if role == "progep-ifs-agentcore-exec":
        return "assistente#modelo"
    if role.startswith("progep-ifs-kb"):
        return "assistente#busca" if sessao.startswith("BKB-Retrieve") else "documentos#indexacao"
    equipe = {u.strip().casefold() for u in usuarios_equipe if u.strip()}
    if role == "DNXAccess" and sessao.casefold() in equipe:
        return "documentos#equipe_tecnica"
    return None


# --- Linhas de log do WhatsApp e do Connect -------------------------------------------------

_ENTRADA = re.compile(r"\[entrada\] msg=(\S+)")
_ENTREGA = re.compile(r"\[entrega\] (sent|delivered) msg=(\S+)")
_CHAT = re.compile(r"\[chat\] mensagem papel=(\w+) id=(\S+)")


def interpretar_linha_whatsapp(mensagem: str) -> tuple[str, str] | None:
    """``("recebida"|"enviada"|"entregue", id)`` de uma linha da Lambda do WhatsApp.

    A AWS cobra a mensagem enviada (status ``sent``) e a recebida; a Meta cobra a
    entregue (``delivered``) acima da cota. ``[entrada] repetida`` não casa de
    propósito: é a mesma mensagem reentregue, que não gera cobrança nova.
    """
    m = _ENTRADA.search(mensagem)
    if m:
        return "recebida", m.group(1)
    m = _ENTREGA.search(mensagem)
    if m:
        return ("enviada" if m.group(1) == "sent" else "entregue"), m.group(2)
    return None


def interpretar_linha_chat(mensagem: str) -> str | None:
    """Id da mensagem de chat do Connect (cliente ou atendente)."""
    m = _CHAT.search(mensagem)
    return m.group(2) if m else None


def custo_meta(entregues_no_mes: int, *, isento: bool = False,
               cota: int = COTA_META_MENSAL) -> float:
    """Tarifa da Meta para a ``entregues_no_mes``-ésima mensagem entregue."""
    if isento or entregues_no_mes <= cota:
        return 0.0
    return PRECOS["meta_servico_br"]


# --- Resumo para a tela --------------------------------------------------------------------

#: Itens medidos que a fatura do Claude Sonnet 4.5 cobre.
ITENS_DO_MODELO = ("assistente#modelo", "documentos#equipe_tecnica")


def media_diaria(linhas: Iterable[Mapping[str, Any]]) -> float | None:
    """Média por dia **com registro** — no início da operação, dia sem nenhum
    registro é dia antes do controle existir, não dia de custo zero."""
    por_dia: dict[str, float] = {}
    for l in linhas:
        if str(l.get("item")) in ITENS:
            por_dia[str(l.get("dia"))] = por_dia.get(str(l.get("dia")), 0.0) + _num(l.get("usd"))
    return sum(por_dia.values()) / len(por_dia) if por_dia else None


def conferir_modelo(
    linhas: Iterable[Mapping[str, Any]], faturas: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Medido × faturado do modelo Claude, até o penúltimo dia que a fatura tem.

    O último dia da fatura costuma estar incompleto (o Cost Explorer atualiza
    algumas vezes ao dia), por isso fica de fora. Os dias da fatura são UTC e os
    do medido, do fuso do IFS: no total de vários dias a diferença se dilui; num
    dia isolado, até 3 h de uso podem cair no dia vizinho.
    """
    por_dia = {str(f.get("dia")): _num(f.get("usd")) for f in faturas
               if str(f.get("item")) == "modelo"}
    if len(por_dia) < 2:
        return None
    ate = (date.fromisoformat(max(por_dia)) - timedelta(days=1)).isoformat()
    faturado = sum(v for d, v in por_dia.items() if d <= ate)
    medido = sum(_num(l.get("usd")) for l in linhas
                 if str(l.get("item")) in ITENS_DO_MODELO and str(l.get("dia")) <= ate)
    return {"ate": ate, "medido_usd": round(medido, 4), "faturado_usd": round(faturado, 4)}


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def resumir(
    linhas: Iterable[Mapping[str, Any]],
    *,
    inicio: date,
    fim: date,
    hoje: date,
    faturas: Iterable[Mapping[str, Any]] = (),
    entregues_meta_mes: int = 0,
    meta_isento: bool = False,
    media_diaria: float | None = None,
) -> dict[str, Any]:
    """Monta o que a aba de custos mostra para ``[inicio, fim]``.

    ``linhas`` são registros ``{"dia", "item", "usd", "quantidade"}``. Itens fora
    do catálogo são ignorados (um item antigo não pode quebrar a tela).

    ``media_diaria`` (dos últimos dias fechados) alimenta a projeção do mês. Ela
    vem de fora porque, no começo do mês, os dias já fechados são poucos: com
    um só, um dia atípico (uma bateria de testes) virava a projeção do mês
    inteiro — medido na prévia, US$ 69 para um mês que caminhava para US$ 10.
    """
    dias = [(inicio + timedelta(days=i)).isoformat() for i in range((fim - inicio).days + 1)]
    por_dia: dict[str, dict[str, float]] = {d: {c: 0.0 for c in CATEGORIAS} for d in dias}
    por_item: dict[str, dict[str, float]] = {}
    perguntas = 0.0
    atualizado = ""

    for linha in linhas:
        dia = str(linha.get("dia", ""))
        item = str(linha.get("item", ""))
        if dia not in por_dia:
            continue
        atualizado = max(atualizado, str(linha.get("atualizado_em") or ""))
        if item == PERGUNTAS:
            perguntas += _num(linha.get("quantidade"))
            continue
        if item not in ITENS:
            continue
        usd = _num(linha.get("usd"))
        por_dia[dia][ITENS[item].categoria] += usd
        acumulado = por_item.setdefault(item, {"usd": 0.0, "quantidade": 0.0})
        acumulado["usd"] += usd
        acumulado["quantidade"] += _num(linha.get("quantidade"))

    total = sum(sum(v.values()) for v in por_dia.values())
    assistente = sum(v["assistente"] for v in por_dia.values())
    itens = [
        {
            "item": chave,
            "categoria": ITENS[chave].categoria,
            "rotulo": ITENS[chave].rotulo,
            "unidade": ITENS[chave].unidade,
            "usd": round(v["usd"], 6),
            "quantidade": round(v["quantidade"], 3),
        }
        for chave, v in sorted(por_item.items(), key=lambda kv: (
            list(CATEGORIAS).index(ITENS[kv[0]].categoria), -kv[1]["usd"]))
    ]

    # Projeção do mês: só faz sentido quando o período é o mês corrente.
    projecao = None
    mes_corrente = inicio.day == 1 and (inicio.year, inicio.month) == (hoje.year, hoje.month)
    if mes_corrente and media_diaria is not None:
        dias_no_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        ate_hoje = sum(sum(por_dia[d].values()) for d in dias if d <= hoje.isoformat())
        projecao = round(ate_hoje + media_diaria * (dias_no_mes - hoje.day), 4)

    return {
        "inicio": inicio.isoformat(),
        "fim": fim.isoformat(),
        "hoje": hoje.isoformat(),
        "total_usd": round(total, 6),
        "por_categoria": {
            c: round(sum(v[c] for v in por_dia.values()), 6) for c in CATEGORIAS
        },
        "categorias": CATEGORIAS,
        "dias": [
            {"dia": d, **{c: round(v, 6) for c, v in por_dia[d].items()}} for d in dias
        ],
        "itens": itens,
        "perguntas": int(perguntas),
        "custo_por_pergunta_usd": round(assistente / perguntas, 6) if perguntas else None,
        "projecao_mes_usd": projecao,
        "media_diaria_usd": round(media_diaria, 6) if media_diaria is not None else None,
        "meta": {
            "entregues_no_mes": entregues_meta_mes,
            "cota": COTA_META_MENSAL,
            "isento": meta_isento,
        },
        "faturas": [
            {"dia": str(f.get("dia")), "item": str(f.get("item")), "usd": round(_num(f.get("usd")), 6)}
            for f in faturas
        ],
        "atualizado_em": atualizado or None,
    }
