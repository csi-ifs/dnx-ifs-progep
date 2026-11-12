"""Nome legível das fontes citadas na resposta (Req 2.3).

O ``doc_id`` de cada fonte é a chave do objeto no S3
(``s3://progep-ifs-documents/cliente/DAP/05_Portaria_Conjunta_6_2022_Licenca_Capacitacao.pdf``).
Mostrar isso ao servidor não ajuda ninguém; mostrar *"Portaria Conjunta 6 2022 —
Licença Capacitação"* permite conferir a resposta contra o documento oficial.

**Por que derivar do nome do arquivo e não pedir a citação ao modelo:** um LLM
pede-se para citar e ele inventa o número da portaria com a mesma fluência com
que acerta. Aqui a origem é a chave do S3, que nós controlamos — não há como
alucinar uma fonte que não foi usada.

Lógica pura: recebe os identificadores, devolve rótulos.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

#: Prefixos de caminho que só dizem onde o arquivo mora, não o que ele é.
_RUIDO_CAMINHO = ("site", "cliente", "images", "arquivos", "documentos", "ocr")

#: O portal da PROGEP grava acento no nome do arquivo como ``-C3-A7`` em vez de
#: ``%C3%A7``, então ``unquote`` não desfaz. Sem tratar, o servidor lê
#: *"MANIFESTA C3 87 C3 83O T C3 89CNICA PDP 2020"* na lista de fontes da
#: resposta — 17 dos 407 documentos do acervo têm nome assim.
#:
#: O casamento exige uma sequência UTF-8 **válida** (byte inicial ``C2``–``F4``
#: seguido de continuações ``80``–``BF``). Sem essa restrição, o ``-20`` de um
#: nome como "Edital 01-2024" seria lido como byte e viraria caractere solto.
_ACENTO_DO_PORTAL = re.compile(r"-([C-F][0-9A-F])((?:-[89AB][0-9A-F])+)")


def decodificar_nome(nome: str) -> str:
    """Desfaz a codificação de acento usada nos nomes de arquivo do portal."""

    def troca(m: re.Match[str]) -> str:
        partes = [m.group(1), *m.group(2).split("-")[1:]]
        return unquote("".join(f"%{b}" for b in partes))

    return _ACENTO_DO_PORTAL.sub(troca, nome)

#: Sufixos numéricos de versão que o site usa (``..._1.pdf``, ``... (3).pdf``).
#: O separador é **obrigatório**: sem ele, ``D10835`` (um decreto) perderia os
#: dois últimos dígitos e viraria "D108".
_SUFIXO_VERSAO = re.compile(r"(?:[\s_-]+\d{1,2}|\s*\(\d{1,2}\))$")


#: Comprimido, o nome de arquivo esconde a norma: ``L8112compilado`` é a Lei
#: 8.112 e ``D10835`` é um decreto. São padrões do próprio repositório.
_ABREVIACOES = (
    (re.compile(r"^L(\d{4,5}).*", re.IGNORECASE), r"Lei \1"),
    (re.compile(r"^D(\d{4,5}).*", re.IGNORECASE), r"Decreto \1"),
    (re.compile(r"^EC (\d{1,3})\b.*", re.IGNORECASE), r"Emenda Constitucional \1"),
)

#: Rótulo longo demais compete com a resposta em vez de apoiá-la.
_MAX_ROTULO = 72


def rotulo_da_fonte(doc_id: str) -> str:
    """Converte a chave de um documento no nome pelo qual ele é conhecido."""
    if not doc_id:
        return ""
    caminho = unquote(doc_id)
    # Transcrição de fluxograma: o leitor precisa saber que a fonte é o desenho
    # do processo (e que está pendente de validação), não uma norma.
    e_fluxo = "mapeamento-processos" in caminho
    partes = caminho.rstrip("/").split("/")
    nome = partes[-1]
    # FAQ dividido em um arquivo por pergunta (`.../faq/<nome do FAQ>/09-….md`,
    # ver src/domain/faq.py): a fonte é o FAQ, não o arquivo da pergunta.
    if len(partes) >= 3 and partes[-3] == "faq":
        nome = partes[-2]
    # Antes de qualquer limpeza: os hifens da codificação viram espaço logo
    # abaixo, e aí não haveria mais como distinguir ``-C3-A7`` de texto.
    nome = decodificar_nome(nome)
    nome = re.sub(r"\.(pdf|docx?|html?|md|txt)$", "", nome, flags=re.IGNORECASE)
    # Resto da ferramenta de compressão de PDF ("Cartilha20IFS.compressed.pdf").
    nome = re.sub(r"\.compressed$", "", nome, flags=re.IGNORECASE)
    # ``%20`` que perdeu o ``%`` no upload: "Cartilha20IFS" é "Cartilha IFS". Só
    # entre minúscula e maiúscula, para não tocar em "Edital 01-2024" nem em
    # números de norma.
    nome = re.sub(r"(?<=[a-z])20(?=[A-Z])", " ", nome)
    # Numeração de ordenação do repositório do cliente ("05_Portaria...").
    nome = re.sub(r"^\d{1,2}[_-]", "", nome)
    nome = nome.replace("_", " ").replace("-", " ")
    nome = re.sub(r"\s+", " ", nome).strip()
    nome = _SUFIXO_VERSAO.sub("", nome).strip()
    if nome.lower() in _RUIDO_CAMINHO or not nome:
        return ""

    for padrao, substituto in _ABREVIACOES:
        if padrao.match(nome):
            nome = padrao.sub(substituto, nome)
            break

    nome = nome[0].upper() + nome[1:] if nome else nome
    if len(nome) > _MAX_ROTULO:
        nome = nome[:_MAX_ROTULO].rstrip() + "…"
    return f"Fluxo do processo: {nome}" if e_fluxo else nome


def formatar_fontes(doc_ids: tuple[str, ...] | list[str], *, maximo: int = 3) -> str:
    """Bloco de texto com as fontes, ou ``""`` quando não há o que citar.

    Limita a ``maximo`` itens: numa mensagem de WhatsApp, uma lista longa de
    documentos compete com a resposta em vez de apoiá-la.
    """
    rotulos: list[str] = []
    for doc_id in doc_ids:
        rotulo = rotulo_da_fonte(doc_id)
        if rotulo and rotulo not in rotulos:
            rotulos.append(rotulo)
        if len(rotulos) >= maximo:
            break
    if not rotulos:
        return ""
    itens = "\n".join(f"• {r}" for r in rotulos)
    return f"\n\n_Fontes consultadas:_\n{itens}"
