"""Aplica prazo de retenção aos log groups da PROGEP.

Os log groups das Lambdas e do runtime do agente são criados pela própria AWS na
primeira execução, **sem prazo** — guardam tudo para sempre, inclusive linhas
antigas com o telefone de quem conversou (antes de 02/10/2026 ele ia inteiro
para alguns logs). O Pulumi não os gerencia: importá-los com outra retenção faz
o ``import`` falhar, e criá-los colide com os que já existem. Por isso este
script, idempotente: rode depois de criar uma Lambda nova.

90 dias, como o log de invocações do Bedrock: cobre investigação de problemas e o
reprocessamento do controle de custos (scripts/custos_retroativo.py).

Uso:
    python scripts/retencao_logs.py --simular
    python scripts/retencao_logs.py
"""

from __future__ import annotations

import argparse

import boto3

PREFIXOS = ("/aws/lambda/progep-ifs", "/aws/bedrock-agentcore/runtimes/progep_ifs_agent",
            "/progep-ifs")
DIAS = 90


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--perfil", default="ifs")
    p.add_argument("--dias", type=int, default=DIAS)
    p.add_argument("--simular", action="store_true")
    a = p.parse_args()

    logs = boto3.Session(profile_name=a.perfil, region_name="us-east-1").client("logs")
    for prefixo in PREFIXOS:
        for pagina in logs.get_paginator("describe_log_groups").paginate(
                logGroupNamePrefix=prefixo):
            for g in pagina["logGroups"]:
                atual = g.get("retentionInDays")
                if atual == a.dias:
                    continue
                print(f"{g['logGroupName']}: {atual or 'sem prazo'} -> {a.dias} dias")
                if not a.simular:
                    logs.put_retention_policy(logGroupName=g["logGroupName"],
                                              retentionInDays=a.dias)


if __name__ == "__main__":
    main()
