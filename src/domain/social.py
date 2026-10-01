"""Reconhecimento de mensagens sociais (Req 6.2).

Numa conversa de WhatsApp o usuário escreve muito mais que perguntas:
"bom dia", "obrigado", "ok", "tchau". Mandar isso para a Base_de_Conhecimento
gera o pior resultado possível — nenhum documento fala sobre agradecimento,
então o assistente responde *"não encontrei essa informação nos documentos
oficiais"* a quem só disse "obrigado".

Este módulo é lógica pura: decide se a mensagem é social e qual o tipo. A
resposta correspondente e o efeito no ciclo de vida da conversa (uma despedida
encerra na hora, sem esperar a janela de inatividade) ficam com o adapter.

**A regra de ouro é não capturar demais.** "Obrigado, mas como faço para pedir
férias?" é pergunta, não agradecimento. Por isso o casamento exige a mensagem
**inteira** ser social — não basta conter a palavra.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum


class TipoSocial(str, Enum):
    """Categoria de mensagem social reconhecida."""

    SAUDACAO = "saudacao"
    AGRADECIMENTO = "agradecimento"
    DESPEDIDA = "despedida"
    CONFIRMACAO = "confirmacao"


#: Formas normalizadas (sem acento, minúsculas, sem pontuação) por tipo. Cada
#: entrada precisa valer como mensagem inteira.
_EXPRESSOES: dict[TipoSocial, frozenset[str]] = {
    TipoSocial.SAUDACAO: frozenset({
        "oi", "ola", "opa", "eai", "e ai", "bom dia", "boa tarde", "boa noite",
        "oi bom dia", "ola bom dia", "oi boa tarde", "ola boa tarde",
        "oi boa noite", "ola boa noite", "oi tudo bem", "ola tudo bem",
        "tudo bem", "tudo bom", "oie", "hey", "hi", "salve",
    }),
    TipoSocial.AGRADECIMENTO: frozenset({
        "obrigado", "obrigada", "obg", "vlw", "valeu", "muito obrigado",
        "muito obrigada", "obrigado mesmo", "agradeco", "agradecido",
        "grato", "grata", "show", "perfeito", "otimo", "excelente",
        "obrigado pela ajuda", "obrigada pela ajuda", "thanks", "tks",
    }),
    TipoSocial.DESPEDIDA: frozenset({
        "tchau", "ate logo", "ate mais", "adeus", "falou", "flw",
        "bom trabalho", "ate breve", "obrigado tchau", "obrigada tchau",
        "so isso", "era so isso", "e so isso", "nada mais", "por enquanto e so",
        # Visto em 25/09/2026: "não tenho mais dúvidas" foi para a busca e
        # voltou "não encontrei", com oferta de atendente no fim da conversa.
        "nao tenho mais duvidas", "nao tenho mais duvida", "sem mais duvidas",
        "era so isso mesmo", "so isso mesmo", "nao preciso de mais nada",
    }),
    TipoSocial.CONFIRMACAO: frozenset({
        "ok", "okay", "certo", "entendi", "beleza", "blz", "ta bom", "tabom",
        "sim", "isso", "isso mesmo", "compreendi", "entendido", "ciente",
        "ah ta", "ata", "uhum", "certo obrigado",
    }),
}

#: Acima disso a mensagem tem conteúdo demais para ser só cortesia, mesmo que
#: comece com uma. Evita engolir "ok, e quanto tempo demora o processo?".
_MAX_CARACTERES = 40


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento, sem pontuação e sem emoji."""
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    # Mantém apenas letras, números e espaço — descarta pontuação e emoji.
    limpo = re.sub(r"[^a-zA-Z0-9\s]", " ", sem_acento.lower())
    return re.sub(r"\s+", " ", limpo).strip()


#: Palavras de ligação que só costuram cortesias ("entendi, **então** obrigada").
#: Não carregam conteúdo, então não impedem a classificação.
_CONECTORES = frozenset({
    "e", "ok", "entao", "ai", "ja", "mas", "bem", "muito", "ta",
    "mesmo", "voce", "vc", "por", "tudo", "pela", "pelo", "ajuda", "sua",
})

#: Quando a mensagem combina tipos ("entendi, obrigada"), vale o mais forte: uma
#: despedida encerra, um agradecimento fecha o assunto, e assim por diante.
_PRIORIDADE = (
    TipoSocial.DESPEDIDA,
    TipoSocial.AGRADECIMENTO,
    TipoSocial.SAUDACAO,
    TipoSocial.CONFIRMACAO,
)

#: Maior número de palavras de uma expressão conhecida, para o casamento guloso.
_MAX_PALAVRAS = max(
    len(e.split()) for expressoes in _EXPRESSOES.values() for e in expressoes
)


def _tipo_da_expressao(expressao: str) -> TipoSocial | None:
    for tipo, expressoes in _EXPRESSOES.items():
        if expressao in expressoes:
            return tipo
    return None


def classificar_social(texto: str) -> TipoSocial | None:
    """Tipo da mensagem social, ou ``None`` quando é uma pergunta de verdade.

    A mensagem precisa ser **inteiramente** composta de cortesias: "entendi,
    obrigada" é social (duas expressões emendadas), mas "obrigado, mas como faço
    para pedir férias?" não é — sobra conteúdo que ninguém mais responderia.

    Quando há mais de um tipo, vale o de maior prioridade: em "obrigado, tchau"
    o que importa é a despedida, porque ela encerra a conversa.
    """
    if not texto or len(texto.strip()) > _MAX_CARACTERES:
        return None
    palavras = _normalizar(texto).split()
    if not palavras:
        return None

    encontrados: set[TipoSocial] = set()
    posicao = 0
    while posicao < len(palavras):
        # Casamento guloso: tenta a expressão mais longa possível a partir daqui,
        # para "bom dia" não ser lido como "bom" + "dia".
        for tamanho in range(min(_MAX_PALAVRAS, len(palavras) - posicao), 0, -1):
            tipo = _tipo_da_expressao(" ".join(palavras[posicao : posicao + tamanho]))
            if tipo is not None:
                encontrados.add(tipo)
                posicao += tamanho
                break
        else:
            if palavras[posicao] in _CONECTORES:
                posicao += 1
                continue
            return None  # sobrou conteúdo: é pergunta, não cortesia

    for tipo in _PRIORIDADE:
        if tipo in encontrados:
            return tipo
    return None
