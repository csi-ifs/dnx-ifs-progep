"""Testa continuidade de conversa no runtime (follow-ups reais).

As baterias de QA usam uma sessão nova por pergunta — servem para garantir que
a sessão não *quebrou* nada, mas não exercitam o que ela adiciona. Aqui cada
diálogo roda com a **mesma** chave de conversa, e as perguntas seguintes são
dependentes do contexto de propósito ("e nesse caso?", "quanto tempo antes?").

O sinal de sucesso não é "respondeu": é responder algo que **só** faz sentido
com o turno anterior em mente.

Uso:
    aws configure export-credentials --profile ifs --format powershell | iex
    python scripts/testa_conversa.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:342016537070:runtime/"
    "progep_ifs_agent-07SkZL6Kh5"
)

#: Cada diálogo é uma lista de turnos. O primeiro estabelece o assunto; os
#: seguintes só são respondíveis se o contexto tiver sido preservado.
DIALOGOS: tuple[tuple[str, ...], ...] = (
    (
        "Como solicito licença capacitação na PROGEP?",
        "E quanto tempo antes preciso pedir?",
        "Quem precisa assinar?",
    ),
    (
        "Como funciona a aposentadoria voluntária?",
        "Quais setores analisam esse processo?",
    ),
    (
        "Recebi um valor a maior por engano. O que faço?",
        "Posso parcelar?",
    ),
    (
        "O que é o estágio probatório?",
        "Quanto tempo dura?",
    ),
)


def invocar(client, texto: str, chave: str) -> dict:
    inicio = time.monotonic()
    resposta = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        payload=json.dumps(
            {"text": texto, "session_id": chave, "channel": "whatsapp"}
        ).encode("utf-8"),
        contentType="application/json",
    )
    corpo = resposta.get("response")
    dados = corpo.read() if hasattr(corpo, "read") else corpo
    saida = json.loads(dados.decode("utf-8") if isinstance(dados, bytes) else dados)
    saida["_latencia"] = round(time.monotonic() - inicio, 1)
    return saida


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("conversa.md"))
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-agentcore", region_name="us-east-1", config=Config(read_timeout=180)
    )

    linhas = ["# Continuidade de conversa — assistente PROGEP", ""]
    escalonou_seguimento = 0
    total_seguimento = 0

    for numero, dialogo in enumerate(DIALOGOS, 1):
        # Chave nova por diálogo, simulando um número de WhatsApp diferente.
        chave = f"teste-conversa-{uuid.uuid4().hex[:10]}"
        print(f"\n=== Diálogo {numero} (chave {chave}) ===")
        linhas += [f"## Diálogo {numero}", ""]

        for posicao, pergunta in enumerate(dialogo, 1):
            try:
                r = invocar(client, pergunta, chave)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERRO: {exc}")
                linhas += [f"**P{posicao}:** {pergunta}", "", f"_erro: {exc}_", ""]
                continue

            status = r.get("status", "?")
            conteudo = (r.get("content") or "").strip()
            if posicao > 1:
                total_seguimento += 1
                if status != "answered":
                    escalonou_seguimento += 1

            marca = "" if posicao == 1 else "  (follow-up)"
            print(f"  P{posicao}{marca}: {pergunta}")
            print(f"     -> {status} ({r['_latencia']}s) {conteudo[:100]}")

            linhas += [
                f"**P{posicao}:** {pergunta}",
                "",
                f"*{status}* · {r['_latencia']}s · {len(r.get('sources') or [])} fonte(s)",
                "",
                conteudo or "_(vazio — escalonamento)_",
                "",
            ]

    respondidos = total_seguimento - escalonou_seguimento
    resumo = (
        f"{respondidos}/{total_seguimento} perguntas de follow-up respondidas "
        f"(as que dependem do turno anterior)."
    )
    linhas.insert(2, resumo)
    linhas.insert(3, "")
    print(f"\n{resumo}")

    args.out.write_text("\n".join(linhas), encoding="utf-8")
    print(f"Relatório: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
