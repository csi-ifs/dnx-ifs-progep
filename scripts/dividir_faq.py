"""Divide um FAQ do acervo em um documento por pergunta (src/domain/faq.py).

A base corta cada arquivo em trechos de tamanho fixo, sem saber onde uma
pergunta começa — num FAQ longo, a resposta de uma pergunta acaba dividida
entre dois trechos e o assistente responde só metade. Este script:

**O painel já faz isto sozinho** ao indexar um FAQ (src/adapters/acervo_handler.py).
O script serve para FAQ que entrou por outro caminho (repositório, site) ou
que o painel não conseguiu dividir. Ele:

1. lê o FAQ do bucket (PDF, DOCX, HTML, MD ou TXT) e extrai o texto, com o
   mesmo código da Lambda do painel;
2. divide em perguntas e grava ``<prefixo>/faq/<nome do FAQ>/NN-pergunta.md``
   (o rótulo da fonte continua sendo o nome do FAQ — src/domain/fontes.py);
3. apaga arquivos de uma divisão anterior que não existem mais;
4. com ``--remover-original``, tira o FAQ original da base do mesmo jeito que
   o painel (cópia na lixeira por 30 dias + ``DeleteKnowledgeBaseDocuments``):
   se ficasse, os trechos cortados continuariam disputando a busca.

**Confira a divisão antes** com ``--dry-run --saida <pasta>``: se o formato do
FAQ não for reconhecido o script para sem enviar nada.

Uso:
    aws configure export-credentials --profile ifs --format powershell | iex
    python scripts/dividir_faq.py --chave "cliente/painel/FAQ - ESTÁGIO PROBATÓRIO.pdf" --dry-run --saida docs/faq
    python scripts/dividir_faq.py --chave "cliente/painel/FAQ - ESTÁGIO PROBATÓRIO.pdf" \\
        --remover-original --kb-id INLYXJCFOW --ds-id I50FTDPEQ6
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# O mesmo código da Lambda do painel: a divisão sai igual pelos dois caminhos.
from src.adapters.extracao import extrair_texto  # noqa: E402
from src.domain.acervo import chave_original_do_faq, chave_removida  # noqa: E402
from src.domain.faq import documentos, nome_do_faq, pasta_do_faq  # noqa: E402


def chave_real(s3, bucket: str, chave: str) -> str:
    """A chave como está no S3 (acento decomposto ou não)."""
    alvo = unicodedata.normalize("NFC", chave)
    prefixo = chave.rsplit("/", 1)[0] + "/"
    for pagina in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefixo):
        for obj in pagina.get("Contents", []) or []:
            if unicodedata.normalize("NFC", obj["Key"]) == alvo:
                return obj["Key"]
    raise SystemExit(f"não encontrado no bucket: {chave}")


def para_lixeira(s3, kb, chave: str, *, bucket: str, staging: str, kb_id: str, ds_id: str) -> None:
    """O mesmo da remoção do painel (src/adapters/acervo_handler._para_lixeira)."""
    try:
        s3.head_object(Bucket=bucket, Key=chave)
        s3.copy_object(Bucket=staging, Key=chave_removida(chave),
                       CopySource={"Bucket": bucket, "Key": chave})
        s3.delete_object(Bucket=bucket, Key=chave)
    except s3.exceptions.ClientError as erro:  # já foi para a lixeira numa tentativa anterior
        if erro.response["Error"]["Code"] not in ("404", "NoSuchKey"):
            raise
    remover_da_base(kb, chave, bucket=bucket, kb_id=kb_id, ds_id=ds_id)


def remover_da_base(kb, chave: str, *, bucket: str, kb_id: str, ds_id: str) -> None:
    """``DeleteKnowledgeBaseDocuments``, esperando a indexação em andamento.

    A API recusa a remoção enquanto houver ingestion job rodando no data source
    — e acabamos de enviar os arquivos novos, que disparam um. Espera até ~10 min.
    """
    import time

    for tentativa in range(40):
        try:
            kb.delete_knowledge_base_documents(
                knowledgeBaseId=kb_id, dataSourceId=ds_id,
                documentIdentifiers=[{"dataSourceType": "S3",
                                      "s3": {"uri": f"s3://{bucket}/{chave}"}}],
            )
            return
        except kb.exceptions.ValidationException as erro:
            if "running ingestion job" not in str(erro):
                raise
            if tentativa == 0:
                print("  aguardando a indexação em andamento terminar…")
            time.sleep(15)
    raise SystemExit(f"a indexação não terminou a tempo; rode de novo para remover {chave}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chave", action="append", required=True,
                        help="Chave do FAQ no bucket (pode repetir)")
    parser.add_argument("--titulo", default="", help="Título exibido (padrão: nome do arquivo)")
    parser.add_argument("--bucket", default="progep-ifs-documents")
    parser.add_argument("--staging", default="progep-ifs-staging")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--saida", type=Path, help="Grava os arquivos também nesta pasta local")
    parser.add_argument("--sem-apresentacao", action="store_true",
                        help="Descarta o texto antes da 1ª pergunta (ex.: menu de página HTML)")
    parser.add_argument("--remover-original", action="store_true")
    parser.add_argument("--kb-id", default="", help="Base ativa (output knowledge_base_id)")
    parser.add_argument("--ds-id", default="", help="Data source da base ativa (kb_data_source_id)")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.remover_original and not (args.kb_id and args.ds_id):
        raise SystemExit("--remover-original exige --kb-id e --ds-id (outputs do Pulumi)")

    import boto3

    s3 = boto3.client("s3", region_name=args.region)
    kb = boto3.client("bedrock-agent", region_name=args.region)

    for pedida in args.chave:
        chave = chave_real(s3, args.bucket, pedida)
        texto = extrair_texto(chave, s3.get_object(Bucket=args.bucket, Key=chave)["Body"].read())
        titulo = args.titulo or " ".join(nome_do_faq(chave).replace("_", " ").split())
        nome_original = unicodedata.normalize("NFC", chave.rsplit("/", 1)[-1])
        arquivos = documentos(
            titulo, f"{nome_original} — perguntas frequentes da PROGEP/IFS", texto or "",
            com_apresentacao=not args.sem_apresentacao,
        )
        pasta = unicodedata.normalize("NFC", pasta_do_faq(chave.split("/", 1)[0], chave))
        print(f"\n[{nome_original}] {len(arquivos)} arquivo(s) → s3://{args.bucket}/{pasta}")
        if not arquivos:
            print("  nenhuma pergunta reconhecida — nada enviado. Confira o formato do FAQ.")
            continue
        for nome, corpo in arquivos:
            print(f"  {len(corpo):5} chars  {nome}")
            if args.saida:
                destino = args.saida / pasta
                destino.mkdir(parents=True, exist_ok=True)
                (destino / nome).write_text(corpo, encoding="utf-8")
        if args.dry_run:
            continue

        novos = {pasta + nome for nome, _ in arquivos}
        antigos = {
            obj["Key"]
            for pagina in s3.get_paginator("list_objects_v2").paginate(Bucket=args.bucket, Prefix=pasta)
            for obj in pagina.get("Contents", []) or []
        }
        for nome, corpo in arquivos:
            s3.put_object(Bucket=args.bucket, Key=pasta + nome, Body=corpo.encode("utf-8"),
                          ContentType="text/markdown; charset=utf-8")
        # Pergunta que sumiu do FAQ não pode continuar respondendo.
        for sobra in sorted(antigos - novos):
            if args.kb_id and args.ds_id:
                para_lixeira(s3, kb, sobra, bucket=args.bucket, staging=args.staging,
                             kb_id=args.kb_id, ds_id=args.ds_id)
                print(f"  removido (não existe mais no FAQ): {sobra}")
            else:
                print(f"  [!] sobra de divisão anterior, informe --kb-id/--ds-id para remover: {sobra}")
        print(f"  enviados {len(arquivos)}; o Ingestor indexa em alguns minutos.")
        # O original fica guardado fora da base, como faz o painel: serve para
        # baixá-lo e para o painel reconhecer o reenvio do mesmo arquivo.
        s3.copy_object(Bucket=args.staging, Key=chave_original_do_faq(pasta, nome_original),
                       CopySource={"Bucket": args.bucket, "Key": chave})
        if args.remover_original:
            para_lixeira(s3, kb, chave, bucket=args.bucket, staging=args.staging,
                         kb_id=args.kb_id, ds_id=args.ds_id)
            print(f"  original fora da base (recuperável em {chave_removida(chave)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
