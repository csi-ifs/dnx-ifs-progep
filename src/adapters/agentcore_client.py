"""Cliente para invocar o Agente_IA hospedado no Bedrock AgentCore Runtime.

Após a migração para arquitetura única, os handlers (API do site e webhook do
WhatsApp) não executam mais o fluxo Agentic RAG in-process: eles **invocam o
AgentCore Runtime** (`bedrock-agentcore invoke-agent-runtime`), que roda o
container com o `BedrockAgentAdapter`. Este adapter é a fronteira de I/O dessa
invocação: monta o payload, chama o runtime e converte a resposta JSON de volta
em :class:`~src.domain.models.AgentResponse`.

O ``boto3`` é importado de forma protegida para o módulo importar mesmo sem o SDK
(testes injetam um cliente fake). A anotação de billing ``User_Agent_APN`` é
injetada no user-agent do cliente (Req 8.3) — as chamadas de LLM em si acontecem
dentro do runtime, mas mantemos a anotação em toda chamada ao Bedrock.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.domain.config import USER_AGENT_APN, AgentStatus
from src.domain.models import AgentRequest, AgentResponse, SourceRef

logger = logging.getLogger(__name__)

# --- Guarded boto3 import -------------------------------------------------------
try:  # pragma: no cover - depende do ambiente
    import boto3
    from botocore.config import Config as BotoConfig

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    BotoConfig = None  # type: ignore[assignment]
    _BOTO3_AVAILABLE = False

#: Nome do serviço boto3 do data plane do AgentCore Runtime.
AGENTCORE_SERVICE: str = "bedrock-agentcore"


class AgentCoreUnavailableError(RuntimeError):
    """Levantada quando o boto3 não está disponível e nenhum cliente foi injetado."""


def _read_payload(response: dict[str, Any]) -> bytes:
    """Extrai os bytes do corpo de resposta do ``invoke_agent_runtime``.

    A resposta expõe o corpo como um ``StreamingBody`` (chave ``response`` ou
    ``payload``, conforme a versão do SDK) ou, em fakes de teste, já como bytes.
    """
    for key in ("response", "payload", "body"):
        value = response.get(key)
        if value is None:
            continue
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if hasattr(value, "read"):
            return value.read()
        if isinstance(value, str):
            return value.encode("utf-8")
    return b""


class AgentCoreRuntimeAdapter:
    """Invoca o Agente_IA no AgentCore Runtime e devolve a resposta estruturada."""

    def __init__(
        self,
        agent_runtime_arn: str,
        *,
        region_name: str | None = None,
        client: Any | None = None,
        user_agent_extra: str | None = USER_AGENT_APN,
    ) -> None:
        """Inicializa o adapter.

        Args:
            agent_runtime_arn: ARN do runtime a invocar.
            region_name: Região AWS para o cliente criado sob demanda.
            client: Cliente ``bedrock-agentcore`` pré-construído (injete um fake
                em testes). Criado sob demanda quando omitido.
            user_agent_extra: Anotação de billing injetada no cliente (Req 8.3).
        """
        if not (agent_runtime_arn and agent_runtime_arn.strip()):
            raise ValueError("agent_runtime_arn não pode ser vazio")
        self.agent_runtime_arn = agent_runtime_arn
        self._region_name = region_name
        self._client = client
        self._user_agent_extra = user_agent_extra

    @property
    def client(self) -> Any:
        """Cliente ``bedrock-agentcore``, criado sob demanda com a anotação APN."""
        if self._client is None:
            if not _BOTO3_AVAILABLE:
                raise AgentCoreUnavailableError(
                    "boto3 indisponível e nenhum cliente injetado; instale boto3 "
                    "ou injete um cliente para usar o AgentCoreRuntimeAdapter."
                )
            self._client = boto3.client(
                AGENTCORE_SERVICE,
                region_name=self._region_name,
                config=BotoConfig(user_agent_extra=self._user_agent_extra),
            )
        return self._client

    def invoke(self, request: AgentRequest) -> AgentResponse:
        """Invoca o runtime com a requisição e converte a resposta.

        Args:
            request: A requisição do usuário.

        Returns:
            A :class:`AgentResponse` produzida pelo runtime; em erro de parsing,
            uma resposta ``unavailable`` sanitizada.
        """
        payload = json.dumps(
            {
                "text": request.text,
                "channel": request.channel,
                "session_id": request.session_id,
            }
        ).encode("utf-8")

        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.agent_runtime_arn,
            payload=payload,
            contentType="application/json",
        )

        raw = _read_payload(response)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.error("Resposta do AgentCore Runtime ilegível: %r", raw[:200])
            return AgentResponse(
                status=AgentStatus.UNAVAILABLE.value, content="",
                sources=(), confidence=0.0,
            )

        sources = tuple(
            SourceRef(doc_id=str(s.get("doc_id", "")), score=float(s.get("score", 0.0)))
            for s in (data.get("sources") or [])
        )
        return AgentResponse(
            status=str(data.get("status", AgentStatus.UNAVAILABLE.value)),
            content=str(data.get("content", "")),
            sources=sources,
            confidence=float(data.get("confidence", 0.0)),
            setores=tuple(str(s) for s in (data.get("setores") or []) if s),
        )
