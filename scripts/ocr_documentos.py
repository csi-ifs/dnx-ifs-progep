"""OCR de PDFs digitalizados, para que entrem na Knowledge Base (Req 1.1).

Um PDF sem camada de texto é indexado **vazio** pela KB: nenhum erro aparece,
o documento simplesmente não responde nada. Este script rasteriza cada página,
lê com o Claude Sonnet 4.5 (visão) e grava um Markdown indexável, mantendo o
nome do arquivo de origem para rastreabilidade.

Mesma lição de ``scripts/transcribe_flows.py``: a página é enviada em resolução
alta o bastante para o texto ficar legível, porque abaixo disso o modelo
completa lacunas com texto plausível em vez de admitir que não leu.

Uso:
    aws configure export-credentials --profile ifs --format powershell | iex
    python scripts/ocr_documentos.py --zip "CHATBOT — ....zip" \
        --arquivo "Vacância.pdf" --out-dir docs/ocr [--bucket progep-ifs-documents]
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import unicodedata
import zipfile
from urllib.parse import unquote

from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.config import USER_AGENT_APN  # noqa: E402
from src.domain.fontes import decodificar_nome  # noqa: E402

S3_PREFIX = "cliente/ocr"
DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: Rasterização a 200 dpi: legível para o modelo sem estourar o limite de bytes.
DPI = 200
MAX_IMAGE_BYTES = 3_750_000
VISION_MAX_SIDE = 1568

PROMPT = """Esta é a página de um documento oficial digitalizado do Instituto \
Federal de Sergipe (área de gestão de pessoas).

Transcreva **todo** o texto da página, preservando a estrutura: títulos, \
numeração de artigos e incisos, listas e tabelas (em Markdown).

Regras:
- Transcreva apenas o que está escrito. Não resuma, não complete e não corrija.
- Onde não conseguir ler, escreva `[ilegível]`. Nunca substitua por um texto \
plausível: num documento normativo, uma palavra inventada muda a regra.
- Não comente a tarefa nem descreva a imagem. Devolva só a transcrição.
- Se a página estiver em branco, responda apenas: `PAGINA_EM_BRANCO`."""


def slugify(nome: str) -> str:
    base = nome.rsplit(".", 1)[0]
    texto = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", texto.lower()).strip("-")[:80] or "documento"


def paginas_como_png(pdf_bytes: bytes) -> list[bytes]:
    """Rasteriza o PDF em PNGs, um por página, dentro dos limites do Bedrock."""
    import fitz
    from PIL import Image

    imagens: list[bytes] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        for pagina in pdf:
            pix = pagina.get_pixmap(dpi=DPI)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            if max(img.size) > VISION_MAX_SIDE:
                escala = VISION_MAX_SIDE / max(img.size)
                img = img.resize(
                    (int(img.width * escala), int(img.height * escala)), Image.LANCZOS
                )
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            dados = buffer.getvalue()
            if len(dados) > MAX_IMAGE_BYTES:
                raise ValueError("página excedeu o limite de bytes do Bedrock")
            imagens.append(dados)
    return imagens


def build_client(region: str):
    """Cliente Bedrock com a anotação de billing obrigatória (Req 8.3)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            user_agent_extra=USER_AGENT_APN,
            retries={"max_attempts": 5, "mode": "adaptive"},
            read_timeout=300,
        ),
    )


def transcrever(client, model_id: str, imagem: bytes) -> str:
    resposta = client.converse(
        modelId=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": imagem}}},
                    {"text": PROMPT},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 8192, "temperature": 0},
    )
    blocos = resposta["output"]["message"]["content"]
    return "".join(b.get("text", "") for b in blocos).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path)
    parser.add_argument("--arquivo", action="append", default=[],
                        help="Nome do PDF dentro do pacote (pode repetir)")
    # Os PDFs do site nunca passaram pelo pacote do cliente: foram baixados
    # pelo crawler direto para o bucket. Sem esta origem, os digitalizados que
    # vieram de lá não teriam como ser tratados.
    parser.add_argument("--s3-key", action="append", default=[],
                        help="Chave de um PDF já no bucket (pode repetir)")
    parser.add_argument("--out-dir", type=Path, default=Path("docs/ocr"))
    parser.add_argument("--bucket", default="")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = build_client(args.region)
    s3 = None
    if args.bucket:
        import boto3

        s3 = boto3.client("s3", region_name=args.region)

    # (rótulo exibido, origem legível, bytes do PDF)
    fontes: list[tuple[str, str, bytes]] = []

    if args.arquivo:
        if not args.zip:
            raise SystemExit("--arquivo exige --zip")
        with zipfile.ZipFile(args.zip) as pacote:
            mapa = {i.filename.split("/")[-1]: i.filename
                    for i in pacote.infolist() if not i.is_dir()}
            for alvo in args.arquivo:
                if alvo not in mapa:
                    print(f"[!] não encontrado no pacote: {alvo}")
                    continue
                fontes.append(
                    (alvo, "repositório de documentos da PROGEP — IFS",
                     pacote.read(mapa[alvo]))
                )

    if args.s3_key:
        if s3 is None:
            raise SystemExit("--s3-key exige --bucket")
        for chave in args.s3_key:
            fontes.append(
                (
                    decodificar_nome(unquote(chave.rsplit("/", 1)[-1])),
                    f"site da PROGEP — IFS (s3://{args.bucket}/{chave})",
                    s3.get_object(Bucket=args.bucket, Key=chave)["Body"].read(),
                )
            )

    if not fontes:
        raise SystemExit("nada a transcrever: informe --arquivo ou --s3-key")

    for alvo, origem, dados in fontes:
        print(f"[{alvo}]")
        imagens = paginas_como_png(dados)
        partes: list[str] = []
        ilegiveis = 0
        for numero, imagem in enumerate(imagens, 1):
            texto = transcrever(client, args.model_id, imagem)
            if texto.strip() == "PAGINA_EM_BRANCO":
                print(f"  página {numero}/{len(imagens)}: em branco")
                continue
            ilegiveis += texto.count("[ilegível]")
            partes.append(f"## Página {numero}\n\n{texto}")
            print(f"  página {numero}/{len(imagens)}: {len(texto)} chars")

        destino = args.out_dir / f"{slugify(alvo)}.md"
        destino.write_text(
            "\n".join(
                [
                    f"# {alvo.rsplit('.', 1)[0]}",
                    "",
                    f"**Fonte:** {origem}  ",
                    f"**Arquivo original:** {alvo}  ",
                    f"**Transcrito por OCR em:** {date.today().isoformat()}",
                    "",
                    "> Transcrição automática de documento digitalizado. Em caso"
                    " de divergência, vale o arquivo original.",
                    "",
                    "---",
                    "",
                    "",
                    *partes,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        # Marcar `[ilegível]` é o comportamento correto do modelo, mas precisa
        # ficar visível: uma norma com trecho não lido não pode passar por
        # transcrição completa.
        print(f"  -> {destino}" + (f"  ({ilegiveis} trecho(s) ilegível(is))"
                                   if ilegiveis else ""))

        if s3 is not None:
            s3.put_object(
                Bucket=args.bucket,
                Key=f"{S3_PREFIX}/{destino.name}",
                Body=destino.read_bytes(),
                ContentType="text/markdown; charset=utf-8",
            )
            print(f"  -> s3://{args.bucket}/{S3_PREFIX}/{destino.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
