"""Ingestão do repositório de documentos entregue pela PROGEP (Req 1.5).

Lê o pacote ``.zip`` enviado pelo cliente e envia os documentos para o prefixo
``cliente/`` do bucket da Knowledge Base, preservando a sigla do departamento no
caminho (``cliente/DAP/...``) — isso deixa a origem visível na resposta do
assistente, já que o ``doc_id`` citado é a própria chave do S3.

Antes de enviar, valida cada arquivo contra as mesmas regras do Ingestor
(formato e tamanho, Req 1.1/1.3) e **detecta PDF sem camada de texto**: uma
digitalização entra na KB como documento vazio, sem erro nenhum, e some do
índice em silêncio. Esses são listados separadamente para tratamento com OCR.

Uso:
    python scripts/ingest_repositorio.py --zip "CHATBOT — ....zip" --dry-run
    aws configure export-credentials --profile ifs --format powershell | iex
    python scripts/ingest_repositorio.py --zip "CHATBOT — ....zip" \
        --bucket progep-ifs-documents
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.domain.config import SUPPORTED_CONTENT_TYPES  # noqa: E402

#: Prefixo dos documentos do repositório do cliente no bucket da KB.
S3_PREFIX = "cliente"

#: Limite por documento (Req 1.1): 50 MB.
MAX_SIZE_BYTES = 50 * 1024 * 1024

#: Abaixo disso por página, o PDF é quase certamente digitalizado sem OCR.
MIN_CHARS_POR_PAGINA = 120


@dataclass(frozen=True)
class Documento:
    """Um arquivo do pacote, já mapeado para a chave de destino no S3."""

    nome_original: str
    setor: str
    key: str
    tamanho: int

    @property
    def extensao(self) -> str:
        return self.nome_original.rsplit(".", 1)[-1].lower() if "." in self.nome_original else ""


def sigla_do_setor(caminho: str) -> str:
    """Extrai a sigla do departamento do nome da pasta.

    As pastas vêm como ``(DAP) DIRETORIA DE ADMINISTRAÇÃO DE PESSOAL``; a sigla
    entre parênteses é o identificador estável e curto para a chave do S3.
    """
    for parte in caminho.split("/")[:-1]:
        match = re.match(r"\s*\(([A-ZÀ-Ú]{2,10})\)", parte.strip())
        if match:
            return match.group(1)
    return "GERAL"


def sanitizar(nome: str) -> str:
    """Nome de arquivo seguro para chave do S3, mantendo-o legível.

    A chave aparece como fonte na resposta do assistente, então vale preservar
    o nome; só remove acentos e caracteres que atrapalham em URL.
    """
    base, _, ext = nome.rpartition(".")
    base = base or nome
    texto = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    texto = re.sub(r"[^A-Za-z0-9._-]+", "_", texto).strip("_.")
    texto = re.sub(r"_{2,}", "_", texto)[:150] or "documento"
    return f"{texto}.{ext.lower()}" if ext else texto


def mapear(zip_path: Path) -> tuple[list[Documento], list[tuple[str, str]]]:
    """Lê o pacote e devolve ``(documentos aceitos, rejeitados com motivo)``."""
    aceitos: list[Documento] = []
    rejeitados: list[tuple[str, str]] = []
    usados: set[str] = set()

    with zipfile.ZipFile(zip_path) as pacote:
        for info in pacote.infolist():
            if info.is_dir():
                continue
            nome = info.filename.split("/")[-1]
            extensao = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""

            if extensao not in SUPPORTED_CONTENT_TYPES:
                rejeitados.append((nome, f"formato não suportado (.{extensao})"))
                continue
            if info.file_size > MAX_SIZE_BYTES:
                rejeitados.append((nome, f"{info.file_size / 1e6:.1f} MB > 50 MB"))
                continue

            setor = sigla_do_setor(info.filename)
            key = f"{S3_PREFIX}/{setor}/{sanitizar(nome)}"
            if key in usados:  # nomes iguais em setores diferentes já se separam
                stem, _, ext = key.rpartition(".")
                contador = 2
                while f"{stem}-{contador}.{ext}" in usados:
                    contador += 1
                key = f"{stem}-{contador}.{ext}"
            usados.add(key)
            aceitos.append(Documento(nome, setor, key, info.file_size))

    return aceitos, rejeitados


def sem_camada_de_texto(pacote: zipfile.ZipFile, doc: Documento, nome_no_zip: str) -> bool:
    """``True`` quando o PDF não tem texto extraível (digitalização sem OCR)."""
    if doc.extensao != "pdf":
        return False
    try:
        import fitz
    except ImportError:  # pragma: no cover - checagem é opcional
        return False
    try:
        with fitz.open(stream=pacote.read(nome_no_zip), filetype="pdf") as pdf:
            texto = "".join(pagina.get_text() for pagina in pdf)
            return len(texto) / max(1, pdf.page_count) < MIN_CHARS_POR_PAGINA
    except Exception:  # noqa: BLE001 - arquivo ilegível também merece atenção
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True, help="Pacote do cliente")
    parser.add_argument("--bucket", default="", help="Bucket da KB (vazio = dry-run)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    aceitos, rejeitados = mapear(args.zip)
    print(f"{len(aceitos)} documento(s) aceito(s), {len(rejeitados)} rejeitado(s)\n")

    por_setor: dict[str, int] = {}
    for doc in aceitos:
        por_setor[doc.setor] = por_setor.get(doc.setor, 0) + 1
    for setor, quantidade in sorted(por_setor.items()):
        print(f"  {quantidade:4d}  cliente/{setor}/")

    if rejeitados:
        print("\n-- rejeitados --")
        for nome, motivo in rejeitados:
            print(f"  {nome[:80]} :: {motivo}")

    print("\n-- verificando camada de texto nos PDFs --")
    sem_texto: list[Documento] = []
    with zipfile.ZipFile(args.zip) as pacote:
        mapa = {i.filename.split("/")[-1]: i.filename for i in pacote.infolist() if not i.is_dir()}
        for doc in aceitos:
            if sem_camada_de_texto(pacote, doc, mapa[doc.nome_original]):
                sem_texto.append(doc)
    if sem_texto:
        print(f"  {len(sem_texto)} PDF(s) sem texto extraível — indexariam VAZIOS:")
        for doc in sem_texto:
            print(f"    {doc.nome_original[:80]}")
        print("  (precisam de OCR; ver scripts/ocr_documentos.py)")
    else:
        print("  todos os PDFs têm texto extraível")

    if args.dry_run or not args.bucket:
        print("\n(dry-run — nada enviado)")
        return 0

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    tipos = {"pdf": "application/pdf",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

    print(f"\n-- enviando para s3://{args.bucket}/{S3_PREFIX}/ --")
    with zipfile.ZipFile(args.zip) as pacote:
        mapa = {i.filename.split("/")[-1]: i.filename for i in pacote.infolist() if not i.is_dir()}
        for numero, doc in enumerate(aceitos, 1):
            s3.put_object(
                Bucket=args.bucket,
                Key=doc.key,
                Body=pacote.read(mapa[doc.nome_original]),
                ContentType=tipos.get(doc.extensao, "application/octet-stream"),
            )
            if numero % 25 == 0 or numero == len(aceitos):
                print(f"  {numero}/{len(aceitos)}")

    print("\nUpload concluído; o Ingestor dispara o job da KB automaticamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
