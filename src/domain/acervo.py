"""Decisões do painel de acervo — lógica pura, sem AWS e sem rede.

O painel deixa um administrador da PROGEP acrescentar, revisar e remover
documentos da Base_de_Conhecimento sem passar por nós. Este módulo concentra o
que dá para decidir sem I/O; o handler é o adaptador fino.

**A regra central é o bucket de preparo.** O envio cai em ``preparo/`` no bucket
de staging, que **nenhum data source varre**. Confirmar é copiar para o bucket
de documentos; até lá, descartar é apagar algo que nunca entrou na base. Isso
torna a revisão real em vez de um aviso na tela — e a separação é por *bucket*
porque ``inclusionPrefixes`` aceita no máximo um prefixo por data source
(:mod:`src.domain.config`).

**Exclusão é reversível.** Remover move o objeto para ``removidos/`` e tira o
documento da base pela API. Um clique que apaga norma institucional em
definitivo é perigoso demais — e a origem mais comum de arrependimento é
justamente a limpeza de "documento antigo".
"""

from __future__ import annotations

import posixpath
import unicodedata
from dataclasses import dataclass

from src.domain.config import (
    MAX_DOC_SIZE_BYTES,
    PAINEL_PREFIX,
    PREPARO_PREFIX,
    REMOVIDOS_PREFIX,
    SUPPORTED_CONTENT_TYPES,
    DocumentSource,
)

#: Grupo do Cognito que autoriza alterar o acervo. A associação é **manual**, de
#: propósito: quem pode acrescentar norma ao acervo institucional é decisão da
#: PROGEP, não consequência de ter um e-mail do domínio.
GRUPO_ADMIN = "administradores"

#: Título de exibição do documento. Curto porque vai na lista de fontes ao pé da
#: resposta no WhatsApp, onde uma linha longa compete com a resposta.
MAX_TITULO = 120


class NaoAutorizado(PermissionError):
    """Quem não está no grupo não altera o acervo."""


class PedidoInvalido(ValueError):
    """Entrada malformada, com motivo legível para quem está na tela."""


def exige_admin(grupos: list[str] | tuple[str, ...] | None) -> None:
    """A interface esconde a aba; **isto** é o controle.

    O token já foi validado pelo authorizer do API Gateway antes de chegar aqui,
    mas validar token responde "quem é", não "pode". A conferência de grupo é o
    que separa um servidor comum de quem altera o acervo institucional.
    """
    if GRUPO_ADMIN not in (grupos or ()):
        raise NaoAutorizado(
            "Apenas administradores podem alterar o acervo da PROGEP."
        )


def extensao(nome: str) -> str:
    """Extensão em minúsculas, sem ponto; ``""`` quando não há."""
    base = nome.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].strip().lower() if "." in base else ""


def nome_seguro(nome: str) -> str:
    """Reduz o nome enviado ao que pode virar chave de S3 sem surpresa.

    **Preserva acentos e espaços**: o acervo inteiro tem nomes assim
    (``Resolução CS 348.pdf``), e é do nome que sai o rótulo da fonte citada na
    resposta. O que é removido é o que muda o **caminho** — barra, ``..`` e
    caracteres de controle. Um nome como ``../../site/x.pdf`` escreveria fora da
    área de preparo, num prefixo que a base varre.
    """
    nome = posixpath.basename(str(nome).replace("\\", "/")).strip()
    nome = "".join(c for c in nome if unicodedata.category(c)[0] != "C")
    nome = nome.replace("..", ".")
    if not nome or nome.startswith("."):
        raise PedidoInvalido("Nome de arquivo inválido.")
    if len(nome) > 200:
        raise PedidoInvalido("Nome de arquivo muito longo (máximo 200 caracteres).")
    ext = extensao(nome)
    if ext not in SUPPORTED_CONTENT_TYPES:
        aceitos = ", ".join(sorted(SUPPORTED_CONTENT_TYPES))
        raise PedidoInvalido(f"Formato {ext or 'sem extensão'!r} não aceito ({aceitos}).")
    return nome


def validar_envio(corpo: dict) -> tuple[str, int]:
    """Confere o pedido de upload antes de gerar a URL pré-assinada.

    O teto é o mesmo da ingestão (:data:`MAX_DOC_SIZE_BYTES`): recusar aqui
    poupa o envio de um arquivo que a base descartaria em silêncio depois.
    """
    if not isinstance(corpo, dict):
        raise PedidoInvalido("Corpo do pedido deve ser um objeto JSON.")

    nome = nome_seguro(corpo.get("nome", ""))

    try:
        tamanho = int(corpo.get("tamanho", 0))
    except (TypeError, ValueError):
        raise PedidoInvalido("'tamanho' deve ser um número de bytes.")
    if tamanho <= 0:
        raise PedidoInvalido("Arquivo vazio.")
    if tamanho > MAX_DOC_SIZE_BYTES:
        raise PedidoInvalido(
            f"Arquivo de {tamanho / 1024 / 1024:.0f} MB excede o limite de "
            f"{MAX_DOC_SIZE_BYTES // 1024 // 1024} MB."
        )
    return nome, tamanho


def validar_titulo(valor: object) -> str | None:
    """Título declarado por quem envia, ou ``None`` quando não informado.

    **Existe porque inferir título de nome de arquivo é chute.** Hoje o rótulo
    da fonte sai da chave do S3, e é por isso que a resposta cita
    "CS 348 Aprova a reformulao do regulamento dos adicionais ocupacionais".
    Quem envia sabe o que o documento é — perguntar custa um campo.

    Vira metadado de objeto no S3, que trafega como cabeçalho HTTP: quebra de
    linha aqui permitiria injetar cabeçalho na requisição de cópia. Por isso a
    conferência vem **antes** da normalização — ``" ".join(split())`` já engole
    a quebra de linha, e checar depois deixaria a verificação sempre verdadeira.
    """
    if valor is None:
        return None
    bruto = str(valor)
    # ``\r`` e ``\n`` são citados à parte porque são **exatamente** o vetor de
    # injeção de cabeçalho — e escapariam de um teste genérico de "caractere de
    # controle que não é espaço", já que ambos são espaço em branco. Deixá-los
    # para a normalização engolir funcionaria hoje, mas faria a proteção
    # depender de um detalhe de outra linha.
    if any(
        c in "\r\n" or (unicodedata.category(c)[0] == "C" and not c.isspace())
        for c in bruto
    ):
        raise PedidoInvalido("Título com caracteres inválidos.")
    texto = " ".join(bruto.split())
    if not texto:
        return None
    if len(texto) > MAX_TITULO:
        raise PedidoInvalido(f"Título muito longo (máximo {MAX_TITULO} caracteres).")
    return texto


def chave_de_preparo(sub: str, nome: str) -> str:
    """Cada administrador prepara na sua própria pasta.

    Sem separação por usuário, dois administradores enviando o mesmo nome
    sobrescreveriam um ao outro — e um veria na lista o arquivo do outro,
    podendo confirmá-lo sem saber o que é.
    """
    if not sub:
        raise PedidoInvalido("Usuário não identificado.")
    return f"{PREPARO_PREFIX}{sub}/{nome}"


def chave_publicada(nome: str) -> str:
    """Onde o documento fica depois de confirmado.

    Prefixo próprio do painel dentro de ``cliente/``: separa o que foi curado
    aqui da carga inicial do repositório, sem inventar uma terceira origem.
    """
    return f"{PAINEL_PREFIX}{nome}"


def chave_removida(chave: str) -> str:
    """Destino na lixeira, preservando o caminho original.

    Manter o caminho é o que permite desfazer: sem ele, um documento de
    ``cliente/DAP/`` e outro de ``site/`` com o mesmo nome colidiriam na
    lixeira, e não haveria como saber de onde cada um veio.
    """
    return f"{REMOVIDOS_PREFIX}{chave}"


# --- Procedência ---------------------------------------------------------------
# O painel mostra tudo o que está na base, mas nem tudo se comporta igual quando
# apagado: `site/` é regenerado pelo crawler e voltaria no próximo `ingest_site`.
# Esconder isso da tela seria prometer um controle que ela não tem.

#: Prefixos gerados por script, que voltam se a origem for reprocessada.
_PREFIXOS_GERADOS = (
    f"{DocumentSource.SITE.value}/",
)


@dataclass(frozen=True)
class Procedencia:
    """De onde o documento veio e o que acontece se for removido."""

    origem: str
    regenerave: bool
    aviso: str


def procedencia(chave: str) -> Procedencia:
    """Classifica um documento da base pela chave do S3."""
    if chave.startswith(PAINEL_PREFIX):
        return Procedencia("painel", False, "")
    if chave.startswith(_PREFIXOS_GERADOS):
        return Procedencia(
            "site institucional",
            True,
            "Publicado no site da PROGEP: volta na próxima varredura, "
            "a menos que entre na lista de exclusão.",
        )
    if chave.startswith(f"{DocumentSource.CLIENTE.value}/"):
        return Procedencia("repositório da PROGEP", False, "")
    return Procedencia("desconhecida", False, "")


def validar_chave_do_acervo(chave: str) -> str:
    """Confere uma chave vinda do cliente antes de qualquer operação nela.

    **Esta função é o controle de acesso da remoção**, não uma conveniência: o
    papel da Lambda alcança o bucket inteiro, então uma chave sem validação
    permitiria apagar qualquer objeto — inclusive fora do acervo.
    """
    chave = str(chave or "").strip().lstrip("/")
    if not chave:
        raise PedidoInvalido("Documento não informado.")
    if chave.startswith("s3://"):
        chave = chave[5:].partition("/")[2]
    if ".." in chave or any(unicodedata.category(c)[0] == "C" for c in chave):
        raise PedidoInvalido("Caminho de documento inválido.")
    indexaveis = tuple(f"{s.value}/" for s in DocumentSource)
    if not chave.startswith(indexaveis):
        raise PedidoInvalido("Documento fora do acervo da PROGEP.")
    if chave.endswith("/"):
        raise PedidoInvalido("Caminho de documento inválido.")
    return chave
