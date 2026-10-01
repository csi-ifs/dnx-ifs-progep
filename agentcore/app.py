"""Entrypoint do Agente_IA no Amazon Bedrock AgentCore Runtime.

Empacota o fluxo de Agentic RAG já implementado
(:class:`src.adapters.agent.BedrockAgentAdapter`) no contrato do AgentCore
Runtime: um servidor HTTP em ``0.0.0.0:8080`` com ``POST /invocations`` e
``GET /ping``, fornecido pelo ``BedrockAgentCoreApp`` do SDK
``bedrock-agentcore``. A observabilidade (traces OTEL) é emitida automaticamente
quando o container roda sob ``opentelemetry-instrument`` e a observabilidade da
conta está habilitada (ver scripts/deploy_agentcore.ps1).

Payload de invocação (definido por nós):

    {"text": "<pergunta>", "session_id": "<opcional>", "channel": "site|whatsapp"}

Resposta:

    {"status": "answered|not_found|unavailable", "content": "...",
     "sources": [{"doc_id": "...", "score": 0.8}], "confidence": 0.8}

O ``MODEL_ARN`` (inference profile) e o ``KNOWLEDGE_BASE_ID`` vêm do ambiente,
iguais aos da Lambda da API; as chamadas ao Bedrock carregam o ``User_Agent_APN``
(Req 8.3) pelo próprio adapter.
"""

from __future__ import annotations

import os
import time
from typing import Any

import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from src.adapters.agent import BedrockAgentAdapter, BillingGuardError
from src.adapters.conversation_store import ConversationStore
from src.adapters.monitor_adapter import MonitorAdapter
from src.domain.config import AgentStatus, Channel
from src.domain.models import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

_DEFAULT_THRESHOLD = 0.4
_adapter: BedrockAgentAdapter | None = None
_monitor: MonitorAdapter | None = None
_conversas: ConversationStore | None = None

#: Instante em que o processo subiu e quantas invocações ele já atendeu. Se o
#: container fosse recriado a cada requisição, ``_invocacoes`` nunca passaria de
#: 1 e o uptime seria sempre ~0 — é assim que se distingue overhead de
#: inicialização de overhead da plataforma.
_processo_iniciado = time.monotonic()
_invocacoes = 0


def _preaquecer() -> None:
    """Constrói adapter e monitor no *import*, não dentro da requisição.

    Medido: o AgentCore Runtime cria um processo novo a cada invocação
    (``invocacao=1`` em todas as chamadas), então os caches de módulo nunca são
    reaproveitados entre requisições. Construir os clientes boto3 aqui tira o
    custo (carregar os modelos de serviço, resolver credencial) da janela que o
    usuário espera e joga para o startup do container, que a plataforma faz
    antes de entregar a requisição.

    Falha aqui não pode impedir o container de subir: o caminho preguiçoso em
    :func:`_get_adapter` continua valendo como reserva.
    """
    try:
        # Construir o adapter é barato: o custo real é o cliente boto3, criado
        # preguiçosamente no primeiro uso (carregar o modelo de serviço e
        # resolver a credencial). Forçar a criação aqui é o ponto do exercício —
        # pré-aquecer só o objeto não move nada.
        _get_adapter()._get_kb_client()
        _get_monitor().client
        loja = _get_conversas()
        if loja is not None:
            loja.client
    except Exception:  # noqa: BLE001 - pré-aquecimento é otimização, não requisito
        logger.warning("Pré-aquecimento falhou; seguindo com inicialização preguiçosa",
                       exc_info=True)


def _get_conversas() -> ConversationStore | None:
    """Store do vínculo conversa → sessão do Bedrock; ``None`` se não configurado."""
    global _conversas
    if _conversas is None:
        tabela = os.environ.get("SESSIONS_TABLE")
        if not tabela:
            return None
        _conversas = ConversationStore(tabela, region_name=os.environ.get("AWS_REGION"))
    return _conversas


def _get_monitor() -> MonitorAdapter:
    """Monitor_de_Consumo (CloudWatch), construído uma vez por container."""
    global _monitor
    if _monitor is None:
        _monitor = MonitorAdapter(region_name=os.environ.get("AWS_REGION"))
    return _monitor


def _get_adapter() -> BedrockAgentAdapter:
    """Constrói (uma vez) o adapter Bedrock anotado para billing."""
    global _adapter
    if _adapter is None:
        kb_id = os.environ.get("KNOWLEDGE_BASE_ID")
        if not kb_id:
            raise RuntimeError("KNOWLEDGE_BASE_ID deve estar definido no ambiente")
        _adapter = BedrockAgentAdapter(
            knowledge_base_id=kb_id,
            region_name=os.environ.get("AWS_REGION"),
            model_arn=os.environ.get("MODEL_ARN") or None,
        )
    return _adapter


def _record_failure(session_id: str, reason: str) -> None:
    """Registra falha de chamada de LLM no Monitor_de_Consumo (Req 7.4)."""
    try:
        _get_monitor().record_call_failure(session_id, reason=reason)
    except Exception:  # noqa: BLE001 - monitoramento não pode derrubar o fluxo
        logger.exception("Falha ao registrar erro de LLM no Monitor_de_Consumo")


def _error_response(message: str) -> dict[str, Any]:
    return {
        "status": AgentStatus.UNAVAILABLE.value,
        "content": message,
        "sources": [],
        "confidence": 0.0,
    }


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Processa uma requisição do Agentic RAG e devolve a resposta estruturada."""
    global _invocacoes
    _invocacoes += 1
    marco_inicio = time.perf_counter()
    reaproveitou = _adapter is not None

    text = str((payload or {}).get("text", "") or "").strip()
    if not text:
        return _error_response("Requisição sem texto.")

    channel = str((payload or {}).get("channel", Channel.SITE.value) or Channel.SITE.value)
    session_id = str((payload or {}).get("session_id", "") or "agentcore")
    threshold = float(os.environ.get("RELEVANCE_THRESHOLD", str(_DEFAULT_THRESHOLD)))

    request = AgentRequest(session_id=session_id, channel=channel, text=text)
    # Recupera a conversa em curso: é o que permite entender um follow-up.
    # Sem store configurado (ou sem conversa aberta), o turno começa do zero.
    loja = _get_conversas()
    sessao_bedrock = loja.get(channel, session_id) if loja else ""
    try:
        adapter = _get_adapter()
        marco_adapter = time.perf_counter()
        turno = adapter.forward_turn(
            request, threshold, provider_session_id=sessao_bedrock
        )
        response: AgentResponse = turno.response
        marco_agente = time.perf_counter()
    except BillingGuardError as exc:
        _record_failure(session_id, f"billing guard: {exc}")
        return _error_response(f"Chamada bloqueada pela anotação de billing: {exc}")
    except Exception as exc:  # noqa: BLE001 - superfície de erro sanitizada
        # Req 7.4: registra a tentativa falha com o session_id, preservando os
        # registros anteriores; o usuário recebe mensagem sanitizada.
        # O detalhe vai para o log do runtime: o Monitor_de_Consumo guarda só a
        # contagem, e sem esta linha a causa de um "indisponível" se perdia
        # (17/09/2026).
        logger.exception("[falha] turno do agente falhou canal=%s", channel)
        _record_failure(session_id, str(exc))
        return _error_response("O serviço está temporariamente indisponível.")

    # Req 7.2: registra a resposta gerada associada à sessão. Falha de registro
    # nunca derruba o atendimento (o Monitor já faz retry ≤3 internamente).
    try:
        _get_monitor().record_response(session_id, response.content)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao registrar resposta no Monitor_de_Consumo")

    # Guarda a alça devolvida pelo Bedrock para o próximo turno. Renova o TTL a
    # cada mensagem, então a janela conta a partir da última interação.
    if loja is not None and turno.provider_session_id:
        loja.salvar_sessao(channel, session_id, turno.provider_session_id)
    marco_monitor = time.perf_counter()

    # Uma linha por invocação com a decomposição do tempo *dentro* do container.
    # A diferença entre este total e o tempo visto pelo chamador é o overhead da
    # plataforma AgentCore — que só dá para separar medindo dos dois lados.
    logger.info(
        "[tempo] reaproveitou_adapter=%s invocacao=%d uptime=%.1fs "
        "adapter=%.3fs agente=%.3fs monitor=%.3fs total=%.3fs",
        reaproveitou,
        _invocacoes,
        time.monotonic() - _processo_iniciado,
        marco_adapter - marco_inicio,
        marco_agente - marco_adapter,
        marco_monitor - marco_agente,
        marco_monitor - marco_inicio,
    )

    # Por que a resposta saiu assim. Um ``not_found`` tem três origens possíveis
    # e o status sozinho não as separa — sem esta linha, um relato de "não
    # respondeu" só é investigável se der para reproduzir, e nem sempre dá.
    # Não registra o texto da pergunta: ele já fica no log de invocação do
    # Bedrock, e repeti-lo aqui espalharia conteúdo de conversa sem necessidade.
    logger.info(
        "[decisao] status=%s motivo=%s trechos=%d citacoes=%d sessao=%s",
        response.status,
        turno.motivo,
        turno.trechos,
        turno.citacoes,
        "retomada" if sessao_bedrock else "nova",
    )

    # Sem resposta: qual setor da PROGEP indicar (pedido do cliente, 28/09/2026).
    # Falha aqui não pode virar "indisponível" — o contato geral é o fallback.
    setores: tuple[str, ...] = ()
    if response.status == AgentStatus.NOT_FOUND.value:
        try:
            setores = adapter.classificar_setor(text)
        except Exception:  # noqa: BLE001
            logger.warning("Classificação de setor falhou; usando o geral", exc_info=True)
            setores = ("PROGEP",)
        logger.info("[setor] setores=%s", ",".join(setores))

    return {
        "status": response.status,
        "content": response.content,
        "sources": [{"doc_id": s.doc_id, "score": s.score} for s in response.sources],
        "confidence": response.confidence,
        "setores": list(setores),
    }


# Roda no import do módulo, ou seja, durante o startup do container.
_preaquecer()


if __name__ == "__main__":
    app.run()
