"""Texto de um documento do acervo, para reconhecer e dividir FAQs.

Roda dentro da Lambda do painel, então só usa o que existe lá: a biblioteca
padrão (DOCX é um ZIP com XML; HTML pelo ``html.parser``) e o ``pypdf`` da
camada ``progep-ifs-pypdf`` (Python puro). Fora da Lambda, o mesmo código serve
ao ``scripts/dividir_faq.py`` — a divisão sai igual nos dois caminhos.

Nunca levanta exceção por arquivo ruim: devolve ``None``, e quem chama indexa o
documento inteiro, como antes. Não reconhecer um FAQ não pode impedir o envio.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from src.domain.faq import texto_de_html

logger = logging.getLogger(__name__)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _texto_de_docx(dados: bytes) -> str:
    """Um parágrafo por linha, como o python-docx devolveria."""
    with zipfile.ZipFile(io.BytesIO(dados)) as pacote:
        raiz = ElementTree.fromstring(pacote.read("word/document.xml"))
    linhas = []
    for paragrafo in raiz.iter(f"{_W}p"):
        partes = []
        for no in paragrafo.iter():
            if no.tag == f"{_W}t" and no.text:
                partes.append(no.text)
            elif no.tag in (f"{_W}tab",):
                partes.append(" ")
            elif no.tag in (f"{_W}br", f"{_W}cr"):
                partes.append("\n")
        linhas.append("".join(partes))
    return "\n".join(linhas)


class TempoEsgotado(Exception):
    """A leitura passou do prazo (o painel tem 29 s para responder)."""


def _texto_de_pdf(dados: bytes, prazo_s: float | None) -> str:
    import time

    from pypdf import PdfReader

    limite = time.monotonic() + prazo_s if prazo_s else None
    partes = []
    for pagina in PdfReader(io.BytesIO(dados)).pages:
        partes.append(pagina.extract_text() or "")
        if limite and time.monotonic() > limite:
            raise TempoEsgotado
    return "\n".join(partes)


def extrair_texto(nome: str, dados: bytes, *, prazo_s: float | None = None) -> str | None:
    """Texto do documento, ou ``None`` se o formato não é lido ou o arquivo falha.

    ``prazo_s`` limita a leitura de PDF: o pypdf leva dezenas de segundos em
    alguns PDFs de centenas de páginas (medido no acervo: 2,3 MB, mais de 50 s).
    """
    ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
    try:
        if ext == "pdf":
            texto = _texto_de_pdf(dados, prazo_s)
        elif ext == "docx":
            texto = _texto_de_docx(dados)
        elif ext in ("html", "htm"):
            texto = texto_de_html(dados.decode("utf-8", errors="replace"))
        elif ext in ("md", "markdown", "txt"):
            texto = dados.decode("utf-8", errors="replace")
        else:
            return None
    except TempoEsgotado:
        logger.warning("Leitura de %s passou de %s s; segue sem dividir", nome, prazo_s)
        return None
    except Exception:  # noqa: BLE001 - arquivo corrompido/protegido: indexa inteiro
        logger.warning("Não foi possível ler o texto de %s", nome, exc_info=True)
        return None
    return texto if re.search(r"\w", texto or "") else None
