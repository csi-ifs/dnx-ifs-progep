"""Divisão de FAQs em um documento por pergunta — lógica pura, sem I/O.

**Por que existe.** A base de conhecimento divide cada arquivo em trechos de
tamanho fixo (pai de ~1.500 tokens, :data:`src.infra.resources.CHUNKING_HIERARQUICO`),
sem saber onde uma pergunta começa. Num FAQ de 28 mil caracteres, o corte caía
no meio de uma resposta: no FAQ de estágio probatório, "Quem é responsável por
avaliar o servidor?" ficou com a parte dos nomeados a partir de 07/02/2025 num
trecho e a dos nomeados até 06/02/2025 no seguinte — e o assistente respondia
só metade.

Um arquivo por pergunta faz cada resposta caber num trecho só, inteira, com o
título do FAQ e a pergunta no topo (o que também melhora a busca: a resposta
sozinha muitas vezes nem diz "estágio probatório").

**Resposta maior que um trecho** é dividida em partes nos parágrafos, e cada
parte repete o título e a pergunta — perder o vínculo entre a pergunta e a
continuação da resposta é justamente o defeito que este módulo corrige.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

#: Teto de caracteres por arquivo. O trecho-pai tem ~1.500 tokens; em português
#: isso dá uns 5.000–6.000 caracteres. A folga cobre o cabeçalho e a variação
#: do tokenizador — passar do teto faria a própria resposta ser cortada.
MAX_CARACTERES = 4000

#: Onde começa uma pergunta: "9. ", "2 - ", "3) " ou "Pergunta:".
_MARCADOR = re.compile(
    r"^\s*(?:(?P<num>\d{1,3})\s*[.)\-–]\s+|Pergunta\s*:\s*)(?P<resto>\S.*)$",
    re.IGNORECASE,
)

#: Quantas linhas uma pergunta pode ocupar (PDF quebra a linha no meio dela).
_MAX_LINHAS_PERGUNTA = 5

#: Abaixo disso, o texto antes da primeira pergunta é cabeçalho de página, não
#: apresentação que valha um arquivo.
_MIN_APRESENTACAO = 200


@dataclass(frozen=True)
class Pergunta:
    numero: int
    pergunta: str
    resposta: str


def _pergunta_em(linhas: list[str], i: int, esperado: int | None) -> tuple[str, int] | None:
    """Se a linha ``i`` abre uma pergunta, devolve (texto, linhas consumidas).

    Exige que a pergunta termine em "?" em até :data:`_MAX_LINHAS_PERGUNTA`
    linhas e, quando numerada, que o número seja o próximo da sequência: é o
    que separa "3. Qual a carga horária?" de um inciso "3. prazo de 30 dias"
    dentro de uma resposta.
    """
    m = _MARCADOR.match(linhas[i])
    if not m:
        return None
    numerada = m.group("num") is not None
    # Aceita pular um número: o FAQ da Perícia vai do 14 ao 16.
    if numerada and esperado is not None and not esperado <= int(m.group("num")) <= esperado + 1:
        return None
    partes = [m.group("resto").strip()]
    j = i
    while True:
        if partes[-1].endswith("?"):
            return _texto_da_pergunta(partes), j - i + 1
        j += 1
        # Conta só linhas com texto: o PDF põe linha em branco (ou um marcador
        # de lista solto) entre o tema e a "Pergunta:" logo abaixo.
        while j < len(linhas) and not re.search(r"\w", linhas[j]):
            j += 1
        if (
            j >= len(linhas)
            or len(partes) >= _MAX_LINHAS_PERGUNTA
            # o próximo item numerado começou: esta pergunta não termina em "?"
            # ("Pergunta:" logo abaixo do tema é o mesmo item, não o próximo)
            or ((mm := _MARCADOR.match(linhas[j])) and mm.group("num") is not None)
            or partes[-1].endswith((".", ";"))
        ):
            break
        partes.append(linhas[j].strip())
    # Sem "?": item numerado com título afirmativo ("9 - Atestado devolvido sem
    # ação do servidor") vale como pergunta, com a primeira linha de título.
    # Inciso de lista dentro de uma resposta também é "N. texto" e pode até ter
    # o número certo — por isso o título curto ou logo depois de "São eles:"
    # não vale.
    anterior = next((l.strip() for l in reversed(linhas[:i]) if l.strip()), "")
    if (
        numerada
        and esperado is not None
        and len(partes[0].split()) >= 4
        and not anterior.endswith((":", ";"))
    ):
        return _texto_da_pergunta(partes[:1]), 1
    return None


def _texto_da_pergunta(partes: list[str]) -> str:
    """Junta as linhas da pergunta; "Tema Pergunta: …?" vira "Tema — …?"."""
    texto = re.sub(r"\s+", " ", " ".join(partes)).strip()
    return re.sub(r"\s*\bPergunta\s*:\s*", " — ", texto).strip(" —")


def dividir(texto: str) -> tuple[str, list[Pergunta]]:
    """Separa o texto do FAQ em (apresentação, perguntas).

    Duas formas de marcar a pergunta, detectadas sozinhas: numerada ("9. …?",
    "2 - …?"), com a numeração em sequência a partir de 1; ou rotulada
    ("Pergunta: …?"). Devolve lista vazia quando não reconhece nenhuma — o
    chamador deve parar e avisar, não indexar um FAQ mal dividido.
    """
    linhas = [l.rstrip() for l in texto.replace("\r\n", "\n").split("\n")]
    inicios = [
        i for i, l in enumerate(linhas)
        if (m := _MARCADOR.match(l)) and m.group("num") == "1"
    ]
    if not inicios:
        return _dividir_desde(linhas, 0, numerado=False)
    # Um FAQ com sumário lista as perguntas duas vezes: no sumário, sem
    # resposta, e no corpo. Dividir a partir de cada "1" e ficar com a divisão
    # que tem mais respostas de verdade descarta o sumário (vira apresentação).
    melhor = max(
        (_dividir_desde(linhas, i, numerado=True) for i in inicios),
        key=lambda r: sum(len(p.resposta) >= 40 for p in r[1]),
    )
    return melhor


def _dividir_desde(
    linhas: list[str], inicio: int, *, numerado: bool
) -> tuple[str, list[Pergunta]]:
    perguntas: list[Pergunta] = []
    apresentacao: list[str] = list(linhas[:inicio])
    atual: list[str] | None = None
    pergunta_atual = ""
    ultimo = 0
    i = inicio
    while i < len(linhas):
        m = _MARCADOR.match(linhas[i])
        achado = None
        if m and (m.group("num") is not None) == numerado:
            achado = _pergunta_em(linhas, i, ultimo + 1 if numerado else None)
        if achado:
            if atual is not None:
                perguntas.append(Pergunta(len(perguntas) + 1, pergunta_atual, _limpar(atual)))
            pergunta_atual, consumidas = achado
            if numerado:
                ultimo = int(m.group("num"))
            atual = []
            i += consumidas
            continue
        (atual if atual is not None else apresentacao).append(linhas[i])
        i += 1
    if atual is not None:
        perguntas.append(Pergunta(len(perguntas) + 1, pergunta_atual, _limpar(atual)))
    intro = _limpar(apresentacao)
    return (intro if len(intro) >= _MIN_APRESENTACAO else ""), perguntas


def _limpar(linhas: list[str]) -> str:
    """Junta as linhas, tirando espaço sobrando e linhas em branco repetidas."""
    texto = "\n".join(l.strip() for l in linhas)
    texto = re.sub(r"[ \t]+", " ", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def _partes(resposta: str, limite: int) -> list[str]:
    """Divide uma resposta longa em partes de até ``limite``, nos parágrafos.

    Parágrafo maior que o limite (PDF sem linha em branco entre parágrafos)
    cai para a divisão por linha.
    """
    if len(resposta) <= limite:
        return [resposta]
    blocos = [b for b in re.split(r"\n\s*\n", resposta) if b.strip()]
    if len(blocos) == 1:
        blocos = [b for b in resposta.split("\n") if b.strip()]
    # Bloco que sozinho passa do limite é cortado nas frases — nunca no meio
    # de uma palavra, e nunca deixado inteiro (a base o cortaria às cegas).
    miudos: list[str] = []
    for bloco in blocos:
        if len(bloco) <= limite:
            miudos.append(bloco)
            continue
        frase_atual = ""
        for frase in re.split(r"(?<=[.;:])\s+", bloco):
            if frase_atual and len(frase_atual) + len(frase) + 1 > limite:
                miudos.append(frase_atual)
                frase_atual = ""
            frase_atual = f"{frase_atual} {frase}".strip()
        if frase_atual:
            miudos.append(frase_atual)
    blocos = miudos
    partes: list[str] = []
    atual = ""
    for bloco in blocos:
        if atual and len(atual) + len(bloco) + 2 > limite:
            partes.append(atual)
            atual = ""
        atual = f"{atual}\n\n{bloco}" if atual else bloco
    if atual:
        partes.append(atual)
    return partes


def slug(texto: str, maximo: int = 60) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", texto).strip("-")[:maximo].strip("-") or "pergunta"


def _e_sumario(apresentacao: str, perguntas: list[Pergunta]) -> bool:
    """A apresentação é só a lista das perguntas (sumário)?

    Sumário não tem resposta nenhuma, mas casa com a busca tão bem quanto as
    perguntas de verdade — ocuparia a vaga de um trecho útil.
    """
    texto = re.sub(r"\s+", " ", apresentacao).lower()
    citadas = sum(re.sub(r"\s+", " ", p.pergunta).lower()[:40] in texto for p in perguntas)
    return citadas >= min(3, len(perguntas))


def documentos(
    titulo: str,
    origem: str,
    texto: str,
    *,
    limite: int = MAX_CARACTERES,
    com_apresentacao: bool = True,
) -> list[tuple[str, str]]:
    """Os arquivos Markdown do FAQ: lista de (nome do arquivo, conteúdo).

    Cada arquivo traz o título do FAQ e a pergunta no topo, porque é o trecho
    inteiro que a busca compara e que o modelo lê — sem eles, a resposta
    "Para servidores nomeados a partir de 07/02/2025: …" não diz de que assunto
    trata.
    """
    apresentacao, perguntas = dividir(texto)
    if not perguntas:
        return []
    total = len(perguntas)
    saida: list[tuple[str, str]] = []
    cabecalho = f"# {titulo}\n\n**Fonte:** {origem}\n\n"
    if apresentacao and com_apresentacao and not _e_sumario(apresentacao, perguntas):
        for k, parte in enumerate(_partes(apresentacao, limite), 1):
            sufixo = f"-parte{k}" if k > 1 else ""
            saida.append((f"00-apresentacao{sufixo}.md",
                          f"{cabecalho}\n## Apresentação\n\n{parte}\n"))
    for p in perguntas:
        partes = _partes(p.resposta, limite)
        for k, parte in enumerate(partes, 1):
            continua = f" (parte {k} de {len(partes)})" if len(partes) > 1 else ""
            sufixo = f"-parte{k}" if len(partes) > 1 else ""
            nome = f"{p.numero:02d}-{slug(p.pergunta)}{sufixo}.md"
            corpo = (
                f"{cabecalho}**Pergunta {p.numero} de {total}**{continua}\n\n"
                f"## {p.pergunta}\n\n{parte}\n"
            )
            saida.append((nome, corpo))
    return saida


class _TextoDeHtml(HTMLParser):
    """Texto visível de uma página, uma linha por bloco; ignora script/estilo."""

    _BLOCOS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.partes: list[str] = []
        self._ignorar = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style", "noscript"):
            self._ignorar += 1
        elif tag in self._BLOCOS:
            self.partes.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._ignorar = max(0, self._ignorar - 1)
        elif tag in self._BLOCOS:
            self.partes.append("\n")

    def handle_data(self, dados: str) -> None:
        if not self._ignorar:
            self.partes.append(dados)


def texto_de_html(html: str) -> str:
    """Texto de uma página HTML, uma linha por bloco, sem script nem estilo."""
    leitor = _TextoDeHtml()
    leitor.feed(html)
    linhas = [" ".join(l.split()) for l in "".join(leitor.partes).split("\n")]
    return "\n".join(l for l in linhas if l)


# --- Reconhecer um FAQ no envio -------------------------------------------------

#: Mínimo de perguntas para dividir um documento que **se chama** FAQ.
MIN_PERGUNTAS_COM_NOME = 3

#: Sem "FAQ" no nome, só divide com mais perguntas e quase todas com "?": uma
#: norma com itens numerados ("1. Compete à PROGEP: ...") não pode virar FAQ.
MIN_PERGUNTAS_SEM_NOME = 5
MIN_FRACAO_INTERROGACAO = 0.7
#: ... e quase todas com resposta de verdade. Checklist ("Está no efetivo
#: exercício? ( ) sim ( ) não", anexo da Resolução CS 348) também é uma lista
#: numerada de perguntas, mas sem resposta escrita.
MIN_FRACAO_RESPONDIDAS = 0.8
MIN_RESPOSTA = 40

_NOME_DE_FAQ = re.compile(r"\bfaqs?\b|perguntas\s+frequentes", re.IGNORECASE)


def e_nome_de_faq(nome: str) -> bool:
    """O nome do arquivo diz que é um FAQ ("FAQ", "Perguntas Frequentes")?"""
    base = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return bool(_NOME_DE_FAQ.search(re.sub(r"[_\-.]+", " ", base)))


def e_faq(nome: str, perguntas: list[Pergunta]) -> bool:
    """O documento deve ser dividido por pergunta?"""
    if e_nome_de_faq(nome):
        return len(perguntas) >= MIN_PERGUNTAS_COM_NOME
    if len(perguntas) < MIN_PERGUNTAS_SEM_NOME:
        return False
    com_interrogacao = sum(p.pergunta.rstrip().endswith("?") for p in perguntas)
    respondidas = sum(len(p.resposta) >= MIN_RESPOSTA for p in perguntas)
    return (
        com_interrogacao / len(perguntas) >= MIN_FRACAO_INTERROGACAO
        and respondidas / len(perguntas) >= MIN_FRACAO_RESPONDIDAS
    )


def nome_do_faq(nome_arquivo: str) -> str:
    """``FAQ - ESTÁGIO PROBATÓRIO.pdf`` → ``FAQ - ESTÁGIO PROBATÓRIO`` (nome da pasta)."""
    nome = nome_arquivo.rsplit("/", 1)[-1]
    nome = nome.rsplit(".", 1)[0] if "." in nome else nome
    return " ".join(nome.split()).strip(". ") or "FAQ"


def pasta_do_faq(origem: str, nome_arquivo: str) -> str:
    """Onde ficam as perguntas: ``<origem>/faq/<nome do FAQ>/``.

    O rótulo da fonte (src/domain/fontes.py) e a conferência de cópias
    (src/domain/acervo.py) reconhecem essa forma — não mudar sem mudar lá.
    """
    return f"{origem}/faq/{nome_do_faq(nome_arquivo)}/"


def faq_do_documento(
    nome_arquivo: str, texto: str | None, origem: str, *, titulo: str | None = None,
    com_apresentacao: bool = True,
) -> list[tuple[str, str]]:
    """Os arquivos por pergunta, ou ``[]`` se o documento não é um FAQ."""
    if not texto:
        return []
    _, perguntas = dividir(texto)
    if not e_faq(nome_arquivo, perguntas):
        return []
    return documentos(
        titulo or nome_do_faq(nome_arquivo), f"{origem} — perguntas frequentes da PROGEP/IFS",
        texto, com_apresentacao=com_apresentacao,
    )
