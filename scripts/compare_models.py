"""Compara modelos de geração nas baterias de QA (qualidade × latência).

Roda o **código de produção** (:class:`src.adapters.agent.BedrockAgentAdapter`)
trocando apenas o ``model_arn``, de modo que threshold de relevância,
``is_no_answer`` e a exigência de citação valem igual para os dois modelos. A
chamada vai direto ao Bedrock, sem o hop do AgentCore Runtime — o objetivo é
isolar o efeito do modelo, não medir o fim a fim.

As perguntas são executadas em paralelo e o resultado é gravado a cada resposta,
então uma credencial que expire no meio não custa o trabalho já feito: basta
rodar de novo.

Uso:
    aws configure export-credentials --profile ifs --format powershell | iex
    python scripts/compare_models.py --out comparacao.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters.agent import BedrockAgentAdapter  # noqa: E402
from src.domain.models import AgentRequest  # noqa: E402

ACCOUNT = "342016537070"
REGION = "us-east-1"
KB_ID = "1AACEWPJNC"
THRESHOLD = 0.4

MODELOS = {
    "sonnet-4.5": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "haiku-4.5": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
}

BATERIAS = (
    "scripts/perguntas_teste.json",
    "scripts/perguntas_processos.json",
    "scripts/perguntas_repositorio.json",
)

_lock = threading.Lock()


def perfil(model_id: str) -> str:
    return f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{model_id}"


def carregar_perguntas(caminhos: tuple[str, ...]) -> list[dict[str, Any]]:
    perguntas: list[dict[str, Any]] = []
    vistas: set[str] = set()
    for caminho in caminhos:
        arquivo = Path(caminho)
        if not arquivo.exists():
            print(f"[aviso] bateria não encontrada: {caminho}")
            continue
        for item in json.loads(arquivo.read_text(encoding="utf-8")):
            if item["pergunta"] in vistas:  # a mesma pergunta aparece em 2 baterias
                continue
            vistas.add(item["pergunta"])
            perguntas.append({**item, "bateria": arquivo.stem})
    return perguntas


def executar(adapter: BedrockAgentAdapter, pergunta: str, indice: int) -> dict[str, Any]:
    inicio = time.monotonic()
    try:
        resposta = adapter.forward_request(
            AgentRequest(session_id=f"cmp-{indice}", channel="site", text=pergunta),
            THRESHOLD,
        )
        return {
            "status": resposta.status,
            "content": resposta.content,
            "sources": [s.doc_id for s in resposta.sources],
            "latencia_s": round(time.monotonic() - inicio, 2),
        }
    except Exception as exc:  # noqa: BLE001 - uma falha não derruba a comparação
        return {
            "status": "erro",
            "content": str(exc)[:200],
            "sources": [],
            "latencia_s": round(time.monotonic() - inicio, 2),
        }


def rodar_modelo(
    nome: str, model_id: str, perguntas: list[dict[str, Any]],
    cache: dict[str, Any], cache_path: Path, workers: int,
) -> None:
    adapter = BedrockAgentAdapter(KB_ID, region_name=REGION, model_arn=perfil(model_id))
    pendentes = [
        (i, p) for i, p in enumerate(perguntas)
        if f"{nome}|{p['pergunta']}" not in cache
    ]
    if not pendentes:
        print(f"  {nome}: nada pendente")
        return
    print(f"  {nome}: {len(pendentes)} pergunta(s) a executar")

    def tarefa(par: tuple[int, dict[str, Any]]) -> None:
        indice, item = par
        resultado = executar(adapter, item["pergunta"], indice)
        with _lock:
            cache[f"{nome}|{item['pergunta']}"] = resultado
            cache_path.write_text(
                json.dumps(cache, ensure_ascii=False), encoding="utf-8"
            )
            feitos = sum(1 for k in cache if k.startswith(f"{nome}|"))
            print(f"    [{nome}] {feitos}/{len(perguntas)} ({resultado['latencia_s']}s)")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(tarefa, pendentes))


def veredito(esperado: str, obtido: str) -> str:
    if esperado == "any":
        return "—"
    return "OK" if esperado == obtido else "FALHOU"


def render(perguntas: list[dict[str, Any]], cache: dict[str, Any]) -> str:
    nomes = list(MODELOS)
    linhas = ["# Sonnet 4.5 × Haiku 4.5 — baterias de QA", ""]

    resumo: dict[str, dict[str, Any]] = {}
    for nome in nomes:
        acertos = julgadas = 0
        latencias: list[float] = []
        tamanhos: list[int] = []
        for item in perguntas:
            r = cache.get(f"{nome}|{item['pergunta']}")
            if not r:
                continue
            latencias.append(r["latencia_s"])
            tamanhos.append(len(r["content"]))
            if item["esperado"] != "any":
                julgadas += 1
                acertos += veredito(item["esperado"], r["status"]) == "OK"
        resumo[nome] = {
            "acertos": acertos, "julgadas": julgadas,
            "p50": round(statistics.median(latencias), 2) if latencias else 0,
            "media": round(statistics.mean(latencias), 2) if latencias else 0,
            "p90": round(sorted(latencias)[int(len(latencias) * 0.9)], 2) if latencias else 0,
            "chars": round(statistics.mean(tamanhos)) if tamanhos else 0,
        }

    linhas += [
        "| modelo | status esperado | latência média | mediana | p90 | tamanho médio |",
        "|---|---|---|---|---|---|",
    ]
    for nome in nomes:
        r = resumo[nome]
        linhas.append(
            f"| {nome} | {r['acertos']}/{r['julgadas']} | {r['media']}s | "
            f"{r['p50']}s | {r['p90']}s | {r['chars']} chars |"
        )

    divergencias = [
        item for item in perguntas
        if all(cache.get(f"{n}|{item['pergunta']}") for n in nomes)
        and cache[f"{nomes[0]}|{item['pergunta']}"]["status"]
        != cache[f"{nomes[1]}|{item['pergunta']}"]["status"]
    ]
    linhas += ["", f"## Divergências de status ({len(divergencias)})", ""]
    if divergencias:
        linhas += ["| pergunta | " + " | ".join(nomes) + " | esperado |", "|---|---|---|---|"]
        for item in divergencias:
            estados = " | ".join(cache[f"{n}|{item['pergunta']}"]["status"] for n in nomes)
            linhas.append(
                f"| {item['pergunta'].replace('|', '/')} | {estados} | {item['esperado']} |"
            )
    else:
        linhas.append("_Nenhuma: os dois modelos classificaram tudo igual._")

    linhas += ["", "## Respostas lado a lado", ""]
    for item in perguntas:
        respostas = {n: cache.get(f"{n}|{item['pergunta']}") for n in nomes}
        if not all(respostas.values()):
            continue
        linhas += [f"### {item['pergunta']}", ""]
        for nome in nomes:
            r = respostas[nome]
            linhas += [
                f"**{nome}** — `{r['status']}` · {r['latencia_s']}s · "
                f"{len(r['sources'])} fonte(s)",
                "",
                (r["content"] or "_(vazio — escalonamento)_"),
                "",
            ]
    return "\n".join(linhas)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("comparacao-modelos.md"))
    parser.add_argument("--cache", type=Path, default=Path("comparacao-cache.json"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    perguntas = carregar_perguntas(BATERIAS)
    if args.limit:
        perguntas = perguntas[: args.limit]
    print(f"{len(perguntas)} pergunta(s) x {len(MODELOS)} modelo(s)")

    cache: dict[str, Any] = {}
    if args.cache.exists():
        cache = json.loads(args.cache.read_text(encoding="utf-8"))
        print(f"cache com {len(cache)} resultado(s) — retomando")

    for nome, model_id in MODELOS.items():
        rodar_modelo(nome, model_id, perguntas, cache, args.cache, args.workers)

    args.out.write_text(render(perguntas, cache), encoding="utf-8")
    print(f"\nRelatório: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
