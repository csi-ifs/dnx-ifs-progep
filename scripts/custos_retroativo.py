"""Preenche o controle de custos com o histórico (uso que aconteceu antes do deploy).

O controle mede em tempo real a partir do deploy; este script reprocessa os logs
e as métricas de dias anteriores com **as mesmas funções** da coleta ao vivo
(:func:`src.adapters.custos_handler.processar_eventos` e
:class:`~src.adapters.custos_handler.Coletor`). Pode rodar depois do deploy sem
contar nada duas vezes: os eventos passam pelas mesmas marcas da coleta ao vivo,
e os itens de métrica são substituídos, não somados.

Limites do histórico: o log de invocações do Bedrock guarda 90 dias; as linhas
de mensagem do WhatsApp existem desde 22/09/2026 (número do IFS); as do chat do
Connect, só a partir deste deploy.

Uso:
    python scripts/custos_retroativo.py --inicio 2026-09-01 --simular
    python scripts/custos_retroativo.py --inicio 2026-09-01 --tabela progep-ifs-custos
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.adapters.custos_handler import Coletor, _inicio_do_dia, processar_eventos  # noqa: E402
from src.adapters.custos_store import CustosStore  # noqa: E402
from src.domain import custos as dom  # noqa: E402

GRUPOS = {
    "/progep-ifs/bedrock/model-invocations": None,
    "/aws/lambda/progep-ifs-whatsapp-social": '?"[entrada] msg=" ?"[entrega]" ?"[chat] mensagem"',
    "/aws/lambda/progep-ifs-connect-chat-stream": '"[chat] mensagem"',
}
RUNTIME = "arn:aws:bedrock-agentcore:us-east-1:342016537070:runtime/progep_ifs_agent-07SkZL6Kh5"


class LojaSimulada:
    """Acumula em memória e imprime no fim; nada vai para a AWS."""

    def __init__(self) -> None:
        self.valores: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        self.marcas: set[str] = set()
        self.contadores: dict[tuple[str, str], int] = defaultdict(int)

    def marcar(self, marca: str, agora=None) -> bool:
        if marca in self.marcas:
            return False
        self.marcas.add(marca)
        return True

    def somar(self, dia, item, usd, quantidade=0):
        v = self.valores[(dia, item)]
        v[0] += usd
        v[1] += quantidade

    def definir(self, dia, item, usd, quantidade=0, origem=""):
        self.valores[(dia, item)] = [usd, quantidade]

    def definir_fatura(self, dia, item, usd):
        self.valores[(dia, f"fatura#{item}")] = [usd, 0]

    def contar_mes(self, mes, nome, n=1):
        self.contadores[(mes, nome)] += n
        return self.contadores[(mes, nome)]

    def imprimir(self) -> None:
        por_item: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        por_dia: dict[str, float] = defaultdict(float)
        for (dia, item), (usd, qtd) in sorted(self.valores.items()):
            por_item[item][0] += usd
            por_item[item][1] += qtd
            if not item.startswith("fatura#"):
                por_dia[dia] += usd
        print("\n== por item (período todo)")
        for item, (usd, qtd) in sorted(por_item.items()):
            print(f"  {item:36} US$ {usd:9.4f}   qtd={qtd:10.1f}")
        print("\n== por dia")
        for dia, usd in sorted(por_dia.items()):
            print(f"  {dia}  US$ {usd:8.4f}")


def _equipe_do_pulumi() -> str:
    """A mesma lista que a Lambda usa — os e-mails ficam na config, não no código."""
    r = subprocess.run(
        ["pulumi", "config", "get", "progep-ifs:custosEquipeTecnica",
         "--cwd", str(Path(__file__).resolve().parents[1])],
        capture_output=True, text=True,
        env={**os.environ, "PULUMI_CONFIG_PASSPHRASE": "progep-ifs-local"},
        shell=(os.name == "nt"),
    )
    if r.returncode != 0:
        print("aviso: config custosEquipeTecnica ausente; uso manual da equipe não será contado")
        return ""
    return r.stdout.strip()


def eventos_do_grupo(logs, grupo: str, padrao: str | None, inicio_ms: int, fim_ms: int):
    parametros = {"logGroupName": grupo, "startTime": inicio_ms, "endTime": fim_ms}
    if padrao:
        parametros["filterPattern"] = padrao
    for pagina in logs.get_paginator("filter_log_events").paginate(**parametros):
        yield from pagina["events"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--inicio", required=True, help="AAAA-MM-DD (fuso do IFS)")
    p.add_argument("--tabela", default="progep-ifs-custos")
    p.add_argument("--perfil", default="ifs")
    p.add_argument("--equipe", default=None,
                   help="e-mails da equipe técnica (DNXAccess), separados por vírgula; "
                        "padrão: a config progep-ifs:custosEquipeTecnica do Pulumi")
    p.add_argument("--simular", action="store_true", help="não grava; imprime os totais")
    a = p.parse_args()
    if a.equipe is None:
        a.equipe = _equipe_do_pulumi()

    sessao = boto3.Session(profile_name=a.perfil, region_name="us-east-1")
    loja = LojaSimulada() if a.simular else CustosStore(a.tabela, client=sessao.client("dynamodb"))
    inicio = date.fromisoformat(a.inicio)
    hoje = dom.hoje_local()
    inicio_ms = int(_inicio_do_dia(inicio).timestamp() * 1000)
    fim_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    logs = sessao.client("logs")
    for grupo, padrao in GRUPOS.items():
        try:
            contagem = processar_eventos(
                eventos_do_grupo(logs, grupo, padrao, inicio_ms, fim_ms), loja,
                usuarios_equipe=a.equipe.split(","),
            )
        except logs.exceptions.ResourceNotFoundException:
            contagem = "log group inexistente"
        print(f"{grupo}: {contagem}")

    coletor = Coletor(
        loja,
        cloudwatch=sessao.client("cloudwatch"), lambda_=sessao.client("lambda"),
        logs=logs, ecr=sessao.client("ecr"), s3vectors=sessao.client("s3vectors"),
        ce=sessao.client("ce"), runtime_arn=RUNTIME, ecr_repo="progep-ifs-agentcore",
        vector_bucket="progep-ifs-vectors", vector_index="progep-ifs-index", segredos=2,
    )
    dias = [inicio + timedelta(days=i) for i in range((hoje - inicio).days + 1)]
    # GetMetricData aceita qualquer intervalo, mas em blocos de 2 semanas a
    # resposta fica pequena e um erro não perde o resto.
    for i in range(0, len(dias), 14):
        coletor.coletar(dias[i:i + 14])
    coletor.fatura(hoje)
    print(f"métricas: {len(dias)} dias; consultas US$ {coletor.custo_das_consultas():.4f}")

    if a.simular:
        loja.imprimir()


if __name__ == "__main__":
    main()
