"""Roda a bateria de perguntas de teste contra o AgentCore Runtime.

Dispara cada pergunta de ``scripts/perguntas_teste.json`` no runtime deployado e
gera um relatório Markdown com a resposta, o ``status`` devolvido e se ele bateu
com o esperado. Serve para medir regressão de qualidade depois de mexer no
corpus da Knowledge Base ou no prompt de geração — em vez de perguntar uma a uma
no WhatsApp.

Uso:
    aws login
    aws configure export-credentials --format powershell | Invoke-Expression
    python scripts/run_qa_suite.py --runtime-arn <arn> --out relatorio.md

O ``bedrock-agentcore`` só existe em botocore recente; quando o boto3 local não
o conhece, o script cai para a AWS CLI (que traz o serviço embutido).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_QUESTIONS = Path(__file__).with_name("perguntas_teste.json")

#: Perguntas marcadas com este esperado são só inspeção manual (sem veredito).
ANY_STATUS = "any"


class RuntimeInvoker:
    """Invoca o AgentCore Runtime via boto3, com fallback para a AWS CLI."""

    def __init__(self, runtime_arn: str, region: str) -> None:
        self.runtime_arn = runtime_arn
        self.region = region
        self._client = self._build_client()

    def _build_client(self) -> Any | None:
        try:
            import boto3  # importado tarde: o modo CLI não precisa dele
            from botocore.exceptions import UnknownServiceError
        except ImportError:
            return None
        try:
            return boto3.client("bedrock-agentcore", region_name=self.region)
        except UnknownServiceError:
            print("[aviso] boto3 local sem 'bedrock-agentcore'; usando a AWS CLI.")
            return None

    def invoke(self, text: str, session_id: str) -> dict[str, Any]:
        payload = {"text": text, "session_id": session_id, "channel": "site"}
        raw = (
            self._invoke_boto3(payload)
            if self._client is not None
            else self._invoke_cli(payload)
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "erro-parse", "content": raw[:500]}

    def _invoke_boto3(self, payload: dict[str, Any]) -> str:
        response = self._client.invoke_agent_runtime(  # type: ignore[union-attr]
            agentRuntimeArn=self.runtime_arn,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
        )
        body = response.get("response")
        data = body.read() if hasattr(body, "read") else body
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    def _invoke_cli(self, payload: dict[str, Any]) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            payload_file = Path(tmp, "payload.json")
            out_file = Path(tmp, "out.json")
            payload_file.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [
                    "aws", "bedrock-agentcore", "invoke-agent-runtime",
                    "--agent-runtime-arn", self.runtime_arn,
                    "--payload", f"file://{payload_file}",
                    "--content-type", "application/json",
                    "--cli-binary-format", "raw-in-base64-out",
                    "--region", self.region,
                    str(out_file),
                ],
                capture_output=True, text=True, shell=(os.name == "nt"),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "falha na AWS CLI")
            return out_file.read_text(encoding="utf-8")


def verdict(expected: str, actual: str) -> str:
    """Símbolo do veredito de uma pergunta."""
    if expected == ANY_STATUS:
        return "—"
    return "OK" if expected == actual else "FALHOU"


def render_report(rows: Sequence[dict[str, Any]]) -> str:
    """Monta o relatório Markdown (resumo + tabela + respostas na íntegra)."""
    judged = [r for r in rows if r["esperado"] != ANY_STATUS]
    passed = [r for r in judged if r["veredito"] == "OK"]
    lines = [
        "# Bateria de perguntas — Assistente PROGEP-IFS",
        "",
        f"{len(passed)}/{len(judged)} no status esperado "
        f"({len(rows) - len(judged)} sem veredito, só inspeção).",
        "",
        "| # | Tema | Pergunta | Esperado | Obtido | Veredito | Fontes |",
        "|---|------|----------|----------|--------|----------|--------|",
    ]
    for i, r in enumerate(rows, 1):
        pergunta = r["pergunta"].replace("|", "\\|")
        lines.append(
            f"| {i} | {r['tema']} | {pergunta} | {r['esperado']} | "
            f"{r['status']} | {r['veredito']} | {len(r['sources'])} |"
        )
    lines += ["", "## Respostas na íntegra", ""]
    for i, r in enumerate(rows, 1):
        lines += [
            f"### {i}. {r['pergunta']}",
            "",
            f"*status:* `{r['status']}` · *confiança:* {r['confidence']} · "
            f"*latência:* {r['latencia_s']}s",
            "",
            (r["content"] or "_(vazio — escalonamento para atendimento humano)_"),
            "",
        ]
        if r["sources"]:
            lines.append("Fontes:")
            lines += [f"- `{s}`" for s in r["sources"]]
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-arn",
        default=os.environ.get("AGENT_RUNTIME_ARN", ""),
        help="ARN do AgentCore Runtime (ou env AGENT_RUNTIME_ARN)",
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--out", type=Path, default=Path("relatorio-qa.md"))
    parser.add_argument(
        "--filter", default="", help="Roda só as perguntas cujo tema contém este texto"
    )
    args = parser.parse_args(argv)

    if not args.runtime_arn:
        parser.error("informe --runtime-arn ou defina AGENT_RUNTIME_ARN")

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.filter:
        questions = [q for q in questions if args.filter.lower() in q["tema"].lower()]

    invoker = RuntimeInvoker(args.runtime_arn, args.region)
    rows: list[dict[str, Any]] = []

    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['pergunta']}")
        started = time.monotonic()
        try:
            answer = invoker.invoke(q["pergunta"], session_id=f"qa-suite-{i}")
        except Exception as exc:  # a bateria segue mesmo se uma pergunta falhar
            print(f"    ERRO: {exc}")
            answer = {"status": "erro", "content": str(exc)[:300]}
        elapsed = round(time.monotonic() - started, 1)

        status = str(answer.get("status", "?"))
        row = {
            "tema": q["tema"],
            "pergunta": q["pergunta"],
            "esperado": q["esperado"],
            "status": status,
            "veredito": verdict(q["esperado"], status),
            "content": str(answer.get("content", "")).strip(),
            "confidence": answer.get("confidence", 0.0),
            "sources": [s.get("doc_id", "") for s in answer.get("sources", []) or []],
            "latencia_s": elapsed,
        }
        rows.append(row)
        print(f"    -> {status} ({elapsed}s) [{row['veredito']}]")

    args.out.write_text(render_report(rows), encoding="utf-8")
    judged = [r for r in rows if r["esperado"] != ANY_STATUS]
    passed = sum(1 for r in judged if r["veredito"] == "OK")
    print(f"\n{passed}/{len(judged)} no status esperado. Relatório: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
