"""I/O adapter for the Agente_IA (Bedrock AgentCore Runtime + Knowledge Base).

This adapter is the thin I/O boundary for the AI agent (Task 12.3). It:

* forwards a user request to the agent (Req 2.1),
* queries the Base_de_Conhecimento and recovers similarity scores and source
  references (Req 2.1, 2.2), and
* applies the mandatory billing-annotation guard **before any call to Amazon
  Bedrock**, injecting the ``User_Agent_APN`` via
  ``Config(user_agent_extra=...)`` and blocking the call when the annotation is
  missing (Req 8.3, 8.5).

The pure-logic decisions are delegated to the domain layer and reused here:

* :func:`src.domain.billing.guard_llm_call` enforces the billing annotation.
* :func:`src.domain.agent_response.assemble_response` packages the grounded
  answer with one :class:`~src.domain.models.SourceRef` per source document.
* :func:`src.domain.relevance.decide_relevance` decides the ``not_found`` /
  escalation outcome from the retrieved scores.

``boto3`` is imported lazily and guarded so this module can be imported (and
unit-tested with injected fakes) in environments where ``boto3`` is not
installed or AWS is not reachable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.domain.agent_response import assemble_response
from src.domain.billing import (
    DEFAULT_USER_AGENT_KEY,
    BillingGuardResult,
    guard_llm_call,
)
from src.domain.config import USER_AGENT_APN, AgentStatus
from src.domain.models import AgentRequest, AgentResponse, ErrorDetail, SourceRef
from src.domain.relevance import RelevanceDecision, decide_relevance

# --- Guarded boto3 import -------------------------------------------------------
# boto3/botocore are optional at import time so the module remains importable
# (and unit-testable via injected clients) even when the AWS SDK is absent.
try:  # pragma: no cover - exercised implicitly by import
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore

    _BOTO3_AVAILABLE = True
except ImportError:  # pragma: no cover - only taken when boto3 is not installed
    boto3 = None  # type: ignore
    Config = None  # type: ignore
    _BOTO3_AVAILABLE = False


#: boto3 service name for the Knowledge Base retrieval / agent runtime client.
BEDROCK_AGENT_RUNTIME_SERVICE: str = "bedrock-agent-runtime"
#: Cliente das chamadas diretas ao modelo (classificação de setor).
BEDROCK_RUNTIME_SERVICE: str = "bedrock-runtime"

#: Default number of passages to retrieve from the Knowledge Base per query.
DEFAULT_MAX_RESULTS: int = 5

#: Sentinela que o modelo devolve quando os trechos recuperados não respondem à
#: pergunta. Sem ela, o modelo escreve uma negativa em prosa comentando o
#: próprio retrieval ("os documentos listados referem-se a um edital de...") —
#: e como a negativa vem *com* citações, seria entregue como ``answered``.
NO_ANSWER_SENTINEL: str = "NAO_ENCONTRADO"

#: Prompt de geração do RetrieveAndGenerate. Define a persona da PROGEP, proíbe
#: comentar o retrieval e obriga a sentinela na ausência de fundamento.
#: ``$search_results$`` e ``$output_format_instructions$`` são preenchidos pelo
#: Bedrock (o segundo é o que faz a resposta sair com citações).
GENERATION_PROMPT_TEMPLATE: str = f"""Você é o assistente virtual da PROGEP
(Pró-Reitoria de Gestão de Pessoas) do Instituto Federal de Sergipe. Você atende
servidores e candidatos, respondendo apenas com base nos documentos oficiais da
PROGEP.

Trechos dos documentos oficiais:
$search_results$

Regras de resposta:
- Responda em português do Brasil, de forma cordial, direta e objetiva.
- Use somente as informações dos trechos acima. Nunca invente prazos, valores,
  percentuais, datas ou nomes de setores.
- Quando os trechos trouxerem informações diferentes sobre o mesmo ponto, use a
  mais atual: prefira a norma ou o documento de data mais recente (ano da
  portaria, do decreto, da edição ou da versão) e os documentos do IFS
  (cartilhas, instruções normativas e fluxos da PROGEP) aos manuais nacionais
  mais antigos. Não misture as duas versões na mesma resposta.
- Se o servidor questionar ou contestar uma resposta anterior, responda de novo
  apenas com base nos trechos: confirme o que eles dizem ou corrija o que
  estiver diferente, sem acrescentar regra, prazo, valor ou exceção que não
  esteja neles.
- Cobertura parcial não é ausência de resposta. Se os trechos responderem
  apenas parte da pergunta, apresente essa parte normalmente e encerre com a
  linha: "Para os demais casos, recomendo confirmar com a PROGEP."
- Responda exatamente {NO_ANSWER_SENTINEL} e mais nada somente quando os
  trechos não trouxerem nada sobre o assunto perguntado. Na dúvida entre
  responder em parte e usar {NO_ANSWER_SENTINEL}, responda em parte.
- Comece pela informação em si, nunca pelo que os documentos dizem. Escreva
  "O afastamento para pós-graduação exige...", não "Os documentos mencionam o
  afastamento...".
- Nunca comente o processo de busca. Não escreva "os resultados da busca", "os
  documentos fornecidos", "os documentos apresentados", "os documentos
  disponíveis", "os trechos acima", "não consta nos documentos" nem descreva
  quais documentos foram recuperados. Responda como quem já conhece a
  informação.
- Seja breve: no máximo 6 linhas, salvo quando a pergunta exigir uma lista de
  requisitos ou documentos.

$output_format_instructions$"""


#: "os documentos [fornecidos|disponíveis|...] não trazem/contêm/mencionam...":
#: a negativa sobre a busca sem o adjetivo que a lista de vazamentos exige
#: ("Infelizmente, os documentos não trazem informações sobre...", 01/10/2026).
_DOCUMENTOS_NEGAM = re.compile(
    r"\b(?:documentos|trechos)(?: \w+)? n[ãa]o (?:traz|cont|menciona|informa|"
    r"apresenta|especifica|detalha|trata|abord)"
)


def is_no_answer(text: str) -> bool:
    """Whether a generated answer is really a "not grounded" outcome.

    Covers the :data:`NO_ANSWER_SENTINEL` (the instructed path) and the prose
    negatives the model still produces occasionally, which describe the
    retrieval instead of answering and must never reach the user.
    """
    normalized = " ".join(text.split()).strip().strip(".").casefold()
    if not normalized:
        return True
    if NO_ANSWER_SENTINEL.casefold() in normalized:
        return True
    if _DOCUMENTOS_NEGAM.search(normalized):
        return True
    leaks = (
        "resultados da busca",
        "resultados de busca",
        "documentos fornecidos",
        "trechos fornecidos",
        "documentos recuperados",
        "documentos apresentados",
        "documentos disponíveis",
        "documentos disponiveis",
        "trechos acima",
    )
    # "não cont" cobre contém/contêm/contam sem depender do acento circunflexo;
    # "não traz" cobre traz/trazem ("Os documentos fornecidos não trazem
    # informações sobre...", visto em 01/10/2026).
    negatives = ("não cont", "nao cont", "não foi possível", "nao foi possivel",
                 "não apresenta", "nao apresenta", "não encontr", "nao encontr",
                 "não trata", "nao trata", "não detalha", "nao detalha",
                 "não traz", "nao traz", "não menciona", "nao menciona",
                 "não informa", "nao informa", "não especifica", "nao especifica")
    return any(leak in normalized for leak in leaks) and any(
        neg in normalized for neg in negatives
    )


class BillingGuardError(RuntimeError):
    """Raised when a Bedrock call is blocked by the billing guard (Req 8.5).

    The blocked call carries the structured :class:`ErrorDetail` produced by the
    pure-logic guard so callers can report the missing/incorrect
    ``User_Agent_APN`` without leaking internal detail.
    """

    def __init__(self, error: ErrorDetail) -> None:
        super().__init__(error.message)
        self.error = error


class BedrockUnavailableError(RuntimeError):
    """Raised when ``boto3`` is unavailable and no client was injected."""


@dataclass(frozen=True)
class KnowledgeBaseResult:
    """Outcome of a Knowledge Base retrieval (Req 2.1, 2.2).

    Attributes:
        sources: One :class:`SourceRef` per retrieved passage, carrying the
            source document reference and its similarity score, ordered as
            returned by the Knowledge Base (highest relevance first).
        relevance: The :class:`RelevanceDecision` computed from the retrieved
            scores against the configured relevance threshold (Req 2.4).
    """

    sources: tuple[SourceRef, ...]
    relevance: RelevanceDecision

    @property
    def scores(self) -> tuple[float, ...]:
        """Similarity scores of the retrieved passages, in retrieval order."""
        return tuple(ref.score for ref in self.sources)


#: Por que o turno terminou como terminou. Cada ``not_found`` tem uma correção
#: diferente, e sem isso registrado não dá para saber qual aplicar:
#:
#: * ``sem_relevancia`` — nenhum trecho passou do corte de relevância. Hoje é
#:   raro a ponto de ser suspeito (os scores vivem entre 0,63 e 0,78 contra um
#:   corte de 0,4), então indica falha na busca, não pergunta fora de escopo.
#: * ``sentinela`` — o modelo leu os trechos e concluiu que não respondem. É o
#:   caminho normal de uma pergunta fora de escopo; a correção, quando é caso de
#:   corrigir, é de corpus ou de prompt, nunca de infraestrutura.
#: * ``sem_citacao`` — o modelo escreveu uma resposta **de conteúdo** e mesmo
#:   assim nenhuma referência voltou. Raro e suspeito: a resposta pode ter sido
#:   boa e descartada por não haver como sustentá-la.
MOTIVOS = ("ok", "sem_relevancia", "sem_citacao", "sentinela")


def _not_found() -> AgentResponse:
    """Resposta de escalonamento: sem conteúdo e sem fontes (Req 2.4)."""
    return AgentResponse(
        status=AgentStatus.NOT_FOUND.value, content="", sources=(), confidence=0.0
    )


@dataclass(frozen=True)
class AgentTurn:
    """Uma rodada de conversa: a resposta e a alça para continuar o assunto.

    ``provider_session_id`` é o identificador de conversa gerado pelo **Bedrock**
    (ele recusa ids nossos). Guardá-lo e devolvê-lo no turno seguinte é o que
    faz "e quanto tempo antes preciso pedir?" ser entendido em vez de virar
    ``not_found``. Fica aqui, na camada de adapter, para não vazar detalhe de
    fornecedor para o domínio.

    ``motivo`` existe para depuração: um ``not_found`` sai por **três** caminhos
    diferentes e, olhando só o status, eles são indistinguíveis — o que já
    custou uma investigação inteira sem conclusão. Ver :data:`MOTIVOS`.
    """

    response: AgentResponse
    provider_session_id: str = ""
    motivo: str = ""
    trechos: int = 0
    citacoes: int = 0


def _extract_doc_id(result: Mapping[str, Any]) -> str:
    """Extract a stable document identifier from a retrieval result.

    Prefers the source URI carried in the result location (e.g. the S3 object
    URI); falls back to KB-source metadata, then to any ``documentId`` field.
    Returns an empty string only when no identifier can be found.
    """
    location = result.get("location") or {}
    if isinstance(location, Mapping):
        for key in ("s3Location", "webLocation", "confluenceLocation",
                    "salesforceLocation", "sharePointLocation", "customDocumentLocation"):
            loc = location.get(key)
            if isinstance(loc, Mapping):
                uri = loc.get("uri") or loc.get("url") or loc.get("id")
                if uri:
                    return str(uri)

    metadata = result.get("metadata")
    if isinstance(metadata, Mapping):
        source_uri = metadata.get("x-amz-bedrock-kb-source-uri")
        if source_uri:
            return str(source_uri)
        doc_id = metadata.get("documentId") or metadata.get("doc_id")
        if doc_id:
            return str(doc_id)

    doc_id = result.get("documentId")
    return str(doc_id) if doc_id else ""


def _sessao_recusada(exc: BaseException) -> bool:
    """``True`` se o Bedrock recusou o ``sessionId`` (sessão vencida ou inválida).

    Olha código **e** mensagem: ``ValidationException`` também cobre erros de
    configuração, e esses não podem ser engolidos por uma repetição sem sessão.
    """
    erro = (getattr(exc, "response", None) or {}).get("Error", {}) or {}
    return (
        erro.get("Code") == "ValidationException"
        and "session" in str(erro.get("Message", "")).lower()
    )


def parse_citations(
    citations: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Extract the distinct source-document ids cited by a generated answer.

    Each citation of a ``retrieve_and_generate`` response carries the
    ``retrievedReferences`` actually used to ground that span of the answer
    (Req 2.2, 2.3). First-seen order is preserved; duplicates are collapsed.
    """
    seen: dict[str, None] = {}
    for citation in citations:
        for ref in citation.get("retrievedReferences", []) or []:
            doc_id = _extract_doc_id(ref)
            if doc_id:
                seen.setdefault(doc_id, None)
    return tuple(seen)


def parse_retrieval_results(
    retrieval_results: Sequence[Mapping[str, Any]],
) -> tuple[SourceRef, ...]:
    """Parse Bedrock ``retrieve`` results into source references with scores.

    Each entry in ``retrieval_results`` (the ``retrievalResults`` list of a
    Bedrock Knowledge Base ``retrieve`` response) is mapped to a
    :class:`SourceRef` with the document identifier and its similarity score.
    """
    refs: list[SourceRef] = []
    for result in retrieval_results:
        score = result.get("score")
        refs.append(
            SourceRef(
                doc_id=_extract_doc_id(result),
                score=float(score) if score is not None else 0.0,
            )
        )
    return tuple(refs)


class BedrockAgentAdapter:
    """Adapter that forwards requests and queries the Knowledge Base.

    All outbound Bedrock calls are guarded by the mandatory billing annotation
    (Req 8.3, 8.5): the ``User_Agent_APN`` is injected via
    ``Config(user_agent_extra=...)`` on the boto3 client, and the same value is
    validated by :func:`guard_llm_call` **before** any call is dispatched. When
    the annotation is missing or incorrect the call is blocked and a
    :class:`BillingGuardError` is raised.

    The client is injectable so the adapter can be unit-tested without AWS: pass
    a pre-built ``kb_client`` (any object exposing a ``retrieve`` method). When
    no client is injected, a real boto3 ``bedrock-agent-runtime`` client is
    created lazily with the billing-annotated configuration.
    """

    def __init__(
        self,
        knowledge_base_id: str,
        *,
        region_name: str | None = None,
        kb_client: Any | None = None,
        user_agent_extra: str | None = USER_AGENT_APN,
        model_arn: str | None = None,
        llm_client: Any | None = None,
    ) -> None:
        """Initialize the adapter.

        Args:
            knowledge_base_id: The Bedrock Knowledge Base identifier to query.
            region_name: Optional AWS region for the lazily-created client.
            kb_client: Optional pre-built client exposing ``retrieve`` (and
                ``retrieve_and_generate`` when generation is enabled). When
                provided (e.g. a test double), boto3 is not used.
            user_agent_extra: The billing annotation to inject and validate.
                Defaults to :data:`~src.domain.config.USER_AGENT_APN`. Passing
                ``None`` or a different value causes the guard to block calls
                (Req 8.5), which is useful to exercise the failure path.
            model_arn: ARN of the LLM (foundation model or inference profile)
                used by ``retrieve_and_generate`` to compose the grounded
                answer. When ``None``, generation is disabled and the response
                content falls back to the source-reference summary.
        """
        self.knowledge_base_id = knowledge_base_id
        self.region_name = region_name
        self.model_arn = model_arn
        self._user_agent_extra = user_agent_extra
        self._kb_client = kb_client
        self._llm_client = llm_client

    @property
    def billing_config(self) -> dict[str, Any]:
        """The configuration mapping validated by the billing guard (Req 8.3)."""
        return {DEFAULT_USER_AGENT_KEY: self._user_agent_extra}

    def check_billing(self) -> BillingGuardResult:
        """Run the pure-logic billing guard on this adapter's configuration."""
        return guard_llm_call(self.billing_config)

    def _ensure_billing_allowed(self) -> None:
        """Block the outbound Bedrock call unless the annotation is present.

        Raises:
            BillingGuardError: When the ``User_Agent_APN`` is missing/incorrect
                (Req 8.5). No Bedrock call is made in that case.
        """
        result = self.check_billing()
        if not result.allowed:
            assert result.error is not None  # guard guarantees an error here
            raise BillingGuardError(result.error)

    def _build_client(self) -> Any:
        """Create a billing-annotated boto3 ``bedrock-agent-runtime`` client."""
        if not _BOTO3_AVAILABLE:
            raise BedrockUnavailableError(
                "boto3 is not available and no client was injected; "
                "provide a 'kb_client' to use the adapter without AWS."
            )
        # Inject the User_Agent_APN into the client user agent (Req 8.3).
        config = Config(user_agent_extra=self._user_agent_extra)
        return boto3.client(
            BEDROCK_AGENT_RUNTIME_SERVICE,
            region_name=self.region_name,
            config=config,
        )

    def _get_kb_client(self) -> Any:
        """Return the injected client or lazily build a real boto3 client."""
        if self._kb_client is None:
            self._kb_client = self._build_client()
        return self._kb_client

    def _get_llm_client(self) -> Any:
        """Cliente ``bedrock-runtime`` com a mesma anotação de billing (Req 8.3)."""
        if self._llm_client is None:
            if not _BOTO3_AVAILABLE:
                raise BedrockUnavailableError(
                    "boto3 is not available and no llm_client was injected."
                )
            self._llm_client = boto3.client(
                BEDROCK_RUNTIME_SERVICE,
                region_name=self.region_name,
                config=Config(user_agent_extra=self._user_agent_extra),
            )
        return self._llm_client

    def classificar_setor(self, pergunta: str) -> tuple[str, ...]:
        """Setores da PROGEP a indicar para uma pergunta sem resposta.

        Uma chamada curta ao mesmo modelo da geração, só nos ``not_found``: a
        pergunta vai sozinha, com o catálogo de assuntos de
        :mod:`src.domain.contatos`, e a saída é um código. Qualquer saída
        irreconhecível vira o contato geral.

        Raises:
            BillingGuardError: Sem a anotação de billing (Req 8.5).
            Exception: Falha do Bedrock — quem chama decide o fallback.
        """
        from src.domain.contatos import interpretar_classificacao, prompt_classificacao

        if not self.model_arn:
            raise ValueError("model_arn não configurado para classificar")
        self._ensure_billing_allowed()
        resposta = self._get_llm_client().converse(
            modelId=self.model_arn,
            system=[{"text": prompt_classificacao()}],
            messages=[{"role": "user", "content": [{"text": pergunta[:1000]}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0},
        )
        blocos = (((resposta or {}).get("output") or {}).get("message") or {}).get(
            "content"
        ) or []
        texto = " ".join(str(b.get("text", "")) for b in blocos if isinstance(b, Mapping))
        return interpretar_classificacao(texto)

    def query_knowledge_base(
        self,
        request: AgentRequest,
        threshold: float,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> KnowledgeBaseResult:
        """Query the Knowledge Base and recover scores and source references.

        The billing guard is applied **before** any Bedrock call (Req 8.5). On
        success the retrieved passages are parsed into :class:`SourceRef`
        objects (Req 2.1, 2.2) and the relevance decision is computed against
        ``threshold`` (Req 2.4).

        Args:
            request: The user request to forward to the Knowledge Base.
            threshold: The configured relevance threshold (``limiar_relevancia``).
            max_results: Maximum number of passages to retrieve.

        Returns:
            A :class:`KnowledgeBaseResult` with the source references and the
            relevance decision.

        Raises:
            BillingGuardError: When the billing annotation is missing/incorrect.
        """
        self._ensure_billing_allowed()

        client = self._get_kb_client()
        response = client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": request.text},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": max_results}
            },
        )
        results = response.get("retrievalResults", []) if response else []
        sources = parse_retrieval_results(results)
        relevance = decide_relevance(
            [ref.score for ref in sources], threshold, request
        )
        return KnowledgeBaseResult(sources=sources, relevance=relevance)

    def generate_grounded_answer(
        self,
        request: AgentRequest,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        provider_session_id: str = "",
    ) -> tuple[str, tuple[str, ...], str]:
        """Generate a grounded answer via Bedrock ``retrieve_and_generate``.

        The Knowledge Base retrieval and the LLM composition happen in a single
        managed call; the answer is derived exclusively from the retrieved
        passages (Req 2.2) and each cited source document is reported (Req 2.3).
        The billing guard runs **before** the call (Req 8.3, 8.5).

        Args:
            request: The user request to answer.
            max_results: Maximum number of passages retrieved for grounding.

        Returns:
            A pair ``(answer_text, cited_doc_ids)``. An answer without any cited
            document must not be delivered as grounded — callers treat it as
            ``not_found``.

        Raises:
            BillingGuardError: When the billing annotation is missing/incorrect.
            ValueError: When the adapter has no ``model_arn`` configured.
        """
        if not self.model_arn:
            raise ValueError("model_arn não configurado para geração de respostas")
        self._ensure_billing_allowed()

        client = self._get_kb_client()
        # O Bedrock **não** aceita um id escolhido por nós (ValidationException):
        # ele gera o seu na primeira chamada e espera recebê-lo de volta nos
        # turnos seguintes. Por isso o id viaja de volta ao chamador.
        configuracao = {
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": self.knowledge_base_id,
                "modelArn": self.model_arn,
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {"numberOfResults": max_results}
                },
                "generationConfiguration": {
                    "promptTemplate": {
                        "textPromptTemplate": GENERATION_PROMPT_TEMPLATE
                    }
                },
            },
        }

        def chamar(sessao: str) -> Any:
            parametros: dict[str, Any] = {"input": {"text": request.text}}
            if sessao:
                parametros["sessionId"] = sessao
            return client.retrieve_and_generate(
                **parametros, retrieveAndGenerateConfiguration=configuracao
            )

        try:
            response = chamar(provider_session_id)
        except Exception as exc:
            # **Sessão vencida não pode custar a resposta.** O Bedrock expira a
            # sessão por conta própria e a recusa com ValidationException
            # ("Session with Id ... is not valid"). Em 17/09/2026 isso devolveu
            # "temporariamente indisponível" a quem voltou a conversar no dia
            # seguinte. Perder o contexto é melhor: a pergunta é refeita do zero
            # e o id novo substitui o vencido.
            if not (provider_session_id and _sessao_recusada(exc)):
                raise
            logging.getLogger(__name__).warning(
                "Sessão do Bedrock recusada; repetindo sem contexto anterior"
            )
            response = chamar("")
        output = (response or {}).get("output", {}) or {}
        text = str(output.get("text", "") or "")
        cited = parse_citations((response or {}).get("citations", []) or [])
        return text, cited, str((response or {}).get("sessionId", "") or "")

    def forward_request(
        self,
        request: AgentRequest,
        threshold: float,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> AgentResponse:
        """Uma pergunta isolada: :meth:`forward_turn` sem continuidade."""
        return self.forward_turn(
            request, threshold, max_results=max_results
        ).response

    def forward_turn(
        self,
        request: AgentRequest,
        threshold: float,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        provider_session_id: str = "",
    ) -> AgentTurn:
        """Forward a request through the agent flow and assemble a response.

        Continua a conversa quando ``provider_session_id`` é informado: o
        Bedrock reformula a pergunta com o histórico daquela sessão, o que faz
        um follow-up como "e quanto tempo antes preciso pedir?" ser entendido.
        Devolve em :attr:`AgentTurn.provider_session_id` o id a guardar para o
        próximo turno.

        Queries the Knowledge Base (Req 2.1) and, when at least one document
        meets the relevance threshold, assembles an ``answered``
        :class:`AgentResponse` grounded in the retrieved documents with one
        source reference per used document (Req 2.2, 2.3). When no document is
        relevant, returns a ``not_found`` response that also signals escalation
        (Req 2.4). The billing guard is enforced before any Bedrock call
        (Req 8.5).

        Args:
            request: The user request to process.
            threshold: The configured relevance threshold.
            max_results: Maximum number of passages to retrieve.

        Returns:
            An :class:`AgentResponse` (``answered`` or ``not_found``).

        Raises:
            BillingGuardError: When the billing annotation is missing/incorrect.
        """
        if self.model_arn:
            # As duas chamadas partem da mesma pergunta e não dependem uma da
            # outra: o ``retrieve`` traz os scores (threshold e confiança) e o
            # ``retrieve_and_generate`` busca por conta própria de novo, por
            # dentro. Em série custavam ~0,7 s + ~5,2 s; em paralelo o custo é o
            # da mais lenta. Medido no corpus atual, o threshold nunca antecipa
            # a saída (mesmo pergunta fora de escopo pontua ~0,65 contra um
            # corte de 0,4), então não se paga geração descartada por isso.
            with ThreadPoolExecutor(max_workers=2) as pool:
                kb_task = pool.submit(
                    self.query_knowledge_base, request, threshold,
                    max_results=max_results,
                )
                gen_task = pool.submit(
                    self.generate_grounded_answer, request,
                    max_results=max_results,
                    provider_session_id=provider_session_id,
                )
                kb = kb_task.result()
                text, cited, nova_sessao = gen_task.result()
                generated = (text, cited)
        else:
            kb = self.query_knowledge_base(request, threshold, max_results=max_results)
            generated = None
            nova_sessao = provider_session_id

        trechos = len(kb.sources)

        if not kb.relevance.relevant:
            return AgentTurn(_not_found(), nova_sessao, "sem_relevancia", trechos, 0)

        # Only documents meeting the threshold ground the answer (Req 2.2).
        relevant_sources = tuple(
            ref for ref in kb.sources if ref.score >= threshold
        )

        if generated is not None:
            # LLM generation grounded on the KB (Req 2.2), with one source
            # reference per cited document (Req 2.3). Scores come from the
            # retrieval step, matched by doc_id.
            text, cited = generated
            if is_no_answer(text) or not cited:
                # An answer the model could not ground in any document is not
                # delivered; same outcome as "no relevant documents" (Req 2.4).
                # A negative *with* citations counts here too: the passages were
                # retrieved but none answered the question.
                #
                # A sentinela é testada **antes** da falta de citação porque quem
                # responde "não encontrei" não cita nada — na ordem inversa toda
                # recusa legítima seria rotulada `sem_citacao` e o campo perderia
                # o serventia. O resultado para o usuário é o mesmo; muda o rótulo.
                motivo = "sentinela" if is_no_answer(text) else "sem_citacao"
                return AgentTurn(
                    _not_found(), nova_sessao, motivo, trechos, len(cited)
                )
            score_by_doc = {ref.doc_id: ref.score for ref in kb.sources}
            cited_sources = tuple(
                SourceRef(doc_id=doc_id, score=score_by_doc.get(doc_id, 0.0))
                for doc_id in cited
            )
            return AgentTurn(
                assemble_response(text, cited_sources), nova_sessao,
                "ok", trechos, len(cited),
            )

        content = _grounded_content(request, relevant_sources)
        return AgentTurn(
            assemble_response(content, relevant_sources), nova_sessao,
            "ok", trechos, len(relevant_sources),
        )


def _grounded_content(
    request: AgentRequest, sources: Sequence[SourceRef]
) -> str:
    """Placeholder grounding of the answer text on the retrieved documents.

    Actual LLM generation is wired in a later task; the answer must be derived
    exclusively from the retrieved documents (Req 2.2). Here the content simply
    references the grounding documents so the assembled response stays faithful
    to its sources.
    """
    doc_ids = ", ".join(ref.doc_id for ref in sources)
    return (
        f"Resposta fundamentada nos documentos recuperados ({doc_ids}) "
        f"para a requisicao da sessao {request.session_id}."
    )
