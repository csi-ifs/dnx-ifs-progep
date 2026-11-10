"""Pulumi program entrypoint for the Assistente PROGEP-IFS (Task 13.2).

Wires the single global AWS provider (with the ``Tag_APN`` applied via
``default_tags``) to the full set of solution resources declared in
:mod:`src.infra.resources`, enforcing a single environment per execution
(Req 9.2) and validating that every required resource is defined before any is
created (Req 9.3).

Run with ``pulumi up`` from a directory containing the Pulumi project files. The
module guards the Pulumi import so it can be imported (e.g. by tooling) without
Pulumi installed, but ``main`` requires Pulumi at runtime.
"""

from __future__ import annotations

from src.infra.provider import configure_provider, enforce_single_environment, pulumi_available
from src.infra.resources import (
    define_resources,
    defined_resource_keys,
    validate_required_resources,
)


def main() -> None:
    """Provision all solution resources through the shared, APN-tagged provider."""
    if not pulumi_available():  # pragma: no cover - depends on environment
        raise RuntimeError(
            "pulumi/pulumi_aws não estão instalados; instale-os para executar o "
            "programa Pulumi (pip install pulumi pulumi-aws)."
        )

    import pulumi

    # Enforce a single designated environment/stack per execution (Req 9.2).
    stack = enforce_single_environment([pulumi.get_stack()])

    # Block before creating anything if a required resource is missing (Req 9.3).
    validate_required_resources(defined_resource_keys())

    region = pulumi.Config("aws").get("region") or "us-east-1"
    # ARN do AgentCore Runtime (criado fora do Pulumi por scripts/deploy_agentcore.ps1).
    # Configure com: pulumi config set progep-ifs:agentRuntimeArn <arn>
    agent_runtime_arn = pulumi.Config("progep-ifs").get("agentRuntimeArn")
    # Origem permitida no CORS; em produção aponte para o domínio institucional:
    # pulumi config set progep-ifs:corsAllowOrigin https://www.ifs.edu.br
    cors_allow_origin = pulumi.Config("progep-ifs").get("corsAllowOrigin") or "*"
    # Controle de custos: e-mails da equipe técnica cujo uso do modelo (testes e
    # transcrições rodados à mão) conta como custo da PROGEP, e se a Meta
    # confirmou a isenção de órgão público (as mensagens de serviço deixam de
    # ser cobradas acima da cota).
    #   pulumi config set progep-ifs:custosEquipeTecnica a@x.com,b@y.com
    #   pulumi config set progep-ifs:custosMetaIsento true
    custos_equipe = pulumi.Config("progep-ifs").get("custosEquipeTecnica") or ""
    custos_meta_isento = pulumi.Config("progep-ifs").get("custosMetaIsento") or "false"
    # Trilha própria do CloudTrail: desligada, porque a da organização já cobre a
    # conta e a segunda cópia é cobrada. Religue se a conta sair da organização:
    #   pulumi config set progep-ifs:trilhaPropria true
    trilha_propria = pulumi.Config("progep-ifs").get_bool("trilhaPropria") or False
    # Base de conhecimento em uso: "hierarquica" (padrão desde 10/11/2026) ou
    # "fixa" (a original, de reserva). Trocar exige também o deploy do runtime.
    base_ativa = pulumi.Config("progep-ifs").get("baseAtiva") or "hierarquica"
    provider = configure_provider(region=region)
    solution = define_resources(
        provider=provider,
        region=region,
        agent_runtime_arn=agent_runtime_arn,
        cors_allow_origin=cors_allow_origin,
        custos_equipe_tecnica=custos_equipe,
        custos_meta_isento=custos_meta_isento,
        trilha_propria=trilha_propria,
        base_ativa=base_ativa,
    )

    pulumi.export("environment", stack)
    pulumi.export("region", region)
    pulumi.export("resource_count", len(solution.resources))
    pulumi.export("tagging_exceptions", solution.tagging_exceptions.as_dicts())
    # A base **ativa** — é a que scripts/deploy_agentcore.ps1 passa ao runtime.
    pulumi.export("base_ativa", base_ativa)
    pulumi.export("knowledge_base_id", solution.resources["kb_ativa"].id)
    pulumi.export(
        "kb_data_source_id", solution.resources["kb_ativa_data_source"].data_source_id
    )
    pulumi.export("documents_bucket", solution.resources["documents_bucket"].bucket)
    # As duas bases, para sincronizar a de reserva antes de uma volta.
    pulumi.export("knowledge_base_hier_id", solution.resources["knowledge_base_hier"].id)
    pulumi.export(
        "kb_hier_data_source_id", solution.resources["kb_hier_data_source"].data_source_id
    )
    pulumi.export("knowledge_base_fixa_id", solution.resources["knowledge_base"].id)
    pulumi.export(
        "kb_fixa_data_source_id", solution.resources["kb_data_source"].data_source_id
    )
    pulumi.export(
        "agentcore_ecr_url", solution.resources["agentcore_ecr"].repository_url
    )
    pulumi.export(
        "agentcore_exec_role_arn", solution.resources["agentcore_role"].arn
    )
    # Consumidos por scripts/publicar_painel.py para montar o config.json e
    # invalidar o cache depois de publicar a interface.
    pulumi.export("painel_bucket", solution.resources["painel_bucket"].bucket)
    pulumi.export(
        "painel_distribuicao_id", solution.resources["painel_distribuicao"].id
    )
    pulumi.export(
        "painel_url",
        solution.resources["painel_distribuicao"].domain_name.apply(
            lambda d: f"https://{d}"
        ),
    )
    pulumi.export("cognito_user_pool_id", solution.resources["cognito_user_pool"].id)
    pulumi.export(
        "cognito_client_id", solution.resources["cognito_user_pool_client"].id
    )
    pulumi.export("api_url", solution.resources["api_stage_url"])
    # ARN a colar no console do End User Messaging Social como destino de
    # eventos da WABA, no momento do vínculo (Business account → Event
    # destination → Amazon SNS).
    pulumi.export(
        "whatsapp_inbound_topic_arn", solution.resources["whatsapp_inbound_topic"].arn
    )


if __name__ == "__main__":  # pragma: no cover - only runs inside `pulumi up`
    main()
