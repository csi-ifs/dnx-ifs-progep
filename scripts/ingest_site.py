"""Ingestão inicial da fonte "site" (Req 1.5) — páginas e editais da PROGEP.

Baixa as páginas públicas da PROGEP no site do IFS e os PDFs de editais
vinculados, e envia tudo para o prefixo ``site/`` do bucket de documentos da
Knowledge Base. O upload dispara a notificação S3 → Lambda do Ingestor, que
valida cada documento e inicia o ingestion job da KB (Req 1.1).

Uso:
    python scripts/ingest_site.py --bucket progep-ifs-documents [--dry-run]

Sem dependências externas (usa apenas stdlib + boto3).
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

#: Páginas de conteúdo da PROGEP voltadas ao servidor (rotina de gestão de
#: pessoas: licenças, capacitação, afastamentos, progressão, saúde, benefícios).
#: São enumeradas explicitamente porque o site não usa um prefixo de path comum
#: para elas — sem esta lista o crawl só alcança as listagens de editais, e
#: perguntas do dia a dia caem em ``not_found`` por falta de fonte.
SERVIDOR_PAGES: tuple[str, ...] = (
    "https://www.ifs.edu.br/guia-do-servidor.html",
    "https://www.ifs.edu.br/perguntas-frequentes-faq.html",
    "https://www.ifs.edu.br/legislacao-progep.html",
    "https://www.ifs.edu.br/capacitacao-servidor.html",
    "https://www.ifs.edu.br/capacitacao-servidor-2.html",
    "https://www.ifs.edu.br/editais-de-afastamento-tae-e-docente.html",
    "https://www.ifs.edu.br/rsc-pcctae.html",
    "https://www.ifs.edu.br/siass",
    "https://www.ifs.edu.br/programa-de-gestao-teletrabalho2.html",
    "https://www.ifs.edu.br/quadro-de-vagas-qrsta-e-bpeq.html",
    "https://www.ifs.edu.br/quem-e-quem-progep.html",
    "https://www.ifs.edu.br/apresentacao-progep.html",
    "https://www.ifs.edu.br/acoes-e-programas.html",
    "https://www.ifs.edu.br/funcefetse.html",
    "https://www.ifs.edu.br/pda.html",
    "https://www.ifs.edu.br/novos-projetos-e-programas-em-gestao-de-pessoas.html",
    "https://www.ifs.edu.br/editais-de-multiplicadores.html",
)

#: Páginas-fonte da PROGEP (documentação e hotsite de editais).
SEED_PAGES: tuple[str, ...] = (
    "https://www.ifs.edu.br/reitoria/pro-reitorias/progep.html",
    "https://www.ifs.edu.br/concursos-editais-progep/2026/317-hotsite-progep/editais-progep.html",
    *SERVIDOR_PAGES,
)

#: Apenas conteúdo do domínio institucional é ingerido.
ALLOWED_HOST = "www.ifs.edu.br"

#: Limite de tamanho por documento (Req 1.1): 50 MB.
MAX_SIZE_BYTES = 50 * 1024 * 1024

_UA = "Mozilla/5.0 (compatible; AssistenteProgepIFS/0.1; ingestao-inicial)"


class _LinkExtractor(HTMLParser):
    """Coleta os href de âncoras de uma página HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def _encode_url(url: str) -> str:
    """Percent-encoda o path/query da URL (nomes de arquivo acentuados)."""
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%")
    query = quote(parts.query, safe="=&%")
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def fetch(url: str) -> bytes:
    """Baixa uma URL (até 50 MB), retornando o corpo em bytes."""
    req = Request(_encode_url(url), headers={"User-Agent": _UA})
    with urlopen(req, timeout=60) as resp:  # nosec: fonte institucional fixa
        return resp.read(MAX_SIZE_BYTES + 1)


#: Prefixos de path cujas subpáginas (listagens por ano) também são seguidas.
CRAWL_PATH_PREFIXES: tuple[str, ...] = (
    "/concursos-editais-progep/",
    "/remocao-interna/",
)


def _same_host_links(page_url: str, html_text: str) -> list[str]:
    parser = _LinkExtractor()
    parser.feed(html_text)
    seen: dict[str, None] = {}
    for href in parser.links:
        absolute = urljoin(page_url, href).split("#", 1)[0]
        if urlparse(absolute).hostname == ALLOWED_HOST:
            seen.setdefault(absolute, None)
    return list(seen)


def pdf_links(page_url: str, html_text: str) -> list[str]:
    """Extrai os links de PDF (mesmo domínio) de uma página."""
    return [
        link
        for link in _same_host_links(page_url, html_text)
        if urlparse(link).path.lower().endswith(".pdf")
    ]


def subpage_links(page_url: str, html_text: str) -> list[str]:
    """Extrai subpáginas de listagem (por ano) a serem seguidas — um nível."""
    result = []
    for link in _same_host_links(page_url, html_text):
        path = urlparse(link).path
        if path.lower().endswith(".html") and any(
            path.startswith(p) for p in CRAWL_PATH_PREFIXES
        ):
            result.append(link)
    return result


_YEAR_RE = re.compile(r"/(20\d{2})(?:[./]|$)")

#: Seções organizadas por ano cujos editais o modo ``--deep`` percorre. A
#: remoção interna entra aqui porque o processo **só** está descrito dentro dos
#: editais: a página índice e as listagens por ano são apenas links, e sem os
#: PDFs o assistente não responde "como funciona a remoção interna".
_DEEP_SECTIONS: tuple[str, ...] = ("concursos-editais-progep", "remocao-interna")

_SECTIONS_ALT = "|".join(_DEEP_SECTIONS)

#: Página de detalhe de um edital: ``/<secao>/YYYY/<id>-<slug>.html``.
_DETAIL_RE = re.compile(rf"^/({_SECTIONS_ALT})/20\d{{2}}/.+\.html$")

#: Página de listagem por ano: ``/<secao>/YYYY.html``.
_YEAR_LIST_RE = re.compile(rf"^/({_SECTIONS_ALT})/20\d{{2}}\.html$")


def page_year(url: str) -> int | None:
    """Extrai o ano presente no path da URL (ex.: ``/2024/`` ou ``/2024.html``)."""
    match = _YEAR_RE.search(urlparse(url).path)
    return int(match.group(1)) if match else None


def is_year_list_page(url: str) -> bool:
    """``True`` se a URL é uma listagem de editais por ano."""
    return bool(_YEAR_LIST_RE.match(urlparse(url).path))


def is_detail_page(url: str) -> bool:
    """``True`` se a URL é uma página de detalhe de um edital específico."""
    return bool(_DETAIL_RE.match(urlparse(url).path))


def detail_page_links(page_url: str, html_text: str) -> list[str]:
    """Links de páginas de detalhe de edital referenciados numa listagem de ano."""
    return [link for link in _same_host_links(page_url, html_text) if is_detail_page(link)]


def slug_for(url: str, suffix: str) -> str:
    """Gera uma chave S3 estável e legível para a URL."""
    path = urlparse(url).path.strip("/") or "index"
    text = unicodedata.normalize("NFKD", path).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._/-]+", "-", text).strip("-/")
    text = text.removesuffix(".html").removesuffix(".pdf")
    return f"site/{text}{suffix}"


def main(argv: list[str] | None = None) -> int:
    # Os nomes dos PDFs do site são acentuados; no console cp1252 do Windows o
    # print quebraria com UnicodeEncodeError no meio do crawl.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - stdout redirecionado
        pass

    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--bucket", required=True, help="Bucket de documentos da KB")
    args_parser.add_argument("--region", default="us-east-1")
    args_parser.add_argument(
        "--dry-run", action="store_true", help="Lista o que seria enviado, sem subir"
    )
    args_parser.add_argument(
        "--deep", action="store_true",
        help="Segue as páginas de detalhe dos editais e baixa os PDFs vinculados",
    )
    args_parser.add_argument(
        "--max-pdfs", type=int, default=150,
        help="Teto de PDFs baixados no modo --deep (padrão 150)",
    )
    args_parser.add_argument(
        "--max-pdfs-per-page", type=int, default=25,
        help="Teto de PDFs por página (evita que uma listagem longa domine o corpus)",
    )
    args_parser.add_argument(
        "--pdf-include", type=str, default="",
        help=(
            "Só baixa PDFs cujo nome casa com esta regex, ex.: '(?i)edital'. "
            "Útil nas seções por ano, onde a maioria dos PDFs é "
            "resultado preliminar/final e só o edital descreve o processo."
        ),
    )
    args_parser.add_argument(
        "--sections", type=str, default="",
        help=(
            "Restringe o modo --deep a seções, ex.: 'remocao-interna'. Sem isso "
            f"todas são percorridas: {', '.join(_DEEP_SECTIONS)}"
        ),
    )
    args_parser.add_argument(
        "--years", type=str, default="",
        help="Filtra anos no modo --deep, ex.: '2024,2025' (padrão: todos)",
    )
    args = args_parser.parse_args(argv)

    year_filter = {int(y) for y in args.years.split(",") if y.strip()} if args.years else None
    pdf_include = re.compile(args.pdf_include) if args.pdf_include else None
    uploads: list[tuple[str, bytes, str]] = []  # (key, body, content_type)
    visited: set[str] = set()
    pdf_count = 0

    def fetch_pdf(pdf: str) -> None:
        nonlocal pdf_count
        if pdf in visited or pdf_count >= args.max_pdfs:
            return
        # O nome do arquivo vem percent-encodado na URL; comparar decodificado.
        if pdf_include and not pdf_include.search(unquote(urlparse(pdf).path)):
            return
        visited.add(pdf)
        print(f"    [pdf] {pdf}")
        try:
            pdf_body = fetch(pdf)
        except Exception as exc:  # segue com os demais documentos
            print(f"      falha ao baixar: {exc}")
            return
        if len(pdf_body) > MAX_SIZE_BYTES:
            print("      descartado: > 50 MB (Req 1.1)")
            return
        uploads.append((slug_for(pdf, ".pdf"), pdf_body, "application/pdf"))
        pdf_count += 1

    def process_page(page: str, *, follow: bool) -> None:
        if page in visited:
            return
        visited.add(page)
        print(f"[pagina] {page}")
        try:
            body = fetch(page)
        except Exception as exc:
            print(f"  falha ao baixar: {exc}")
            return
        if len(body) > MAX_SIZE_BYTES:
            print("  descartada: > 50 MB (Req 1.1)")
            return
        uploads.append((slug_for(page, ".html"), body, "text/html"))
        text = body.decode("utf-8", errors="replace")
        for pdf in pdf_links(page, text)[: args.max_pdfs_per_page]:
            fetch_pdf(pdf)
        if follow:
            for sub in subpage_links(page, text):
                process_page(sub, follow=False)

    def process_deep(page: str) -> None:
        """Modo profundo: listagem por ano → páginas de detalhe → PDFs."""
        if page in visited:
            return
        visited.add(page)
        year = page_year(page)
        if year_filter and year is not None and year not in year_filter:
            return
        print(f"[listagem] {page}")
        try:
            text = fetch(page).decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"  falha ao baixar: {exc}")
            return
        for detail in detail_page_links(page, text):
            if detail in visited or pdf_count >= args.max_pdfs:
                continue
            visited.add(detail)
            print(f"  [detalhe] {detail}")
            try:
                detail_text = fetch(detail).decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"    falha ao baixar: {exc}")
                continue
            for pdf in pdf_links(detail, detail_text)[: args.max_pdfs_per_page]:
                fetch_pdf(pdf)

    if args.deep:
        sections = {s.strip() for s in args.sections.split(",") if s.strip()}
        # Descobre as listagens por ano a partir das seeds e aprofunda nelas.
        # Só as seeds das seções interessam aqui: as páginas de servidor não
        # têm listagem por ano e cada fetch delas é tempo jogado fora.
        year_pages: list[str] = []
        for seed in SEED_PAGES[:2]:
            try:
                seed_text = fetch(seed).decode("utf-8", errors="replace")
            except Exception:
                continue
            for sub in subpage_links(seed, seed_text):
                if not is_year_list_page(sub) or sub in year_pages:
                    continue
                if sections and urlparse(sub).path.split("/")[1] not in sections:
                    continue
                year_pages.append(sub)
        for yp in year_pages:
            if pdf_count >= args.max_pdfs:
                print(f"\n[teto] {args.max_pdfs} PDFs atingido; parando.")
                break
            process_deep(yp)
    else:
        for page in SEED_PAGES:
            process_page(page, follow=True)

    print(f"\n{len(uploads)} documento(s) para {('listar' if args.dry_run else 'enviar')}:")
    for key, body, _ in uploads:
        print(f"  s3://{args.bucket}/{key}  ({len(body):,} bytes)")

    if args.dry_run:
        return 0

    import boto3  # importado tarde para permitir --dry-run sem AWS

    s3 = boto3.client("s3", region_name=args.region)
    for key, body, content_type in uploads:
        s3.put_object(Bucket=args.bucket, Key=key, Body=body, ContentType=content_type)
        print(f"  enviado: {key}")
    print("\nUpload concluido; a notificacao S3 aciona o Ingestor automaticamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
