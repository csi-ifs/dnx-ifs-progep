"""AWS resource definitions for the Assistente PROGEP-IFS (Task 13.2).

Infrastructure-as-Code module for the ``Provisionador_IaC`` that declares **all**
AWS resources of the solution and provisions them through the single global AWS
provider configured in :mod:`src.infra.provider`. Because every resource is
created with that provider, all taggable resources inherit the ``Tag_APN`` from
the provider's ``default_tags`` (Req 8.1, 8.4, 9.4).

Resources covered (Req 9.1):

* **S3** — document/data-source bucket for the Knowledge Base.
* **OpenSearch Serverless** — the ``Vector_Store`` collection (plus its non-taggable
  security/access policies).
* **Bedrock Knowledge Base** — RAG index over the S3 data source.
* **DynamoDB** — ``Gestor_de_Sessao`` table with TTL.
* **Amazon Cognito** — user pool/client for API authentication.
* **API Gateway + Lambda** — the ``API_de_Integracao`` handler.
* **Amazon Connect** — the ``Roteador_de_Atendimento`` instance.
* **AWS End User Messaging (Social)** — the ``Conector_WhatsApp``.
* **AWS Secrets Manager** — credentials/secrets store.
* **CloudWatch** — log group for the ``Monitor_de_Consumo``.
* **CloudTrail** — audit trail (with its backing S3 bucket).
* **IAM roles** — least-privilege roles built via :mod:`src.domain.iam`.

Design constraints honoured here:

* The ``pulumi`` / ``pulumi_aws`` imports are **guarded** so this module imports
  cleanly even when those packages are absent. All resource construction is
  deferred to :func:`define_resources`, which raises a clear error when Pulumi is
  unavailable rather than failing at import time.
* The **pure, testable** parts — the required-resource catalog and presence
  validation (Req 9.3), the failure/rollback analysis including ``Tag_APN``
  application failure (Req 8.2, 9.6), and the least-privilege IAM policy builders
  (Req 10.2) — have no dependency on Pulumi and can be unit-tested anywhere.
* IAM roles are assembled from statements produced by
  :func:`src.domain.iam.build_statement`, guaranteeing least-privilege by
  construction (no broad wildcards, no admin) (Req 10.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from src.domain.acervo import GRUPO_ADMIN
from src.domain.config import APN_TAG, PREPARO_PREFIX, REMOVIDOS_PREFIX
from src.domain.iam import (
    IamPolicy,
    build_policy,
    build_statement,
    to_policy_document,
)
from src.infra.connect_flow import transfer_flow_content
from src.infra.provider import (
    TaggingExceptionRegistry,
    _require_pulumi,
    configure_provider,
)

# --- Guarded Pulumi imports -----------------------------------------------------
try:  # pragma: no cover - depends on the runtime environment
    import pulumi
    import pulumi_aws as aws

    _PULUMI_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the runtime environment
    pulumi = None  # type: ignore[assignment]
    aws = None  # type: ignore[assignment]
    _PULUMI_AVAILABLE = False

# pulumi-aws-native provides the S3 Vectors resources (VectorBucket/Index) that
# the classic provider does not expose yet. Guarded like the imports above.
try:  # pragma: no cover - depends on the runtime environment
    import pulumi_aws_native as aws_native
    from pulumi_aws_native import s3vectors as native_s3vectors

    _AWS_NATIVE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the runtime environment
    aws_native = None  # type: ignore[assignment]
    native_s3vectors = None  # type: ignore[assignment]
    _AWS_NATIVE_AVAILABLE = False


# --- Required-resource catalog (pure) -------------------------------------------


@dataclass(frozen=True)
class ResourceSpec:
    """Declarative description of a required AWS resource (Req 9.1, 9.3).

    Attributes:
        key: Stable logical identifier used across the IaC and in validation.
        service: The AWS service the resource belongs to.
        taggable: Whether the resource supports tags; non-taggable resources are
            recorded as tagging exceptions (Req 8.6).
        description: Human-readable purpose of the resource.
    """

    key: str
    service: str
    taggable: bool
    description: str


#: Canonical catalog of every resource the solution must define (Req 9.1).
REQUIRED_RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec("documents_bucket", "s3", True, "Bucket S3 fonte de documentos da KB"),
    ResourceSpec("staging_bucket", "s3", True, "Bucket de preparo e lixeira do painel de acervo"),
    ResourceSpec("painel_bucket", "s3", True, "Bucket privado da interface do painel"),
    ResourceSpec("painel_distribuicao", "cloudfront", True, "Distribuição CloudFront do painel"),
    ResourceSpec("vector_store", "s3vectors", True, "Vector_Store (S3 Vectors bucket + index)"),
    ResourceSpec("knowledge_base", "bedrock", True, "Base_de_Conhecimento (Bedrock KB)"),
    ResourceSpec("kb_data_source", "bedrock", False, "Data source S3 da Base_de_Conhecimento"),
    ResourceSpec("session_table", "dynamodb", True, "Tabela de sessões (Gestor_de_Sessao) com TTL"),
    ResourceSpec("cognito_user_pool", "cognito", True, "User pool do Amazon Cognito"),
    ResourceSpec("api_gateway", "apigateway", True, "API_de_Integracao (API Gateway)"),
    ResourceSpec("api_lambda", "lambda", True, "Handler Lambda da API_de_Integracao"),
    ResourceSpec("acervo_lambda", "lambda", True, "Handler Lambda do painel de acervo"),
    ResourceSpec("acervo_role", "iam", True, "Role IAM de privilégio mínimo do painel"),
    ResourceSpec("ingestor_lambda", "lambda", True, "Lambda do Ingestor_de_Documentos"),
    ResourceSpec("connect_instance", "connect", True, "Roteador_de_Atendimento (Amazon Connect)"),
    ResourceSpec("connect_queue", "connect", True, "Fila de atendimento humano da PROGEP"),
    ResourceSpec("connect_routing_profile", "connect", True, "Perfil de roteamento dos atendentes"),
    ResourceSpec("connect_transfer_flow", "connect", True, "Contact flow que só enfileira o transbordo do WhatsApp"),
    ResourceSpec("connect_chat_stream_topic", "sns", True, "Tópico SNS do streaming do chat do Connect"),
    ResourceSpec("connect_chat_stream_lambda", "lambda", True, "Lambda que leva a fala do atendente ao WhatsApp"),
    ResourceSpec("whatsapp_messaging", "socialmessaging", True, "Conector_WhatsApp (End User Messaging)"),
    ResourceSpec("whatsapp_secret", "secretsmanager", True, "Token da Graph API da Meta (diagnóstico manual)"),
    ResourceSpec("whatsapp_role", "iam", True, "Role IAM de privilégio mínimo das Lambdas do WhatsApp"),
    ResourceSpec("agentcore_ecr", "ecr", True, "Repositório ECR da imagem do Agente_IA (AgentCore Runtime)"),
    ResourceSpec("agentcore_role", "iam", True, "Role de execução do Bedrock AgentCore Runtime"),
    ResourceSpec("secrets", "secretsmanager", True, "Segredos/credenciais (Secrets Manager)"),
    ResourceSpec("monitor_log_group", "cloudwatch", True, "Log group do Monitor_de_Consumo"),
    ResourceSpec("bedrock_invocation_logs", "cloudwatch", True, "Log group das invocações do Bedrock (tokens)"),
    ResourceSpec("bedrock_logging_role", "iam", True, "Role que permite ao Bedrock gravar logs de invocação"),
    ResourceSpec("audit_trail", "cloudtrail", True, "Trilha de auditoria (CloudTrail)"),
    ResourceSpec("kb_role", "iam", True, "Role IAM de privilégio mínimo da Base_de_Conhecimento"),
    ResourceSpec("agent_role", "iam", True, "Role IAM de privilégio mínimo do Agente_IA"),
    ResourceSpec("api_role", "iam", True, "Role IAM de privilégio mínimo da API_de_Integracao"),
    ResourceSpec("ingestor_role", "iam", True, "Role IAM de privilégio mínimo do Ingestor"),
    ResourceSpec("monitor_role", "iam", True, "Role IAM de privilégio mínimo do Monitor_de_Consumo"),
    ResourceSpec("custos_table", "dynamodb", True, "Tabela do controle de custos (aba Custos)"),
    ResourceSpec("custos_role", "iam", True, "Role das Lambdas do controle de custos"),
    ResourceSpec("custos_logs_lambda", "lambda", True, "Custo em tempo real a partir dos logs"),
    ResourceSpec("custos_coleta_lambda", "lambda", True, "Coleta horária de custos (métricas e fatura)"),
)


def required_resource_keys() -> frozenset[str]:
    """Return the set of logical keys of all required resources (Req 9.1)."""
    return frozenset(spec.key for spec in REQUIRED_RESOURCE_SPECS)


def resource_spec(key: str) -> ResourceSpec:
    """Return the :class:`ResourceSpec` for ``key``.

    Raises:
        KeyError: If ``key`` is not part of the required-resource catalog.
    """
    for spec in REQUIRED_RESOURCE_SPECS:
        if spec.key == key:
            return spec
    raise KeyError(f"recurso desconhecido: {key!r}")


class MissingResourceError(ValueError):
    """Raised when a required resource is not defined in the IaC (Req 9.3).

    Attributes:
        missing: The tuple of required resource keys that are not defined.
    """

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing: tuple[str, ...] = tuple(missing)
        joined = ", ".join(self.missing)
        super().__init__(
            "provisionamento bloqueado: recurso(s) necessário(s) ausente(s) na "
            f"definição da IaC: {joined} (Req 9.3)"
        )


def validate_required_resources(defined_keys: Iterable[str]) -> None:
    """Ensure every required resource is defined before provisioning (Req 9.3).

    When a resource required by the solution is not present in ``defined_keys``,
    provisioning must be **blocked without creating or modifying any resource**
    and an error identifying the missing resource(s) must be raised. This helper
    is pure so it can be called (and unit-tested) before any Pulumi resource is
    constructed.

    Args:
        defined_keys: The logical keys of the resources actually defined by the
            IaC program.

    Raises:
        MissingResourceError: If one or more required resources are missing; the
            exception's ``missing`` attribute identifies them.
    """
    defined = set(defined_keys)
    missing = [spec.key for spec in REQUIRED_RESOURCE_SPECS if spec.key not in defined]
    if missing:
        raise MissingResourceError(missing)


# --- Failure / rollback analysis (pure) -----------------------------------------

#: Outcome status: the resource was provisioned successfully.
PROVISIONED: str = "provisioned"
#: Outcome status: provisioning the resource failed (Req 9.6).
FAILED: str = "failed"
#: Outcome status: applying the ``Tag_APN`` to the resource failed (Req 8.2).
TAG_FAILED: str = "tag_failed"

#: Failure kinds reported by :func:`analyze_provisioning`.
FAILURE_PROVISIONING: str = "provisioning"
FAILURE_TAGGING: str = "tagging"


@dataclass(frozen=True)
class ProvisionOutcome:
    """Result of attempting to provision a single resource.

    Attributes:
        resource_id: Identification of the resource.
        status: One of :data:`PROVISIONED`, :data:`FAILED` or :data:`TAG_FAILED`.
        detail: Optional human-readable detail (e.g. the underlying error).
    """

    resource_id: str
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class ProvisioningReport:
    """Outcome of analysing a sequence of provisioning attempts.

    Attributes:
        ok: ``True`` when every resource was provisioned successfully.
        failed_resource: The identifier of the resource that failed, if any.
        failure_kind: :data:`FAILURE_PROVISIONING` or :data:`FAILURE_TAGGING`.
        preserved: Resources provisioned successfully before the failure; these
            are kept (Req 9.6).
        rolled_back: Resources that must be rolled back — the affected resource on
            a ``Tag_APN`` application failure (Req 8.2).
        message: Human-readable summary identifying the failing resource.
    """

    ok: bool
    failed_resource: str | None
    failure_kind: str | None
    preserved: tuple[str, ...]
    rolled_back: tuple[str, ...]
    message: str


class ProvisioningError(RuntimeError):
    """Raised to interrupt provisioning, identifying the failing resource."""

    def __init__(self, report: ProvisioningReport) -> None:
        self.report = report
        super().__init__(report.message)


def analyze_provisioning(outcomes: Iterable[ProvisionOutcome]) -> ProvisioningReport:
    """Identify the failing resource and the rollback/preservation plan.

    Processes the provisioning attempts **in order** and stops at the first one
    that is not :data:`PROVISIONED`, implementing two requirements:

    * **Req 9.6** — a provisioning failure interrupts the operation, identifies
      the resource that failed and preserves the resources already provisioned
      successfully up to the point of failure.
    * **Req 8.2** — a failure to apply the ``Tag_APN`` interrupts the operation,
      rolls back the affected resource (it is *not* persisted) and reports which
      resource failed. Resources provisioned before it are still preserved.

    Args:
        outcomes: The per-resource provisioning outcomes, in provisioning order.

    Returns:
        A :class:`ProvisioningReport` describing success or the failure plan.
    """
    preserved: list[str] = []
    for outcome in outcomes:
        if outcome.status == PROVISIONED:
            preserved.append(outcome.resource_id)
            continue

        if outcome.status == TAG_FAILED:
            detail = f": {outcome.detail}" if outcome.detail else ""
            return ProvisioningReport(
                ok=False,
                failed_resource=outcome.resource_id,
                failure_kind=FAILURE_TAGGING,
                preserved=tuple(preserved),
                rolled_back=(outcome.resource_id,),
                message=(
                    "provisionamento interrompido: falha ao aplicar Tag_APN no "
                    f"recurso {outcome.resource_id!r}; rollback do recurso afetado "
                    f"(Req 8.2){detail}"
                ),
            )

        # Any non-provisioned, non-tag status is a provisioning failure (Req 9.6).
        detail = f": {outcome.detail}" if outcome.detail else ""
        return ProvisioningReport(
            ok=False,
            failed_resource=outcome.resource_id,
            failure_kind=FAILURE_PROVISIONING,
            preserved=tuple(preserved),
            rolled_back=(),
            message=(
                "provisionamento interrompido: falha ao provisionar o recurso "
                f"{outcome.resource_id!r}; recursos já provisionados preservados "
                f"(Req 9.6){detail}"
            ),
        )

    return ProvisioningReport(
        ok=True,
        failed_resource=None,
        failure_kind=None,
        preserved=tuple(preserved),
        rolled_back=(),
        message="todos os recursos provisionados com sucesso",
    )


def raise_on_provisioning_failure(
    outcomes: Iterable[ProvisionOutcome],
) -> ProvisioningReport:
    """Analyse outcomes and raise :class:`ProvisioningError` on any failure.

    Returns the successful report when everything provisioned, otherwise raises
    with the failing-resource identification (Req 8.2, 9.6).
    """
    report = analyze_provisioning(outcomes)
    if not report.ok:
        raise ProvisioningError(report)
    return report


# --- Least-privilege IAM policies (pure, built via src.domain.iam) --------------


@dataclass(frozen=True)
class ArnContext:
    """Parameters used to build scoped resource ARNs for IAM policies.

    Defaults use ``"*"`` only in the *partition/region/account* ARN segments,
    never as a whole-resource wildcard, so the resulting statements remain
    least-privilege (they always target named resources) and pass the Property 21
    checks in :mod:`src.domain.iam`.
    """

    partition: str = "aws"
    region: str = "*"
    account_id: str = "*"
    documents_bucket: str = "progep-ifs-documents"
    staging_bucket: str = "progep-ifs-staging"
    painel_bucket: str = "progep-ifs-painel"
    session_table: str = "progep-ifs-sessions"
    #: Tabela do controle de custos (aba "Custos" do painel).
    custos_table: str = "progep-ifs-custos"
    knowledge_base_id: str = "*"
    #: LLM used by the Agente_IA (chamadas anotadas com o User_Agent_APN).
    model_id: str = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    #: Embedding model used by the Knowledge Base (Vector_Store).
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    #: S3 Vectors bucket/index backing the Vector_Store.
    vector_bucket: str = "progep-ifs-vectors"
    vector_index: str = "progep-ifs-index"
    #: Índice da base paralela com divisão hierárquica (em avaliação, 11/2026).
    vector_index_hier: str = "progep-ifs-index-hier"
    secret_name: str = "progep-ifs/*"
    #: ARN do AgentCore Runtime que os handlers invocam (arquitetura única).
    #: Default escopado por nome (passa Property 21); o ARN concreto é injetado
    #: no deploy via configuração do Pulumi.
    agent_runtime_arn: str = "arn:aws:bedrock-agentcore:*:*:runtime/progep_ifs_agent-*"
    log_group: str = "/progep-ifs/*"
    lambda_log_group: str = "/aws/lambda/progep-ifs-*"
    agent_function: str = "progep-ifs-agent"
    connect_instance_id: str = "*"


def _bucket_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:s3:::{ctx.documents_bucket}"


def _staging_bucket_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:s3:::{ctx.staging_bucket}"


def _table_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:dynamodb:{ctx.region}:{ctx.account_id}:table/{ctx.session_table}"


def _kb_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account_id}:knowledge-base/{ctx.knowledge_base_id}"


def _model_arn(ctx: ArnContext) -> str:
    """ARN do LLM: inference profile (modelos ``global.``) ou foundation model."""
    if ctx.model_id.split(".", 1)[0] in ("global", "us", "eu", "apac"):
        return (
            f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account_id}:"
            f"inference-profile/{ctx.model_id}"
        )
    return f"arn:{ctx.partition}:bedrock:{ctx.region}::foundation-model/{ctx.model_id}"


def _embedding_model_arn(ctx: ArnContext, *, region: str | None = None) -> str:
    """ARN do modelo de embedding da KB; região concreta quando fornecida."""
    r = region if region is not None else ctx.region
    return f"arn:{ctx.partition}:bedrock:{r}::foundation-model/{ctx.embedding_model_id}"


def _vector_bucket_arn(ctx: ArnContext) -> str:
    return (
        f"arn:{ctx.partition}:s3vectors:{ctx.region}:{ctx.account_id}:"
        f"bucket/{ctx.vector_bucket}"
    )


def _vector_index_arn(ctx: ArnContext) -> str:
    return f"{_vector_bucket_arn(ctx)}/index/{ctx.vector_index}"


def _vector_index_hier_arn(ctx: ArnContext) -> str:
    return f"{_vector_bucket_arn(ctx)}/index/{ctx.vector_index_hier}"


def _secret_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:secretsmanager:{ctx.region}:{ctx.account_id}:secret:{ctx.secret_name}"


def _log_group_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:logs:{ctx.region}:{ctx.account_id}:log-group:{ctx.log_group}:*"


def _lambda_log_group_arn(ctx: ArnContext) -> str:
    return (
        f"arn:{ctx.partition}:logs:{ctx.region}:{ctx.account_id}:"
        f"log-group:{ctx.lambda_log_group}:*"
    )


def _connect_instance_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:connect:{ctx.region}:{ctx.account_id}:instance/{ctx.connect_instance_id}"


def build_component_policies(ctx: ArnContext | None = None) -> dict[str, IamPolicy]:
    """Build the least-privilege IAM policy for each component (Req 10.2).

    Each policy is assembled from :func:`src.domain.iam.build_statement`, which
    rejects broad wildcards and admin grants at construction time, so every
    returned policy is guaranteed least-privilege (Property 21). Actions are
    scoped to exactly what the component needs and resources target named ARNs.

    Args:
        ctx: ARN parameters; defaults to :class:`ArnContext` with sensible names.

    Returns:
        A mapping of component key (matching an IAM ``ResourceSpec``) to its
        validated :class:`~src.domain.iam.IamPolicy`.
    """
    c = ctx or ArnContext()

    log_stmt = build_statement(
        ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        [_log_group_arn(c), _lambda_log_group_arn(c)],
        sid="CloudWatchLogs",
    )

    # Ingestor_de_Documentos: read S3 documents and drive KB ingestion (Req 1.1, 1.7).
    ingestor = build_policy(
        [
            build_statement(
                ["s3:GetObject", "s3:ListBucket"],
                [_bucket_arn(c), f"{_bucket_arn(c)}/*"],
                sid="ReadDocuments",
            ),
            build_statement(
                ["bedrock:StartIngestionJob", "bedrock:GetIngestionJob",
                 "bedrock:ListIngestionJobs"],
                _kb_arn(c),
                sid="KnowledgeBaseIngestion",
            ),
            log_stmt,
        ]
    )

    # Base_de_Conhecimento: role assumida pelo Bedrock para ler o S3, gravar no
    # Vector_Store (S3 Vectors) e gerar embeddings (Req 1.1, 10.2).
    kb = build_policy(
        [
            build_statement(
                ["s3:GetObject", "s3:ListBucket"],
                [_bucket_arn(c), f"{_bucket_arn(c)}/*"],
                sid="ReadDocuments",
            ),
            build_statement(
                [
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                [_vector_bucket_arn(c), _vector_index_arn(c), _vector_index_hier_arn(c)],
                sid="VectorStoreAccess",
            ),
            build_statement(
                ["bedrock:InvokeModel"],
                _embedding_model_arn(c),
                sid="InvokeEmbeddingModel",
            ),
        ]
    )

    # Agente_IA: query the KB, invoke the model and read secrets (Req 2.1, 8.3, 10.1).
    llm_resources = [_model_arn(c)]
    # Statements adicionais exigidos quando o LLM é um inference profile.
    inference_profile_stmts: list = []
    if "/inference-profile/" in _model_arn(c) or ":inference-profile/" in _model_arn(c):
        # Perfis de inferência roteiam a chamada para o foundation model em uma
        # das regiões cobertas pelo perfil; a permissão precisa cobrir o ARN do
        # perfil e o ARN do foundation model nessas regiões. Para perfis
        # ``global.`` o roteamento é cross-region, então a região do
        # foundation-model é ``*`` (recurso nomeado — não é curinga amplo de
        # recurso, Property 21). Perfis regionais (``us``/``eu``/``apac``)
        # também roteiam entre as regiões do grupo, cobertas por ``*``.
        base_model_id = c.model_id.split(".", 1)[1]
        llm_resources.append(
            f"arn:{c.partition}:bedrock:*::foundation-model/{base_model_id}"
        )
        # RetrieveAndGenerate/InvokeModel com inference profile resolvem o perfil
        # via GetInferenceProfile antes de rotear a chamada (Req 2.1, 8.3).
        inference_profile_stmts.append(
            build_statement(
                ["bedrock:GetInferenceProfile"],
                _model_arn(c),
                sid="GetInferenceProfile",
            )
        )
    agent = build_policy(
        [
            build_statement(
                ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                _kb_arn(c),
                sid="KnowledgeBaseQuery",
            ),
            build_statement(
                ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                llm_resources,
                sid="InvokeModel",
            ),
            *inference_profile_stmts,
            build_statement(
                ["secretsmanager:GetSecretValue"],
                _secret_arn(c),
                sid="ReadSecrets",
            ),
            build_statement(
                ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:DeleteItem", "dynamodb:Query"],
                _table_arn(c),
                sid="SessionState",
            ),
            log_stmt,
        ]
    )

    # Arquitetura única: API e WhatsApp apenas **invocam o AgentCore Runtime**
    # (não consultam mais KB/LLM diretamente — isso vive na role de execução do
    # runtime). Permissão escopada ao ARN do runtime e seus endpoints.
    invoke_runtime_stmt = build_statement(
        ["bedrock-agentcore:InvokeAgentRuntime"],
        [c.agent_runtime_arn, f"{c.agent_runtime_arn}/*"],
        sid="InvokeAgentRuntime",
    )

    # API_de_Integracao: autentica (Cognito authorizer) e invoca o runtime.
    api = build_policy(
        [
            invoke_runtime_stmt,
            build_statement(
                ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:Query"],
                _table_arn(c),
                sid="SessionState",
            ),
            log_stmt,
        ]
    )

    # Lambdas do WhatsApp (entrada pelo End User Messaging, encerramento e chat
    # do Connect). A role ainda se chama `progep-ifs-whatsapp-webhook`, do
    # webhook que veio antes; renomeá-la recriaria a role e as três Lambdas.
    whatsapp = build_policy(
        [
            invoke_runtime_stmt,
            # Ciclo de vida da conversa: saudação na abertura, registro da
            # atividade e varredura das paradas para o encerramento. ``Scan``
            # é da varredura — a tabela guarda uma linha por conversa ativa.
            # ``PutItem`` grava o índice reverso contato do Connect → conversa,
            # usado para levar a fala do atendente ao telefone certo.
            build_statement(
                ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:Scan"],
                _table_arn(c),
                sid="ConversationLifecycle",
            ),
            # Transbordo humano: abre o chat do servidor no Connect e liga o
            # streaming das mensagens para o SNS. A troca de mensagens em si
            # (Participant API) é autorizada por token, não por IAM.
            build_statement(
                ["connect:StartChatContact"],
                f"{_connect_instance_arn(c)}/contact-flow/*",
                sid="AbrirAtendimentoHumano",
            ),
            build_statement(
                ["connect:StartContactStreaming"],
                f"{_connect_instance_arn(c)}/contact/*",
                sid="EspelharChatDoAtendimento",
            ),
            # Envio pelo End User Messaging Social: resposta, "digitando" e
            # encerramento saem por aqui (§6.4 da documentação). Escopado a
            # números de telefone vinculados, não à WABA — enviar é ação do número.
            build_statement(
                ["social-messaging:SendWhatsAppMessage"],
                f"arn:{c.partition}:social-messaging:{c.region}:{c.account_id}"
                ":phone-number-id/*",
                sid="SendWhatsAppViaEndUserMessaging",
            ),
            log_stmt,
        ]
    )

    # Monitor_de_Consumo: emit metrics/records to CloudWatch Logs (Req 7.1-7.5).
    monitor = build_policy([log_stmt])

    # Painel de acervo: é a única role que **escreve e apaga** no bucket de
    # documentos, então é a que mais merece escopo apertado.
    #
    # `s3:DeleteObject` no bucket de documentos é inevitável (remover é o que o
    # cliente pediu), e é justamente por isso que a chave vinda da tela passa
    # por `acervo.validar_chave_do_acervo` antes de qualquer chamada: a role
    # alcança o bucket inteiro, então quem restringe o alvo é o código.
    acervo = build_policy(
        [
            build_statement(
                ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                [_bucket_arn(c), f"{_bucket_arn(c)}/*"],
                sid="AcervoDocumentos",
            ),
            build_statement(
                ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                [_staging_bucket_arn(c), f"{_staging_bucket_arn(c)}/*"],
                sid="PreparoELixeira",
            ),
            # `StartIngestionJob` **não** é para indexar — quem indexa é o
            # Ingestor, disparado pela cópia para o bucket. Está aqui porque
            # `DeleteKnowledgeBaseDocuments` a exige por dentro: a API se chama
            # "delete documents", mas o IAM cobra a permissão do job de
            # sincronização, e sem ela a remoção falha com AccessDenied
            # apontando uma ação que o código nunca chama.
            build_statement(
                [
                    "bedrock:ListKnowledgeBaseDocuments",
                    "bedrock:GetKnowledgeBaseDocuments",
                    "bedrock:DeleteKnowledgeBaseDocuments",
                    "bedrock:StartIngestionJob",
                ],
                _kb_arn(c),
                sid="AcervoNaBaseDeConhecimento",
            ),
            # Aba "Custos": só leitura da tabela que as Lambdas de custos gravam.
            build_statement(
                ["dynamodb:Query", "dynamodb:GetItem"],
                f"arn:{c.partition}:dynamodb:{c.region}:{c.account_id}:table/{c.custos_table}",
                sid="LerCustos",
            ),
            log_stmt,
        ]
    )

    return {
        "ingestor_role": ingestor,
        "kb_role": kb,
        "agent_role": agent,
        "api_role": api,
        "whatsapp_role": whatsapp,
        "monitor_role": monitor,
        "acervo_role": acervo,
    }


def component_policy_documents(ctx: ArnContext | None = None) -> dict[str, dict]:
    """Render every component policy to an AWS IAM policy document ``dict``.

    Convenience wrapper over :func:`build_component_policies` +
    :func:`src.domain.iam.to_policy_document`, used when attaching inline policies
    to the Pulumi IAM roles.
    """
    return {key: to_policy_document(policy) for key, policy in build_component_policies(ctx).items()}


#: Ações que a AWS **exige** com ``Resource: "*"`` e que, por isso, não passam
#: pelo builder estrito de privilégio mínimo (Property 21). São exceções
#: inerentes ao serviço, não excesso de privilégio evitável:
#: ``ecr:GetAuthorizationToken`` (token de registry não é resource-scoped) e as
#: ações de X-Ray/telemetria usadas pela observabilidade do AgentCore.
AGENTCORE_WILDCARD_ACTION_EXCEPTIONS: tuple[str, ...] = (
    "ecr:GetAuthorizationToken",
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
    "xray:GetSamplingRules",
    "xray:GetSamplingTargets",
)

#: Ciclo de vida do repositório da imagem do agente: só imagens **sem tag** e
#: com mais de 7 dias (as versões anteriores ao `latest`), o que deixa uma
#: semana de versões para voltar atrás.
ECR_LIFECYCLE_POLICY: dict = {
    "rules": [
        {
            "rulePriority": 1,
            "description": "Expira versões antigas (sem tag) com mais de 7 dias",
            "selection": {
                "tagStatus": "untagged",
                "countType": "sinceImagePushed",
                "countUnit": "days",
                "countNumber": 7,
            },
            "action": {"type": "expire"},
        }
    ]
}

#: Leituras do controle de custos que a AWS só aceita com ``Resource: "*"``:
#: métricas do CloudWatch não têm ARN, e o Cost Explorer não tem tipo de recurso
#: para ``GetCostAndUsage``. As três são **somente leitura**.
CUSTOS_WILDCARD_ACTION_EXCEPTIONS: tuple[str, ...] = (
    "cloudwatch:GetMetricData",
    "cloudwatch:ListMetrics",
    "ce:GetCostAndUsage",
)


def _custos_table_arn(ctx: ArnContext) -> str:
    return f"arn:{ctx.partition}:dynamodb:{ctx.region}:{ctx.account_id}:table/{ctx.custos_table}"


def custos_policy_document(ctx: ArnContext, *, ecr_repository_arn: str) -> dict:
    """Role das duas Lambdas do controle de custos.

    Escreve só na própria tabela; o resto é leitura, escopada ao que é da
    PROGEP sempre que a AWS permite (as funções ``progep-ifs-*``, o repositório
    da imagem, o índice de vetores). As exceções com ``"*"`` estão em
    :data:`CUSTOS_WILDCARD_ACTION_EXCEPTIONS`.
    """
    c = ctx
    escopadas = build_policy([
        build_statement(
            ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"],
            _custos_table_arn(c),
            sid="TabelaDeCustos",
        ),
        build_statement(
            ["lambda:GetFunctionConfiguration"],
            f"arn:{c.partition}:lambda:{c.region}:{c.account_id}:function:progep-ifs-*",
            sid="MemoriaDasLambdas",
        ),
        build_statement(
            ["logs:DescribeLogGroups"],
            f"arn:{c.partition}:logs:{c.region}:{c.account_id}:log-group:*",
            sid="TamanhoDosLogs",
        ),
        build_statement(["ecr:DescribeImages"], ecr_repository_arn, sid="TamanhoDaImagem"),
        build_statement(
            ["s3vectors:ListVectors"],
            [_vector_bucket_arn(c), _vector_index_arn(c)],
            sid="TamanhoDoIndice",
        ),
        build_statement(
            ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            _lambda_log_group_arn(c),
            sid="CloudWatchLogs",
        ),
    ])
    documento = to_policy_document(escopadas)
    documento["Statement"].append({
        "Sid": "LeiturasSemRecurso",
        "Effect": "Allow",
        "Action": list(CUSTOS_WILDCARD_ACTION_EXCEPTIONS),
        "Resource": "*",
    })
    return documento


def agentcore_execution_policy_document(
    ctx: ArnContext, *, ecr_repository_arn: str
) -> dict:
    """Monta o policy document da role de execução do AgentCore Runtime.

    Ao contrário das roles dos componentes (montadas por
    :func:`build_component_policies`, garantidamente sem curinga de recurso), esta
    role de **execução do runtime gerenciado** precisa de duas ações que a AWS só
    aceita com ``Resource: "*"`` — ``ecr:GetAuthorizationToken`` (autenticação no
    registry) e as ações de X-Ray da observabilidade
    (:data:`AGENTCORE_WILDCARD_ACTION_EXCEPTIONS`). Essas são exceções inerentes
    ao serviço; todas as demais permissões continuam escopadas a recursos nomeados
    (repo ECR, KB, modelo, log groups).
    """
    c = ctx
    llm_resources = [_model_arn(c)]
    profile_stmt: list[dict] = []
    if "inference-profile/" in _model_arn(c):
        base_model_id = c.model_id.split(".", 1)[1]
        llm_resources.append(
            f"arn:{c.partition}:bedrock:*::foundation-model/{base_model_id}"
        )
        profile_stmt.append({
            "Sid": "GetInferenceProfile",
            "Effect": "Allow",
            "Action": "bedrock:GetInferenceProfile",
            "Resource": _model_arn(c),
        })
    log_arn = (
        f"arn:{c.partition}:logs:{c.region}:{c.account_id}:log-group:"
        f"/aws/bedrock-agentcore/*"
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EcrPullImage",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                ],
                "Resource": ecr_repository_arn,
            },
            {
                # Exceção inerente: token de registry não é resource-scoped.
                "Sid": "EcrAuthToken",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
            {
                "Sid": "Observability",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                "Resource": log_arn,
            },
            {
                # Exceção inerente: X-Ray/telemetria da observabilidade do
                # AgentCore não é resource-scoped.
                "Sid": "XRayTelemetry",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "cloudwatch:PutMetricData",
                ],
                "Resource": "*",
            },
            {
                "Sid": "KnowledgeBaseQuery",
                "Effect": "Allow",
                "Action": ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                "Resource": _kb_arn(c),
            },
            {
                "Sid": "InvokeModel",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": llm_resources,
            },
            {
                # O runtime guarda o vínculo "conversa do usuário → sessionId do
                # Bedrock" para entender follow-ups. Só leitura e escrita do
                # item: nada de varrer a tabela.
                "Sid": "ConversationSessions",
                "Effect": "Allow",
                # UpdateItem (não PutItem): o runtime grava só a sessão do
                # Bedrock, enquanto a Lambda do WhatsApp grava a atividade no
                # mesmo item. Com PutItem um sobrescreveria o outro.
                "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
                "Resource": _table_arn(c),
            },
            *profile_stmt,
        ],
    }


#: Trust-policy assume-role document for AWS Lambda execution roles.
LAMBDA_ASSUME_ROLE_POLICY: dict = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

#: Trust-policy assume-role document for the Bedrock Knowledge Base role.
BEDROCK_ASSUME_ROLE_POLICY: dict = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

#: Trust-policy assume-role document for the Bedrock AgentCore Runtime role.
AGENTCORE_ASSUME_ROLE_POLICY: dict = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


# --- Pulumi resource construction (guarded) -------------------------------------


@dataclass
class SolutionResources:
    """Container for the provisioned Pulumi resources, keyed by logical name."""

    provider: Any
    resources: dict[str, Any] = field(default_factory=dict)
    tagging_exceptions: TaggingExceptionRegistry = field(default_factory=TaggingExceptionRegistry)

    def keys(self) -> frozenset[str]:
        """Logical keys of the resources actually created."""
        return frozenset(self.resources)


def defined_resource_keys() -> frozenset[str]:
    """Return the logical keys that :func:`define_resources` provisions.

    Mirrors the required-resource catalog: :func:`define_resources` creates a
    resource for every entry in :data:`REQUIRED_RESOURCE_SPECS`, so this equals
    :func:`required_resource_keys`. Kept as a distinct function so the presence
    check (Req 9.3) validates the program's *actual* definitions.
    """
    return required_resource_keys()


def define_resources(
    *,
    provider: Any | None = None,
    ctx: ArnContext | None = None,
    name_prefix: str = "progep-ifs",
    region: str = "us-east-1",
    agent_runtime_arn: str | None = None,
    cors_allow_origin: str = "*",
    custos_equipe_tecnica: str = "",
    custos_meta_isento: str = "false",
    trilha_propria: bool = False,
    base_ativa: str = "hierarquica",
) -> SolutionResources:
    """Declare all AWS resources of the solution via the shared provider.

    Every resource is created with ``opts=pulumi.ResourceOptions(provider=...)``
    so it inherits the single global ``Tag_APN`` from the provider's
    ``default_tags`` (Req 8.1, 8.4, 9.4). Before creating anything, the required
    resources are validated for presence (Req 9.3); resources that do not support
    tags are recorded as tagging exceptions (Req 8.6). IAM roles receive the
    least-privilege policies built via :mod:`src.domain.iam` (Req 10.2).

    Args:
        provider: An existing ``pulumi_aws.Provider``; one is created via
            :func:`src.infra.provider.configure_provider` when omitted.
        ctx: ARN parameters for the least-privilege IAM policies.
        name_prefix: Prefix applied to physical resource names.

    Returns:
        A :class:`SolutionResources` with the created resources and any tagging
        exceptions.

    Raises:
        RuntimeError: If Pulumi is not available in the environment.
        MissingResourceError: If the program would not define a required resource.
    """
    _require_pulumi()

    # Req 9.3: block before creating/modifying anything if a required resource is
    # not part of the program's definitions.
    validate_required_resources(defined_resource_keys())

    prov = provider if provider is not None else configure_provider(region=region)
    opts = pulumi.ResourceOptions(provider=prov)
    _ctx_overrides: dict[str, Any] = {"region": region}
    if agent_runtime_arn:
        _ctx_overrides["agent_runtime_arn"] = agent_runtime_arn
    c = ctx or ArnContext(**_ctx_overrides)
    out = SolutionResources(provider=prov)
    r = out.resources

    # -- S3 document bucket ------------------------------------------------------
    # Nome físico exato: as policies IAM de privilégio mínimo referenciam o ARN
    # nominal do bucket (Property 21), então o nome não pode ser randomizado.
    r["documents_bucket"] = aws.s3.BucketV2(
        f"{name_prefix}-documents",
        bucket=c.documents_bucket,
        opts=opts,
    )
    aws.s3.BucketPublicAccessBlock(
        f"{name_prefix}-documents-pab",
        bucket=r["documents_bucket"].id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
        opts=opts,
    )
    # S3 public access block does not support tags (Req 8.6).
    out.tagging_exceptions.register(
        f"{name_prefix}-documents-pab",
        "aws.s3.BucketPublicAccessBlock não suporta tags",
    )

    # -- S3 staging bucket (preparo e lixeira do painel de acervo) ---------------
    # **Bucket separado, e não um prefixo do bucket de documentos**, porque
    # `inclusionPrefixes` de um data source S3 aceita no máximo um prefixo
    # (limite da API) e as origens já são duas — não havia como declarar "varra
    # site/ e cliente/, ignore preparo/". Sem essa declaração, o data source
    # varre o bucket inteiro e indexaria o que ninguém confirmou.
    #
    # Guarda `preparo/` (aguardando confirmação) e `removidos/` (exclusão
    # reversível). Nenhum data source o varre e nenhuma notificação o observa:
    # é isso que faz a revisão ser real em vez de um aviso na tela.
    r["staging_bucket"] = aws.s3.BucketV2(
        f"{name_prefix}-staging",
        bucket=c.staging_bucket,
        opts=opts,
    )
    aws.s3.BucketPublicAccessBlock(
        f"{name_prefix}-staging-pab",
        bucket=r["staging_bucket"].id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
        opts=opts,
    )
    # Envio abandonado não fica para sempre, e exclusão continua reversível por
    # um prazo: 30 dias é folgado o bastante para alguém perceber que apagou o
    # documento errado, e curto o bastante para o bucket não virar depósito.
    aws.s3.BucketLifecycleConfigurationV2(
        f"{name_prefix}-staging-lifecycle",
        bucket=r["staging_bucket"].id,
        rules=[
            aws.s3.BucketLifecycleConfigurationV2RuleArgs(
                id=nome,
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationV2RuleFilterArgs(prefix=pref),
                expiration=aws.s3.BucketLifecycleConfigurationV2RuleExpirationArgs(
                    days=dias
                ),
            )
            for nome, pref, dias in (
                ("preparo-abandonado", PREPARO_PREFIX, 30),
                ("lixeira", REMOVIDOS_PREFIX, 30),
            )
        ],
        opts=opts,
    )
    for rid, motivo in (
        (f"{name_prefix}-staging-pab", "aws.s3.BucketPublicAccessBlock não suporta tags"),
        (
            f"{name_prefix}-staging-lifecycle",
            "aws.s3.BucketLifecycleConfigurationV2 não suporta tags",
        ),
    ):
        out.tagging_exceptions.register(rid, motivo)

    # -- Painel de acervo: S3 privado atrás do CloudFront ------------------------
    #
    #   navegador ──> CloudFront ──OAC──> S3 (privado)
    #
    # **O bucket não é público.** O acesso é por Origin Access Control: o
    # CloudFront assina as requisições e a policy do bucket recusa qualquer
    # outra origem. Bucket estático público é o erro clássico desta arquitetura,
    # e aqui não traria ganho — ninguém precisa alcançar o S3 direto.
    r["painel_bucket"] = aws.s3.BucketV2(
        f"{name_prefix}-painel",
        bucket=c.painel_bucket,
        opts=opts,
    )
    aws.s3.BucketPublicAccessBlock(
        f"{name_prefix}-painel-pab",
        bucket=r["painel_bucket"].id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
        opts=opts,
    )
    painel_oac = aws.cloudfront.OriginAccessControl(
        f"{name_prefix}-painel-oac",
        description="Acesso do CloudFront ao bucket do painel de acervo",
        origin_access_control_origin_type="s3",
        signing_behavior="always",
        signing_protocol="sigv4",
        opts=opts,
    )
    r["painel_distribuicao"] = aws.cloudfront.Distribution(
        f"{name_prefix}-painel-cf",
        comment="Painel de acervo do Assistente PROGEP-IFS",
        enabled=True,
        default_root_object="index.html",
        origins=[
            aws.cloudfront.DistributionOriginArgs(
                origin_id="painel-s3",
                domain_name=r["painel_bucket"].bucket_regional_domain_name,
                origin_access_control_id=painel_oac.id,
            )
        ],
        default_cache_behavior=aws.cloudfront.DistributionDefaultCacheBehaviorArgs(
            target_origin_id="painel-s3",
            viewer_protocol_policy="redirect-to-https",
            # A SPA só lê: o envio de arquivo vai direto ao S3 por URL assinada
            # e a API é outro host. Restringir os métodos reduz superfície sem
            # custo funcional.
            allowed_methods=["GET", "HEAD", "OPTIONS"],
            cached_methods=["GET", "HEAD"],
            compress=True,
            # CachingOptimized, política gerenciada pela AWS.
            cache_policy_id="658327ea-f89d-4fab-a63d-7e88639e58f6",
        ),
        # 404 vira index.html com HTTP 200: é uma SPA, qualquer caminho é rota
        # do app e não arquivo. Devolver 404 real quebraria o acesso direto a
        # uma URL interna.
        custom_error_responses=[
            aws.cloudfront.DistributionCustomErrorResponseArgs(
                error_code=codigo,
                response_code=200,
                response_page_path="/index.html",
                error_caching_min_ttl=300,
            )
            for codigo in (403, 404)
        ],
        restrictions=aws.cloudfront.DistributionRestrictionsArgs(
            geo_restriction=aws.cloudfront.DistributionRestrictionsGeoRestrictionArgs(
                restriction_type="none"
            )
        ),
        viewer_certificate=aws.cloudfront.DistributionViewerCertificateArgs(
            cloudfront_default_certificate=True
        ),
        # PriceClass_100 atende o Brasil com latência aceitável e custa menos
        # que a classe global; são duas telas de uso interno.
        price_class="PriceClass_100",
        opts=opts,
    )
    # A policy é escrita **depois** da distribuição porque precisa do ARN dela:
    # a condição `AWS:SourceArn` é o que impede outra distribuição, de outra
    # conta, de usar este bucket como origem.
    aws.s3.BucketPolicy(
        f"{name_prefix}-painel-policy",
        bucket=r["painel_bucket"].id,
        policy=pulumi.Output.all(
            r["painel_bucket"].arn, r["painel_distribuicao"].arn
        ).apply(
            lambda args: pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "SomenteCloudFront",
                            "Effect": "Allow",
                            "Principal": {"Service": "cloudfront.amazonaws.com"},
                            "Action": "s3:GetObject",
                            "Resource": f"{args[0]}/*",
                            "Condition": {"StringEquals": {"AWS:SourceArn": args[1]}},
                        }
                    ],
                }
            )
        ),
        opts=opts,
    )
    # CORS do bucket de preparo. **Sem isto o envio pelo painel não funciona**: o
    # navegador manda o arquivo direto ao S3 pela URL pré-assinada (a Lambda tem
    # teto de 6 MB de payload e o acervo aceita 50 MB), e uma requisição
    # cross-origin ao S3 sem CORS é bloqueada antes de sair — a tela mostra
    # "Failed to fetch", sem status nem corpo que expliquem.
    #
    # Fica aqui, depois da distribuição, porque a origem permitida é o domínio
    # dela. `localhost` acompanha para o `npm run dev` funcionar; permitir a
    # origem não autoriza nada por si, já que o envio ainda exige uma política
    # assinada que só um administrador autenticado obtém.
    aws.s3.BucketCorsConfigurationV2(
        f"{name_prefix}-staging-cors",
        bucket=r["staging_bucket"].id,
        cors_rules=[
            aws.s3.BucketCorsConfigurationV2CorsRuleArgs(
                allowed_methods=["POST"],
                allowed_origins=[
                    r["painel_distribuicao"].domain_name.apply(lambda d: f"https://{d}"),
                    "http://localhost:5173",
                ],
                allowed_headers=["*"],
                max_age_seconds=3000,
            )
        ],
        opts=opts,
    )
    for rid, motivo in (
        (f"{name_prefix}-painel-pab", "aws.s3.BucketPublicAccessBlock não suporta tags"),
        (
            f"{name_prefix}-painel-oac",
            "aws.cloudfront.OriginAccessControl não suporta tags",
        ),
        (f"{name_prefix}-painel-policy", "aws.s3.BucketPolicy não suporta tags"),
        (
            f"{name_prefix}-staging-cors",
            "aws.s3.BucketCorsConfigurationV2 não suporta tags",
        ),
    ):
        out.tagging_exceptions.register(rid, motivo)

    # -- S3 Vectors Vector_Store (via pulumi-aws-native) --------------------------
    if not _AWS_NATIVE_AVAILABLE:
        raise RuntimeError(
            "pulumi-aws-native é necessário para o Vector_Store (S3 Vectors); "
            "instale com: pip install pulumi-aws-native"
        )
    native_prov = aws_native.Provider(f"{name_prefix}-aws-native", region=region)
    native_opts = pulumi.ResourceOptions(provider=native_prov)
    # O provider aws-native não herda default_tags do provider clássico; a
    # Tag_APN é aplicada explicitamente aos recursos S3 Vectors (Req 8.1).
    apn_native_tags = [
        aws_native.TagArgs(key=k, value=v) for k, v in APN_TAG.items()
    ]
    vector_bucket = native_s3vectors.VectorBucket(
        f"{name_prefix}-vector-bucket",
        vector_bucket_name=c.vector_bucket,
        tags=apn_native_tags,
        opts=native_opts,
    )
    r["vector_store"] = native_s3vectors.Index(
        f"{name_prefix}-vector-index",
        vector_bucket_name=c.vector_bucket,
        index_name=c.vector_index,
        data_type="float32",
        dimension=1024,  # amazon.titan-embed-text-v2:0
        distance_metric="cosine",
        metadata_configuration=native_s3vectors.IndexMetadataConfigurationArgs(
            # AMAZON_BEDROCK_TEXT (chunk) e AMAZON_BEDROCK_METADATA (atribuição de
            # fonte) ficam fora do limite de 2048 bytes de metadados filtráveis do
            # S3 Vectors; sem isso, documentos com chaves/URIs longas falham na
            # indexação com "Filterable metadata must have at most 2048 bytes".
            non_filterable_metadata_keys=[
                "AMAZON_BEDROCK_TEXT",
                "AMAZON_BEDROCK_METADATA",
            ],
        ),
        tags=apn_native_tags,
        opts=pulumi.ResourceOptions(provider=native_prov, depends_on=[vector_bucket]),
    )

    # -- IAM roles (least-privilege) ---------------------------------------------
    policy_docs = component_policy_documents(c)
    role_defs = {
        "ingestor_role": f"{name_prefix}-ingestor",
        "kb_role": f"{name_prefix}-kb",
        "agent_role": f"{name_prefix}-agent",
        "api_role": f"{name_prefix}-api",
        "whatsapp_role": f"{name_prefix}-whatsapp-webhook",
        "monitor_role": f"{name_prefix}-monitor",
        "acervo_role": f"{name_prefix}-acervo",
    }
    for role_key, role_name in role_defs.items():
        trust = (
            BEDROCK_ASSUME_ROLE_POLICY
            if role_key == "kb_role"
            else LAMBDA_ASSUME_ROLE_POLICY
        )
        role = aws.iam.Role(
            role_name,
            assume_role_policy=pulumi.Output.json_dumps(trust),
            opts=opts,
        )
        r[role_key] = role
        aws.iam.RolePolicy(
            f"{role_name}-policy",
            role=role.id,
            policy=pulumi.Output.json_dumps(policy_docs[role_key]),
            opts=opts,
        )
        # Inline role policies do not support tags (Req 8.6).
        out.tagging_exceptions.register(
            f"{role_name}-policy", "aws.iam.RolePolicy (inline) não suporta tags"
        )

    # -- Bedrock Knowledge Base (storage: S3 Vectors) -----------------------------
    r["knowledge_base"] = aws.bedrock.AgentKnowledgeBase(
        f"{name_prefix}-kb",
        name=f"{name_prefix}-kb",
        role_arn=r["kb_role"].arn,
        knowledge_base_configuration=aws.bedrock.AgentKnowledgeBaseKnowledgeBaseConfigurationArgs(
            type="VECTOR",
            vector_knowledge_base_configuration=aws.bedrock.AgentKnowledgeBaseKnowledgeBaseConfigurationVectorKnowledgeBaseConfigurationArgs(
                embedding_model_arn=_embedding_model_arn(c, region=region),
            ),
        ),
        storage_configuration=aws.bedrock.AgentKnowledgeBaseStorageConfigurationArgs(
            type="S3_VECTORS",
            s3_vectors_configuration=aws.bedrock.AgentKnowledgeBaseStorageConfigurationS3VectorsConfigurationArgs(
                index_arn=r["vector_store"].index_arn,
            ),
        ),
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[r["vector_store"]]),
    )

    # -- KB data source (bucket S3 com prefixos site/ e cliente/) — Req 1.5 ------
    # Sem `inclusion_prefixes`: a API aceita **no máximo um** prefixo por data
    # source e as origens são duas. Por isso a área de preparo do painel fica em
    # `documents_staging_bucket`, um bucket à parte que nenhum data source
    # varre — a separação por bucket é o que garante que só entra na base o que
    # foi confirmado.
    r["kb_data_source"] = aws.bedrock.AgentDataSource(
        f"{name_prefix}-kb-datasource",
        knowledge_base_id=r["knowledge_base"].id,
        name=f"{name_prefix}-documents",
        data_source_configuration=aws.bedrock.AgentDataSourceDataSourceConfigurationArgs(
            type="S3",
            s3_configuration=aws.bedrock.AgentDataSourceDataSourceConfigurationS3ConfigurationArgs(
                bucket_arn=r["documents_bucket"].arn,
            ),
        ),
        opts=opts,
    )
    # Bedrock data sources do not support tags (Req 8.6).
    out.tagging_exceptions.register(
        f"{name_prefix}-kb-datasource",
        "aws.bedrock.AgentDataSource não suporta tags",
    )

    _definir_base_hierarquica(
        out, c, prov=prov, native_prov=native_prov, opts=opts,
        name_prefix=name_prefix, region=region, tags=apn_native_tags,
        depends_on=[vector_bucket],
    )

    # Base que o assistente, o painel e o Ingestor usam. A hierárquica entrou em
    # 10/11/2026 (§4.2 da documentação); a de divisão fixa fica de reserva para
    # voltar com `pulumi config set progep-ifs:baseAtiva fixa` + `pulumi up` +
    # deploy do runtime — sincronizando-a antes, porque o Ingestor só alimenta a
    # base ativa.
    if base_ativa not in BASES_DISPONIVEIS:
        raise ValueError(f"baseAtiva deve ser uma de {BASES_DISPONIVEIS}, não {base_ativa!r}")
    if base_ativa == "fixa":
        r["kb_ativa"], r["kb_ativa_data_source"] = r["knowledge_base"], r["kb_data_source"]
    else:
        r["kb_ativa"] = r["knowledge_base_hier"]
        r["kb_ativa_data_source"] = r["kb_hier_data_source"]

    # -- DynamoDB session table with TTL (Req 4.1, 4.3) --------------------------
    r["session_table"] = aws.dynamodb.Table(
        f"{name_prefix}-sessions",
        name=c.session_table,
        billing_mode="PAY_PER_REQUEST",
        hash_key="session_id",
        attributes=[aws.dynamodb.TableAttributeArgs(name="session_id", type="S")],
        ttl=aws.dynamodb.TableTtlArgs(attribute_name="ttl", enabled=True),
        opts=opts,
    )

    # -- Cognito user pool + client ----------------------------------------------
    r["cognito_user_pool"] = aws.cognito.UserPool(f"{name_prefix}-userpool", opts=opts)
    # Grupo que autoriza alterar o acervo. A associação de usuários é **manual**,
    # de propósito: quem pode acrescentar ou remover norma do acervo
    # institucional é decisão da PROGEP, não consequência de ter uma conta.
    # O grupo chega na claim `cognito:groups` do token e é conferido no backend
    # (`src.domain.acervo.exige_admin`) — a interface usa a mesma claim para
    # mostrar a aba, mas esconder botão nunca protegeu endpoint.
    aws.cognito.UserGroup(
        f"{name_prefix}-grupo-administradores",
        name=GRUPO_ADMIN,
        user_pool_id=r["cognito_user_pool"].id,
        description="Pode enviar, confirmar e remover documentos do acervo.",
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-grupo-administradores",
        "aws.cognito.UserGroup não suporta tags",
    )
    r["cognito_user_pool_client"] = aws.cognito.UserPoolClient(
        f"{name_prefix}-userpool-client",
        user_pool_id=r["cognito_user_pool"].id,
        # USER_PASSWORD_AUTH habilita autenticação direta por usuário/senha para
        # o usuário de teste e para a integração inicial do site.
        explicit_auth_flows=[
            "ALLOW_USER_SRP_AUTH",
            "ALLOW_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
        ],
        opts=opts,
    )

    # -- Lambda handlers ---------------------------------------------------------
    # Empacota o pacote ``src`` real do repositório (handlers + camada pura); o
    # runtime da Lambda já fornece boto3.
    lambda_code = pulumi.AssetArchive({"src": pulumi.FileArchive("./src")})
    r["api_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-api",
        name=c.agent_function,
        role=r["api_role"].arn,
        runtime="python3.12",
        handler="src.adapters.lambda_entry.api_handler",
        code=lambda_code,
        timeout=35,
        memory_size=512,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                # Arquitetura única: a API invoca o AgentCore Runtime.
                "AGENT_RUNTIME_ARN": c.agent_runtime_arn,
                # Origem permitida no CORS (respostas do Lambda-proxy).
                "CORS_ALLOW_ORIGIN": cors_allow_origin,
            }
        ),
        opts=opts,
    )
    r["ingestor_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-ingestor",
        name=f"{name_prefix}-ingestor",
        role=r["ingestor_role"].arn,
        runtime="python3.12",
        handler="src.flows.ingestion_flow.lambda_handler",
        code=lambda_code,
        timeout=300,
        memory_size=512,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                "KNOWLEDGE_BASE_ID": r["kb_ativa"].id,
                "DATA_SOURCE_ID": r["kb_ativa_data_source"].data_source_id,
                "KB_BUCKET": r["documents_bucket"].bucket,
            }
        ),
        opts=opts,
    )
    # Painel de acervo: envio, confirmação e remoção de documentos pela PROGEP.
    # Função própria (e não uma rota da Lambda da API) para que a permissão de
    # apagar no bucket de documentos fique isolada nesta role — a Lambda que
    # atende o chat não precisa dela, e não deve tê-la.
    r["acervo_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-acervo",
        name=f"{name_prefix}-acervo",
        role=r["acervo_role"].arn,
        runtime="python3.12",
        handler="src.adapters.acervo_handler.handler",
        code=lambda_code,
        timeout=30,
        memory_size=512,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                "KB_BUCKET": r["documents_bucket"].bucket,
                "STAGING_BUCKET": r["staging_bucket"].bucket,
                "KNOWLEDGE_BASE_ID": r["kb_ativa"].id,
                "DATA_SOURCE_ID": r["kb_ativa_data_source"].data_source_id,
                "CORS_ALLOW_ORIGIN": cors_allow_origin,
                "CUSTOS_TABLE": c.custos_table,
                "CUSTOS_META_ISENTO": custos_meta_isento,
            }
        ),
        opts=opts,
    )

    # -- S3 -> Ingestor notification (Req 1.1, 1.7) ------------------------------
    s3_invoke_perm = aws.lambda_.Permission(
        f"{name_prefix}-ingestor-s3-perm",
        action="lambda:InvokeFunction",
        function=r["ingestor_lambda"].name,
        principal="s3.amazonaws.com",
        source_arn=r["documents_bucket"].arn,
        opts=opts,
    )
    aws.s3.BucketNotification(
        f"{name_prefix}-documents-notify",
        bucket=r["documents_bucket"].id,
        lambda_functions=[
            aws.s3.BucketNotificationLambdaFunctionArgs(
                lambda_function_arn=r["ingestor_lambda"].arn,
                events=["s3:ObjectCreated:*"],
            ),
        ],
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[s3_invoke_perm]),
    )
    for rid, reason in (
        (f"{name_prefix}-ingestor-s3-perm", "aws.lambda.Permission não suporta tags"),
        (f"{name_prefix}-documents-notify", "aws.s3.BucketNotification não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, reason)

    # -- Amazon Connect (Roteador_de_Atendimento) --------------------------------
    r["connect_instance"] = aws.connect.Instance(
        f"{name_prefix}-connect",
        identity_management_type="CONNECT_MANAGED",
        inbound_calls_enabled=True,
        outbound_calls_enabled=False,
        instance_alias=f"{name_prefix}-progep",
        opts=opts,
    )

    # -- Roteador_de_Atendimento: fila humana + contact flow (Req 3.1-3.4) -------
    # Horário de funcionamento exigido pela fila; 24x7 para não bloquear o
    # enfileiramento fora do expediente (Req 3.4: enfileira independentemente da
    # disponibilidade de atendente).
    connect_hours = aws.connect.HoursOfOperation(
        f"{name_prefix}-connect-hours",
        instance_id=r["connect_instance"].id,
        name=f"{name_prefix}-24x7",
        description="Enfileiramento 24x7 do assistente PROGEP",
        time_zone="America/Sao_Paulo",
        configs=[
            aws.connect.HoursOfOperationConfigArgs(
                day=day,
                start_time=aws.connect.HoursOfOperationConfigStartTimeArgs(hours=0, minutes=0),
                end_time=aws.connect.HoursOfOperationConfigEndTimeArgs(hours=23, minutes=59),
            )
            for day in (
                "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
                "FRIDAY", "SATURDAY", "SUNDAY",
            )
        ],
        opts=opts,
    )
    r["connect_queue"] = aws.connect.Queue(
        f"{name_prefix}-connect-queue",
        instance_id=r["connect_instance"].id,
        name=f"{name_prefix}-progep-humano",
        description="Fila de atendimento humano da PROGEP (escalonamento do agente)",
        hours_of_operation_id=connect_hours.hours_of_operation_id,
        opts=opts,
    )
    # Perfil de roteamento do atendimento humano. Sem ele o transbordo é um beco
    # sem saída: o contato entra na fila e não há a quem entregar — o perfil é o
    # que liga um atendente à fila da PROGEP.
    #
    # Só o canal **CHAT** está declarado porque o transbordo do WhatsApp chega
    # como contato de chat. Declarar VOICE prometeria um atendimento por
    # telefone que não existe (a instância não tem número de voz).
    r["connect_routing_profile"] = aws.connect.RoutingProfile(
        f"{name_prefix}-connect-routing",
        instance_id=r["connect_instance"].id,
        name=f"{name_prefix}-atendimento-progep",
        description="Atendentes da PROGEP que recebem o transbordo do assistente",
        default_outbound_queue_id=r["connect_queue"].queue_id,
        media_concurrencies=[
            aws.connect.RoutingProfileMediaConcurrencyArgs(
                channel="CHAT",
                # Duas conversas simultâneas por atendente: o chat tem pausas
                # naturais enquanto o servidor lê e responde, e uma de cada vez
                # deixaria a fila parada à toa.
                concurrency=2,
            )
        ],
        queue_configs=[
            aws.connect.RoutingProfileQueueConfigArgs(
                channel="CHAT",
                delay=0,
                priority=1,
                queue_id=r["connect_queue"].queue_id,
            )
        ],
        opts=opts,
    )

    # -- Transbordo humano do WhatsApp (§6.5 da documentação) --------------------
    #
    #   servidor → Lambda do WhatsApp ─ StartChatContact/SendMessage → Connect
    #   atendente → Connect ─ streaming → SNS → Lambda → SendWhatsAppMessage
    #
    # O fluxo só enfileira: quem pediu uma pessoa já conversou com o assistente.
    # (Até 02/10/2026 havia também o `assistente-progep`, um fluxo que chamava o
    # assistente a cada mensagem pelo Connect, do desenho original; nada o usava.)
    r["connect_transfer_flow"] = aws.connect.ContactFlow(
        f"{name_prefix}-connect-transbordo-flow",
        instance_id=r["connect_instance"].id,
        name=f"{name_prefix}-transbordo-whatsapp",
        description="Transbordo do WhatsApp: só enfileira para a fila humana da PROGEP",
        type="CONTACT_FLOW",
        content=r["connect_queue"].arn.apply(
            lambda arn: transfer_flow_content(queue_arn=arn)
        ),
        opts=opts,
    )
    r["connect_chat_stream_topic"] = aws.sns.Topic(
        f"{name_prefix}-connect-chat-stream",
        name=f"{name_prefix}-connect-chat-stream",
        opts=opts,
    )
    conta = aws.get_caller_identity_output(opts=pulumi.InvokeOutputOptions(provider=prov))
    aws.sns.TopicPolicy(
        f"{name_prefix}-connect-chat-stream-policy",
        arn=r["connect_chat_stream_topic"].arn,
        policy=pulumi.Output.all(
            r["connect_chat_stream_topic"].arn, conta.account_id, r["connect_instance"].arn
        ).apply(
            lambda args: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "ConnectPublicaOChat",
                            "Effect": "Allow",
                            "Principal": {"Service": "connect.amazonaws.com"},
                            "Action": "sns:Publish",
                            "Resource": args[0],
                            # Só a instância da PROGEP: a conta tem o Connect de
                            # outros projetos do IFS, e o chat deles não pode
                            # virar mensagem no WhatsApp de um servidor daqui.
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": args[1]},
                                "ArnEquals": {"aws:SourceArn": args[2]},
                            },
                        }
                    ],
                }
            )
        ),
        opts=opts,
    )
    #: Configuração da ponte, repassada às duas entradas do WhatsApp.
    transbordo_env = {
        "CONNECT_INSTANCE_ID": r["connect_instance"].id,
        "CONNECT_TRANSFER_FLOW_ID": r["connect_transfer_flow"].contact_flow_id,
        "CONNECT_STREAM_TOPIC_ARN": r["connect_chat_stream_topic"].arn,
    }
    out.tagging_exceptions.register(
        f"{name_prefix}-connect-chat-stream-policy", "aws.sns.TopicPolicy não suporta tags"
    )

    # -- Segredo da Meta -----------------------------------------------------------
    # Token de System User da Graph API, usado só no diagnóstico manual (§7.6 da
    # documentação: nome de exibição, saúde do número). Nenhuma Lambda o lê desde
    # que o webhook do número de teste saiu (02/10/2026): o envio e o
    # recebimento passam pelo End User Messaging, que tem credencial própria.
    r["whatsapp_secret"] = aws.secretsmanager.Secret(
        f"{name_prefix}-whatsapp-secret",
        name="progep-ifs/whatsapp",
        description="Meta WhatsApp Cloud API: verify_token, access_token, app_secret",
        opts=opts,
    )

    # -- Encerramento de conversa por inatividade --------------------------------
    # O TTL do DynamoDB não serve aqui: ele apaga o item em até 48 h depois de
    # vencido, e uma mensagem de encerramento atrasada seria pior que nenhuma.
    # Uma varredura a cada 5 min dá precisão suficiente para uma janela de 20.
    r["whatsapp_closer_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-whatsapp-encerramento",
        name=f"{name_prefix}-whatsapp-encerramento",
        role=r["whatsapp_role"].arn,
        runtime="python3.12",
        handler="src.adapters.lambda_entry.whatsapp_encerramento_handler",
        code=lambda_code,
        timeout=60,
        memory_size=256,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                "SESSIONS_TABLE": c.session_table,
            }
        ),
        opts=opts,
    )
    encerramento_rule = aws.cloudwatch.EventRule(
        f"{name_prefix}-whatsapp-encerramento-rule",
        name=f"{name_prefix}-whatsapp-encerramento",
        description="Varre conversas do WhatsApp paradas e envia o encerramento",
        schedule_expression="rate(5 minutes)",
        opts=opts,
    )
    aws.cloudwatch.EventTarget(
        f"{name_prefix}-whatsapp-encerramento-target",
        rule=encerramento_rule.name,
        arn=r["whatsapp_closer_lambda"].arn,
        opts=opts,
    )
    aws.lambda_.Permission(
        f"{name_prefix}-whatsapp-encerramento-perm",
        action="lambda:InvokeFunction",
        function=r["whatsapp_closer_lambda"].name,
        principal="events.amazonaws.com",
        source_arn=encerramento_rule.arn,
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-whatsapp-encerramento-target",
        "aws.cloudwatch.EventTarget não suporta tags",
    )

    # -- WhatsApp pelo End User Messaging Social (§6.4 da documentação) ----------
    #
    #   Meta → End User Messaging → SNS → Lambda → SendWhatsAppMessage
    #
    # A WABA vinculada no console aponta para este tópico como destino de
    # eventos. Tópico **padrão**: o End User Messaging não publica em FIFO.
    r["whatsapp_inbound_topic"] = aws.sns.Topic(
        f"{name_prefix}-whatsapp-inbound",
        name=f"{name_prefix}-whatsapp-inbound",
        opts=opts,
    )
    aws.sns.TopicPolicy(
        f"{name_prefix}-whatsapp-inbound-policy",
        arn=r["whatsapp_inbound_topic"].arn,
        policy=pulumi.Output.all(r["whatsapp_inbound_topic"].arn, conta.account_id).apply(
            lambda args: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "EndUserMessagingPublica",
                            "Effect": "Allow",
                            "Principal": {"Service": "social-messaging.amazonaws.com"},
                            "Action": "sns:Publish",
                            "Resource": args[0],
                            # Contra *confused deputy*: só o End User Messaging
                            # **desta conta** publica. A conta é compartilhada
                            # com outros projetos do IFS, e sem a condição a
                            # WABA de outro deles poderia apontar para cá.
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": args[1]},
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:social-messaging:{region}:{args[1]}:*"
                                },
                            },
                        }
                    ],
                }
            )
        ),
        opts=opts,
    )
    r["whatsapp_social_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-whatsapp-social",
        name=f"{name_prefix}-whatsapp-social",
        role=r["whatsapp_role"].arn,
        runtime="python3.12",
        handler="src.adapters.lambda_entry.whatsapp_social_handler",
        code=lambda_code,
        # Agente (~7 s, até ~9 s em follow-up) + saudação + resposta; entrada
        # assíncrona pelo SNS, então ninguém espera a Lambda terminar.
        timeout=60,
        memory_size=512,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                "AGENT_RUNTIME_ARN": c.agent_runtime_arn,
                "SESSIONS_TABLE": c.session_table,
                **transbordo_env,
            }
        ),
        opts=opts,
    )
    # **Sem reentrega.** O SNS invoca a Lambda de forma assíncrona, e o padrão da
    # AWS é tentar de novo duas vezes em caso de erro — o que responderia a mesma
    # pergunta três vezes ao servidor. Perder uma resposta rara é melhor que
    # triplicar todas as que falharem no meio do caminho.
    aws.lambda_.FunctionEventInvokeConfig(
        f"{name_prefix}-whatsapp-social-invoke",
        function_name=r["whatsapp_social_lambda"].name,
        maximum_retry_attempts=0,
        opts=opts,
    )
    social_perm = aws.lambda_.Permission(
        f"{name_prefix}-whatsapp-social-perm",
        action="lambda:InvokeFunction",
        function=r["whatsapp_social_lambda"].name,
        principal="sns.amazonaws.com",
        source_arn=r["whatsapp_inbound_topic"].arn,
        opts=opts,
    )
    aws.sns.TopicSubscription(
        f"{name_prefix}-whatsapp-inbound-sub",
        topic=r["whatsapp_inbound_topic"].arn,
        protocol="lambda",
        endpoint=r["whatsapp_social_lambda"].arn,
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[social_perm]),
    )
    for rid, motivo in (
        (f"{name_prefix}-whatsapp-inbound-policy", "aws.sns.TopicPolicy não suporta tags"),
        (f"{name_prefix}-whatsapp-social-invoke",
         "aws.lambda.FunctionEventInvokeConfig não suporta tags"),
        (f"{name_prefix}-whatsapp-social-perm", "aws.lambda.Permission não suporta tags"),
        (f"{name_prefix}-whatsapp-inbound-sub", "aws.sns.TopicSubscription não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, motivo)

    # Volta do transbordo: fala do atendente, entrada dele e fim do atendimento.
    # Mesma role do WhatsApp — precisa exatamente do que ela já tem: ler a
    # conversa e enviar pelo End User Messaging.
    r["connect_chat_stream_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-connect-chat-stream",
        name=f"{name_prefix}-connect-chat-stream",
        role=r["whatsapp_role"].arn,
        runtime="python3.12",
        handler="src.adapters.lambda_entry.connect_chat_stream_handler",
        code=lambda_code,
        timeout=30,
        memory_size=256,
        environment=aws.lambda_.FunctionEnvironmentArgs(
            variables={
                "SESSIONS_TABLE": c.session_table,
            }
        ),
        opts=opts,
    )
    # Sem reentrega pelo mesmo motivo da entrada: o servidor receberia a fala
    # do atendente repetida.
    aws.lambda_.FunctionEventInvokeConfig(
        f"{name_prefix}-connect-chat-stream-invoke",
        function_name=r["connect_chat_stream_lambda"].name,
        maximum_retry_attempts=0,
        opts=opts,
    )
    stream_perm = aws.lambda_.Permission(
        f"{name_prefix}-connect-chat-stream-perm",
        action="lambda:InvokeFunction",
        function=r["connect_chat_stream_lambda"].name,
        principal="sns.amazonaws.com",
        source_arn=r["connect_chat_stream_topic"].arn,
        opts=opts,
    )
    aws.sns.TopicSubscription(
        f"{name_prefix}-connect-chat-stream-sub",
        topic=r["connect_chat_stream_topic"].arn,
        protocol="lambda",
        endpoint=r["connect_chat_stream_lambda"].arn,
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[stream_perm]),
    )
    for rid, motivo in (
        (f"{name_prefix}-connect-chat-stream-invoke",
         "aws.lambda.FunctionEventInvokeConfig não suporta tags"),
        (f"{name_prefix}-connect-chat-stream-perm", "aws.lambda.Permission não suporta tags"),
        (f"{name_prefix}-connect-chat-stream-sub", "aws.sns.TopicSubscription não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, motivo)

    # -- Bedrock AgentCore Runtime (ECR + role de execução) ----------------------
    # O runtime em si é criado fora do Pulumi (scripts/deploy_agentcore.ps1), pois
    # o provider Pulumi ainda não expõe o recurso; aqui provisionamos os recursos
    # que ele consome: o repositório ECR da imagem e a role de execução.
    r["agentcore_ecr"] = aws.ecr.Repository(
        f"{name_prefix}-agentcore",
        name=f"{name_prefix}-agentcore",
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True,
        ),
        force_delete=True,
        opts=opts,
    )
    # Cada deploy empurra um `latest` novo e a versão anterior fica sem tag —
    # eram 55 imagens (2,9 GB, ~US$ 0,29/mês) em 02/10/2026. Expira o que está
    # sem tag há mais de 7 dias. A imagem em uso não corre risco: ela tem a tag
    # `latest`, e o ECR não expira manifest filho de um índice que ainda existe
    # ("If an image is referenced by a manifest list, it cannot be expired ...
    # without the manifest list being deleted first" — guia do ECR).
    aws.ecr.LifecyclePolicy(
        f"{name_prefix}-agentcore-lifecycle",
        repository=r["agentcore_ecr"].name,
        policy=json.dumps(ECR_LIFECYCLE_POLICY),
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-agentcore-lifecycle", "aws.ecr.LifecyclePolicy não suporta tags"
    )
    agentcore_role = aws.iam.Role(
        f"{name_prefix}-agentcore-exec",
        name=f"{name_prefix}-agentcore-exec",
        assume_role_policy=pulumi.Output.json_dumps(AGENTCORE_ASSUME_ROLE_POLICY),
        opts=opts,
    )
    r["agentcore_role"] = agentcore_role
    aws.iam.RolePolicy(
        f"{name_prefix}-agentcore-exec-policy",
        role=agentcore_role.id,
        policy=pulumi.Output.all(
            r["agentcore_ecr"].arn,
            aws.get_caller_identity_output(
                opts=pulumi.InvokeOptions(provider=prov)
            ).account_id,
        ).apply(
            lambda args: pulumi.Output.json_dumps(
                agentcore_execution_policy_document(
                    replace(c, account_id=args[1]), ecr_repository_arn=args[0]
                )
            )
        ),
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-agentcore-exec-policy",
        "aws.iam.RolePolicy (inline) não suporta tags",
    )

    # -- API Gateway (Cognito authorizer + proxy p/ Lambda) — Req 5.1, 5.2 -------
    r["api_gateway"] = aws.apigateway.RestApi(f"{name_prefix}-api-gw", opts=opts)
    api_authorizer = aws.apigateway.Authorizer(
        f"{name_prefix}-api-auth",
        rest_api=r["api_gateway"].id,
        type="COGNITO_USER_POOLS",
        provider_arns=[r["cognito_user_pool"].arn],
        identity_source="method.request.header.Authorization",
        opts=opts,
    )
    api_resource = aws.apigateway.Resource(
        f"{name_prefix}-api-message",
        rest_api=r["api_gateway"].id,
        parent_id=r["api_gateway"].root_resource_id,
        path_part="message",
        opts=opts,
    )
    api_method = aws.apigateway.Method(
        f"{name_prefix}-api-post",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method="POST",
        authorization="COGNITO_USER_POOLS",
        authorizer_id=api_authorizer.id,
        opts=opts,
    )
    api_integration = aws.apigateway.Integration(
        f"{name_prefix}-api-integration",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method=api_method.http_method,
        integration_http_method="POST",
        type="AWS_PROXY",
        uri=r["api_lambda"].invoke_arn,
        opts=opts,
    )
    # -- CORS: preflight OPTIONS (MOCK) para o site institucional ---------------
    # O POST usa integração Lambda-proxy, então os cabeçalhos CORS da resposta
    # vêm da própria função; aqui só respondemos ao preflight do navegador.
    cors_method = aws.apigateway.Method(
        f"{name_prefix}-api-options",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method="OPTIONS",
        authorization="NONE",  # preflight não carrega credenciais (spec CORS)
        opts=opts,
    )
    cors_integration = aws.apigateway.Integration(
        f"{name_prefix}-api-options-integration",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method=cors_method.http_method,
        type="MOCK",
        request_templates={"application/json": '{"statusCode": 200}'},
        opts=opts,
    )
    cors_method_response = aws.apigateway.MethodResponse(
        f"{name_prefix}-api-options-method-response",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method=cors_method.http_method,
        status_code="200",
        response_parameters={
            "method.response.header.Access-Control-Allow-Origin": True,
            "method.response.header.Access-Control-Allow-Headers": True,
            "method.response.header.Access-Control-Allow-Methods": True,
            "method.response.header.Access-Control-Max-Age": True,
        },
        opts=opts,
    )
    cors_integration_response = aws.apigateway.IntegrationResponse(
        f"{name_prefix}-api-options-integration-response",
        rest_api=r["api_gateway"].id,
        resource_id=api_resource.id,
        http_method=cors_method.http_method,
        status_code=cors_method_response.status_code,
        response_parameters={
            "method.response.header.Access-Control-Allow-Origin": f"'{cors_allow_origin}'",
            "method.response.header.Access-Control-Allow-Headers":
                "'Content-Type,Authorization,x-access-token'",
            "method.response.header.Access-Control-Allow-Methods": "'POST,OPTIONS'",
            "method.response.header.Access-Control-Max-Age": "'3600'",
        },
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[cors_integration]),
    )
    # Respostas de erro do próprio API Gateway (authorizer Cognito rejeitando)
    # não passam pela Lambda, logo não teriam CORS — sem isso o navegador vê uma
    # falha de CORS em vez do 401/403 legítimo e não consegue tratar o erro.
    for gw_key, gw_type, gw_status in (
        ("unauthorized", "UNAUTHORIZED", "401"),
        ("access-denied", "ACCESS_DENIED", "403"),
    ):
        # No pulumi_aws o recurso `aws_api_gateway_gateway_response` chama-se
        # `apigateway.Response` (não `GatewayResponse`).
        aws.apigateway.Response(
            f"{name_prefix}-api-gwresp-{gw_key}",
            rest_api_id=r["api_gateway"].id,
            response_type=gw_type,
            status_code=gw_status,
            response_parameters={
                "gatewayresponse.header.Access-Control-Allow-Origin": f"'{cors_allow_origin}'",
                "gatewayresponse.header.Access-Control-Allow-Headers":
                    "'Content-Type,Authorization,x-access-token'",
                "gatewayresponse.header.Access-Control-Allow-Methods": "'POST,OPTIONS'",
            },
            # Declarado explicitamente (é o default da AWS): sem isso o provider
            # lê o template existente como drift e o remove a cada `up`, deixando
            # o 401/403 com corpo vazio para o site.
            response_templates={
                "application/json": '{"message":$context.error.messageString}'
            },
            opts=opts,
        )
        out.tagging_exceptions.register(
            f"{name_prefix}-api-gwresp-{gw_key}",
            "aws.apigateway.GatewayResponse não suporta tags",
        )

    for rid, reason in (
        (f"{name_prefix}-api-options", "aws.apigateway.Method não suporta tags"),
        (f"{name_prefix}-api-options-integration",
         "aws.apigateway.Integration não suporta tags"),
        (f"{name_prefix}-api-options-method-response",
         "aws.apigateway.MethodResponse não suporta tags"),
        (f"{name_prefix}-api-options-integration-response",
         "aws.apigateway.IntegrationResponse não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, reason)

    apigw_invoke_perm = aws.lambda_.Permission(
        f"{name_prefix}-api-gw-perm",
        action="lambda:InvokeFunction",
        function=r["api_lambda"].name,
        principal="apigateway.amazonaws.com",
        source_arn=r["api_gateway"].execution_arn.apply(lambda arn: f"{arn}/*/*"),
        opts=opts,
    )

    # -- Painel de acervo: /acervo/{proxy+} --------------------------------------
    # Uma rota greedy com ANY, e não um Resource/Method/Integration por endpoint:
    # são seis rotas, o roteamento já existe no handler (que precisa dele de
    # qualquer forma para os testes), e quatro recursos por endpoint só
    # multiplicariam superfície de IaC sem acrescentar garantia.
    acervo_root = aws.apigateway.Resource(
        f"{name_prefix}-acervo",
        rest_api=r["api_gateway"].id,
        parent_id=r["api_gateway"].root_resource_id,
        path_part="acervo",
        opts=opts,
    )
    acervo_proxy = aws.apigateway.Resource(
        f"{name_prefix}-acervo-proxy",
        rest_api=r["api_gateway"].id,
        parent_id=acervo_root.id,
        path_part="{proxy+}",
        opts=opts,
    )
    # O authorizer do Cognito valida o token; o **grupo** é conferido no handler
    # (`acervo.exige_admin`). Token válido responde "quem é", não "pode".
    acervo_method = aws.apigateway.Method(
        f"{name_prefix}-acervo-any",
        rest_api=r["api_gateway"].id,
        resource_id=acervo_proxy.id,
        http_method="ANY",
        authorization="COGNITO_USER_POOLS",
        authorizer_id=api_authorizer.id,
        request_parameters={"method.request.path.proxy": True},
        opts=opts,
    )
    acervo_integration = aws.apigateway.Integration(
        f"{name_prefix}-acervo-integration",
        rest_api=r["api_gateway"].id,
        resource_id=acervo_proxy.id,
        http_method=acervo_method.http_method,
        integration_http_method="POST",  # Lambda-proxy sempre invoca por POST
        type="AWS_PROXY",
        uri=r["acervo_lambda"].invoke_arn,
        opts=opts,
    )
    # Preflight sem autenticação: por especificação do CORS o navegador não
    # manda credencial no OPTIONS, então exigir token aqui quebraria a tela
    # antes de qualquer chamada real.
    acervo_options = aws.apigateway.Method(
        f"{name_prefix}-acervo-options",
        rest_api=r["api_gateway"].id,
        resource_id=acervo_proxy.id,
        http_method="OPTIONS",
        authorization="NONE",
        request_parameters={"method.request.path.proxy": True},
        opts=opts,
    )
    acervo_options_integration = aws.apigateway.Integration(
        f"{name_prefix}-acervo-options-integration",
        rest_api=r["api_gateway"].id,
        resource_id=acervo_proxy.id,
        http_method=acervo_options.http_method,
        integration_http_method="POST",
        type="AWS_PROXY",
        uri=r["acervo_lambda"].invoke_arn,
        opts=opts,
    )
    acervo_invoke_perm = aws.lambda_.Permission(
        f"{name_prefix}-acervo-gw-perm",
        action="lambda:InvokeFunction",
        function=r["acervo_lambda"].name,
        principal="apigateway.amazonaws.com",
        source_arn=r["api_gateway"].execution_arn.apply(lambda arn: f"{arn}/*/*"),
        opts=opts,
    )
    for rid, motivo in (
        (f"{name_prefix}-acervo", "aws.apigateway.Resource não suporta tags"),
        (f"{name_prefix}-acervo-proxy", "aws.apigateway.Resource não suporta tags"),
        (f"{name_prefix}-acervo-any", "aws.apigateway.Method não suporta tags"),
        (f"{name_prefix}-acervo-options", "aws.apigateway.Method não suporta tags"),
        (
            f"{name_prefix}-acervo-integration",
            "aws.apigateway.Integration não suporta tags",
        ),
        (
            f"{name_prefix}-acervo-options-integration",
            "aws.apigateway.Integration não suporta tags",
        ),
        (f"{name_prefix}-acervo-gw-perm", "aws.lambda.Permission não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, motivo)
    api_deployment = aws.apigateway.Deployment(
        f"{name_prefix}-api-deployment",
        rest_api=r["api_gateway"].id,
        # Redeploy quando a superfície da API muda (inclui a rota OPTIONS/CORS).
        # Sem trocar este valor ao acrescentar rota, o stage continua servindo a
        # superfície antiga e a rota nova responde 403 "Missing Authentication
        # Token" — que parece problema de autenticação e não é.
        triggers={"redeploy": "acervo-v1"},
        opts=pulumi.ResourceOptions(
            provider=prov,
            depends_on=[
                api_integration,
                apigw_invoke_perm,
                cors_integration,
                cors_integration_response,
                acervo_integration,
                acervo_options_integration,
                acervo_invoke_perm,
            ],
        ),
    )
    api_stage = aws.apigateway.Stage(
        f"{name_prefix}-api-stage",
        rest_api=r["api_gateway"].id,
        deployment=api_deployment.id,
        stage_name="prod",
        opts=opts,
    )
    r["api_stage_url"] = api_stage.invoke_url
    for rid, reason in (
        (f"{name_prefix}-api-auth", "aws.apigateway.Authorizer não suporta tags"),
        (f"{name_prefix}-api-message", "aws.apigateway.Resource não suporta tags"),
        (f"{name_prefix}-api-post", "aws.apigateway.Method não suporta tags"),
        (f"{name_prefix}-api-integration", "aws.apigateway.Integration não suporta tags"),
        (f"{name_prefix}-api-gw-perm", "aws.lambda.Permission não suporta tags"),
        (f"{name_prefix}-api-deployment", "aws.apigateway.Deployment não suporta tags"),
    ):
        out.tagging_exceptions.register(rid, reason)

    # -- AWS End User Messaging (Conector_WhatsApp) ------------------------------
    # WhatsApp channel is provisioned via AWS End User Messaging Social linked to
    # Amazon Connect; the phone-number/config association is tracked in code.
    r["whatsapp_messaging"] = aws.socialmessaging.WhatsappBusinessAccountId(
        f"{name_prefix}-whatsapp",
        id=c.connect_instance_id,
        opts=opts,
    ) if hasattr(aws, "socialmessaging") else _register_unsupported_service(
        out, "whatsapp_messaging", f"{name_prefix}-whatsapp"
    )

    # -- Secrets Manager ---------------------------------------------------------
    r["secrets"] = aws.secretsmanager.Secret(f"{name_prefix}-secrets", opts=opts)

    # -- Monitoramento de tokens (Req 7.1, 7.3) ---------------------------------
    # O RetrieveAndGenerate não devolve contagem de tokens na resposta; a fonte
    # confiável é o *model invocation logging* do Bedrock, que grava cada chamada
    # (com inputTokenCount/outputTokenCount) neste log group. Os metric filters
    # convertem esses campos em métricas do CloudWatch.
    r["bedrock_invocation_logs"] = aws.cloudwatch.LogGroup(
        f"{name_prefix}-bedrock-invocations",
        name="/progep-ifs/bedrock/model-invocations",
        retention_in_days=90,
        opts=opts,
    )
    bedrock_logging_role = aws.iam.Role(
        f"{name_prefix}-bedrock-logging",
        name=f"{name_prefix}-bedrock-logging",
        assume_role_policy=pulumi.Output.json_dumps(BEDROCK_ASSUME_ROLE_POLICY),
        opts=opts,
    )
    r["bedrock_logging_role"] = bedrock_logging_role
    aws.iam.RolePolicy(
        f"{name_prefix}-bedrock-logging-policy",
        role=bedrock_logging_role.id,
        policy=r["bedrock_invocation_logs"].arn.apply(
            lambda arn: pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "WriteInvocationLogs",
                            "Effect": "Allow",
                            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                            "Resource": [arn, f"{arn}:*"],
                        }
                    ],
                }
            )
        ),
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-bedrock-logging-policy",
        "aws.iam.RolePolicy (inline) não suporta tags",
    )
    # Métricas de tokens extraídas dos logs de invocação (Req 7.1, 7.3).
    for mf_key, mf_field, mf_metric in (
        ("input-tokens", "$.input.inputTokenCount", "InputTokens"),
        ("output-tokens", "$.output.outputTokenCount", "OutputTokens"),
    ):
        aws.cloudwatch.LogMetricFilter(
            f"{name_prefix}-mf-{mf_key}",
            log_group_name=r["bedrock_invocation_logs"].name,
            pattern=f"{{ {mf_field} = * }}",
            metric_transformation=aws.cloudwatch.LogMetricFilterMetricTransformationArgs(
                name=mf_metric,
                namespace="PROGEP-IFS/Bedrock",
                value=mf_field,
                unit="Count",
            ),
            opts=opts,
        )
        out.tagging_exceptions.register(
            f"{name_prefix}-mf-{mf_key}",
            "aws.cloudwatch.LogMetricFilter não suporta tags",
        )

    # -- CloudWatch log group (Monitor_de_Consumo) ------------------------------
    r["monitor_log_group"] = aws.cloudwatch.LogGroup(
        f"{name_prefix}-monitor",
        name=c.log_group.rstrip("/*") or "/progep-ifs",
        retention_in_days=90,
        opts=opts,
    )

    _definir_controle_de_custos(
        out, c, prov=prov, opts=opts, name_prefix=name_prefix, lambda_code=lambda_code,
        equipe_tecnica=custos_equipe_tecnica, meta_isento=custos_meta_isento,
    )

    # -- CloudTrail --------------------------------------------------------------
    trail_bucket = aws.s3.BucketV2(
        f"{name_prefix}-trail-bucket",
        bucket_prefix=f"{name_prefix}-trail-",
        opts=opts,
    )
    caller = aws.get_caller_identity_output(
        opts=pulumi.InvokeOptions(provider=prov)
    )
    trail_name = f"{name_prefix}-trail"
    trail_bucket_policy = aws.s3.BucketPolicy(
        f"{name_prefix}-trail-bucket-policy",
        bucket=trail_bucket.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "AWSCloudTrailAclCheck",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudtrail.amazonaws.com"},
                        "Action": "s3:GetBucketAcl",
                        "Resource": trail_bucket.arn,
                        "Condition": {
                            "StringEquals": {
                                "aws:SourceArn": pulumi.Output.format(
                                    "arn:aws:cloudtrail:{0}:{1}:trail/{2}",
                                    region,
                                    caller.account_id,
                                    trail_name,
                                )
                            }
                        },
                    },
                    {
                        "Sid": "AWSCloudTrailWrite",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudtrail.amazonaws.com"},
                        "Action": "s3:PutObject",
                        "Resource": pulumi.Output.format(
                            "{0}/AWSLogs/{1}/*", trail_bucket.arn, caller.account_id
                        ),
                        "Condition": {
                            "StringEquals": {
                                "s3:x-amz-acl": "bucket-owner-full-control",
                                "aws:SourceArn": pulumi.Output.format(
                                    "arn:aws:cloudtrail:{0}:{1}:trail/{2}",
                                    region,
                                    caller.account_id,
                                    trail_name,
                                ),
                            }
                        },
                    },
                ],
            }
        ),
        opts=opts,
    )
    # Bucket policies do not support tags (Req 8.6).
    out.tagging_exceptions.register(
        f"{name_prefix}-trail-bucket-policy",
        "aws.s3.BucketPolicy não suporta tags",
    )
    # A conta já é coberta pela trilha da organização (`dnx`, multirregião, que
    # é a cópia gratuita dos eventos de gestão). Esta, como segunda cópia, é
    # cobrada: US$ 4,30 em setembro/2026, dois terços do custo do projeto. Fica
    # criada e desligada; religar é `pulumi config set progep-ifs:trilhaPropria
    # true` — necessário se a conta sair da organização da DNX, que leva a
    # trilha dela junto. Os logs já gravados no bucket continuam lá.
    r["audit_trail"] = aws.cloudtrail.Trail(
        trail_name,
        name=trail_name,
        s3_bucket_name=trail_bucket.id,
        include_global_service_events=True,
        is_multi_region_trail=False,
        enable_logging=trilha_propria,
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[trail_bucket_policy]),
    )

    return out


#: Divisão hierárquica da base paralela: o assistente recebe o pedaço-pai
#: (até 1.500 *tokens*), e a busca compara com os pedaços-filho (300). Com a
#: divisão fixa de ~300 *tokens*, uma pergunta de FAQ podia ficar num pedaço e
#: a resposta no seguinte (FAQ de estágio probatório, 09/11/2026): a busca achava
#: o pedaço da pergunta, sem a resposta. Valores padrão recomendados pela AWS.
CHUNKING_HIERARQUICO = {"pai": 1500, "filho": 300, "sobreposicao": 60}

#: Bases que podem ser a ativa (config `progep-ifs:baseAtiva`).
BASES_DISPONIVEIS = ("hierarquica", "fixa")


def _definir_base_hierarquica(
    out: SolutionResources,
    c: ArnContext,
    *,
    prov: Any,
    native_prov: Any,
    opts: Any,
    name_prefix: str,
    region: str,
    tags: list,
    depends_on: list,
) -> None:
    """Base de conhecimento paralela, com divisão hierárquica, para avaliação.

    Mesmos documentos (mesmo bucket), mesmo modelo de embedding, índice próprio.
    Nenhum componente a usa até a troca de ``KNOWLEDGE_BASE_ID``; enquanto isso a
    sincronização é manual (o Ingestor só dispara a base atual).
    """
    r = out.resources
    indice = native_s3vectors.Index(
        f"{name_prefix}-vector-index-hier",
        vector_bucket_name=c.vector_bucket,
        index_name=c.vector_index_hier,
        data_type="float32",
        dimension=1024,
        distance_metric="cosine",
        metadata_configuration=native_s3vectors.IndexMetadataConfigurationArgs(
            non_filterable_metadata_keys=["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"],
        ),
        tags=tags,
        opts=pulumi.ResourceOptions(provider=native_prov, depends_on=depends_on),
    )
    r["knowledge_base_hier"] = aws.bedrock.AgentKnowledgeBase(
        f"{name_prefix}-kb-hier",
        name=f"{name_prefix}-kb-hier",
        role_arn=r["kb_role"].arn,
        knowledge_base_configuration=aws.bedrock.AgentKnowledgeBaseKnowledgeBaseConfigurationArgs(
            type="VECTOR",
            vector_knowledge_base_configuration=aws.bedrock.AgentKnowledgeBaseKnowledgeBaseConfigurationVectorKnowledgeBaseConfigurationArgs(
                embedding_model_arn=_embedding_model_arn(c, region=region),
            ),
        ),
        storage_configuration=aws.bedrock.AgentKnowledgeBaseStorageConfigurationArgs(
            type="S3_VECTORS",
            s3_vectors_configuration=aws.bedrock.AgentKnowledgeBaseStorageConfigurationS3VectorsConfigurationArgs(
                index_arn=indice.index_arn,
            ),
        ),
        opts=pulumi.ResourceOptions(provider=prov, depends_on=[indice]),
    )
    r["kb_hier_data_source"] = aws.bedrock.AgentDataSource(
        f"{name_prefix}-kb-hier-datasource",
        knowledge_base_id=r["knowledge_base_hier"].id,
        name=f"{name_prefix}-documents-hier",
        data_source_configuration=aws.bedrock.AgentDataSourceDataSourceConfigurationArgs(
            type="S3",
            s3_configuration=aws.bedrock.AgentDataSourceDataSourceConfigurationS3ConfigurationArgs(
                bucket_arn=r["documents_bucket"].arn,
            ),
        ),
        vector_ingestion_configuration=aws.bedrock.AgentDataSourceVectorIngestionConfigurationArgs(
            chunking_configuration=aws.bedrock.AgentDataSourceVectorIngestionConfigurationChunkingConfigurationArgs(
                chunking_strategy="HIERARCHICAL",
                hierarchical_chunking_configuration=aws.bedrock.AgentDataSourceVectorIngestionConfigurationChunkingConfigurationHierarchicalChunkingConfigurationArgs(
                    level_configurations=[
                        aws.bedrock.AgentDataSourceVectorIngestionConfigurationChunkingConfigurationHierarchicalChunkingConfigurationLevelConfigurationArgs(
                            max_tokens=CHUNKING_HIERARQUICO["pai"]),
                        aws.bedrock.AgentDataSourceVectorIngestionConfigurationChunkingConfigurationHierarchicalChunkingConfigurationLevelConfigurationArgs(
                            max_tokens=CHUNKING_HIERARQUICO["filho"]),
                    ],
                    overlap_tokens=CHUNKING_HIERARQUICO["sobreposicao"],
                ),
            ),
        ),
        opts=opts,
    )
    out.tagging_exceptions.register(
        f"{name_prefix}-kb-hier-datasource", "aws.bedrock.AgentDataSource não suporta tags"
    )


#: Log groups assinados pelo controle de custos e o filtro de cada um. O de
#: invocações vai sem filtro: a atribuição (PROGEP x outros projetos) depende da
#: identidade dentro do JSON, e é feita no código (`custos.item_da_invocacao`).
CUSTOS_ASSINATURAS: tuple[tuple[str, str, str], ...] = (
    ("invocacoes", "/progep-ifs/bedrock/model-invocations", ""),
    ("whatsapp", "/aws/lambda/progep-ifs-whatsapp-social",
     '?"[entrada] msg=" ?"[entrega]" ?"[chat] mensagem"'),
    ("chat", "/aws/lambda/progep-ifs-connect-chat-stream", '"[chat] mensagem"'),
)


def _definir_controle_de_custos(
    out: SolutionResources,
    c: ArnContext,
    *,
    prov: Any,
    opts: Any,
    name_prefix: str,
    lambda_code: Any,
    equipe_tecnica: str,
    meta_isento: str,
) -> None:
    """Aba "Custos" do painel: tabela, coleta em tempo real e coleta horária."""
    r = out.resources
    r["custos_table"] = aws.dynamodb.Table(
        f"{name_prefix}-custos",
        name=c.custos_table,
        billing_mode="PAY_PER_REQUEST",
        hash_key="pk",
        range_key="sk",
        attributes=[
            aws.dynamodb.TableAttributeArgs(name="pk", type="S"),
            aws.dynamodb.TableAttributeArgs(name="sk", type="S"),
        ],
        # Só as marcas de evento vencem; os custos ficam (histórico do painel).
        ttl=aws.dynamodb.TableTtlArgs(attribute_name="expira_em", enabled=True),
        opts=opts,
    )

    role = aws.iam.Role(
        f"{name_prefix}-custos",
        name=f"{name_prefix}-custos",
        assume_role_policy=pulumi.Output.json_dumps(LAMBDA_ASSUME_ROLE_POLICY),
        opts=opts,
    )
    r["custos_role"] = role
    aws.iam.RolePolicy(
        f"{name_prefix}-custos-policy",
        role=role.id,
        policy=pulumi.Output.all(
            r["agentcore_ecr"].arn,
            aws.get_caller_identity_output(opts=pulumi.InvokeOptions(provider=prov)).account_id,
        ).apply(
            lambda args: pulumi.Output.json_dumps(
                custos_policy_document(replace(c, account_id=args[1]),
                                       ecr_repository_arn=args[0])
            )
        ),
        opts=opts,
    )

    ambiente = {
        "CUSTOS_TABLE": c.custos_table,
        "CUSTOS_EQUIPE_TECNICA": equipe_tecnica,
        "CUSTOS_META_ISENTO": meta_isento,
    }
    r["custos_logs_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-custos-logs",
        name=f"{name_prefix}-custos-logs",
        role=role.arn,
        runtime="python3.12",
        handler="src.adapters.custos_handler.logs_handler",
        code=lambda_code,
        timeout=60,
        memory_size=256,
        environment=aws.lambda_.FunctionEnvironmentArgs(variables=ambiente),
        opts=opts,
    )
    runtime_arn = c.agent_runtime_arn if "*" not in c.agent_runtime_arn else ""
    r["custos_coleta_lambda"] = aws.lambda_.Function(
        f"{name_prefix}-custos-coleta",
        name=f"{name_prefix}-custos-coleta",
        role=role.arn,
        runtime="python3.12",
        handler="src.adapters.custos_handler.coleta_handler",
        code=lambda_code,
        timeout=180,
        memory_size=256,
        environment=aws.lambda_.FunctionEnvironmentArgs(variables={
            **ambiente,
            "AGENT_RUNTIME_ARN": runtime_arn,
            "CUSTOS_ECR_REPO": r["agentcore_ecr"].name,
            "CUSTOS_VECTOR_BUCKET": c.vector_bucket,
            "CUSTOS_VECTOR_INDEX": c.vector_index,
            # progep-ifs/whatsapp e o segredo genérico do projeto.
            "CUSTOS_SEGREDOS": "2",
        }),
        opts=opts,
    )

    regra = aws.cloudwatch.EventRule(
        f"{name_prefix}-custos-coleta-rule",
        name=f"{name_prefix}-custos-coleta",
        description="Coleta horária do controle de custos (métricas, fixos e fatura)",
        schedule_expression="rate(1 hour)",
        opts=opts,
    )
    aws.cloudwatch.EventTarget(
        f"{name_prefix}-custos-coleta-target",
        rule=regra.name,
        arn=r["custos_coleta_lambda"].arn,
        opts=opts,
    )
    aws.lambda_.Permission(
        f"{name_prefix}-custos-coleta-perm",
        action="lambda:InvokeFunction",
        function=r["custos_coleta_lambda"].name,
        principal="events.amazonaws.com",
        source_arn=regra.arn,
        opts=opts,
    )
    sem_tags = [
        (f"{name_prefix}-custos-policy", "aws.iam.RolePolicy (inline) não suporta tags"),
        (f"{name_prefix}-custos-coleta-target", "aws.cloudwatch.EventTarget não suporta tags"),
        (f"{name_prefix}-custos-coleta-perm", "aws.lambda.Permission não suporta tags"),
    ]

    for chave, grupo, filtro in CUSTOS_ASSINATURAS:
        permissao = aws.lambda_.Permission(
            f"{name_prefix}-custos-logs-perm-{chave}",
            action="lambda:InvokeFunction",
            function=r["custos_logs_lambda"].name,
            principal="logs.amazonaws.com",
            source_arn=f"arn:{c.partition}:logs:{c.region}:{c.account_id}:log-group:{grupo}:*"
            if c.account_id != "*"
            else aws.get_caller_identity_output(
                opts=pulumi.InvokeOptions(provider=prov)
            ).account_id.apply(
                lambda conta, g=grupo: f"arn:{c.partition}:logs:{c.region}:{conta}:log-group:{g}:*"
            ),
            opts=opts,
        )
        depende = [permissao]
        if chave == "invocacoes":
            depende.append(r["bedrock_invocation_logs"])
        aws.cloudwatch.LogSubscriptionFilter(
            f"{name_prefix}-custos-sub-{chave}",
            name=f"{name_prefix}-custos",
            log_group=grupo,
            filter_pattern=filtro,
            destination_arn=r["custos_logs_lambda"].arn,
            opts=pulumi.ResourceOptions(provider=prov, depends_on=depende),
        )
        sem_tags += [
            (f"{name_prefix}-custos-logs-perm-{chave}", "aws.lambda.Permission não suporta tags"),
            (f"{name_prefix}-custos-sub-{chave}",
             "aws.cloudwatch.LogSubscriptionFilter não suporta tags"),
        ]
    for rid, motivo in sem_tags:
        out.tagging_exceptions.register(rid, motivo)


def _register_unsupported_service(
    out: SolutionResources, key: str, resource_id: str
) -> Any:
    """Fallback used when the installed pulumi_aws lacks a newer service module.

    Records a placeholder so the presence check still passes and documents the
    limitation as a tagging exception, without failing the whole program.
    """
    out.tagging_exceptions.register(
        resource_id,
        "serviço AWS End User Messaging (Social) indisponível na versão do pulumi_aws instalada",
    )
    return {"placeholder": key}
