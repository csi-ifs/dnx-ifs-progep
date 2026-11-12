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
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

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


# --- Cópias ---------------------------------------------------------------------
# Cópia idêntica não é inofensiva: a busca devolve os N pedaços mais parecidos, e
# dois arquivos iguais ocupam o dobro das vagas com o mesmo texto, empurrando para
# fora o trecho de outro documento que completaria a resposta.

#: Transcrições de PDFs sem camada de texto. Têm o mesmo nome do PDF **de
#: propósito** — o PDF entra vazio na base e a transcrição é o que responde —,
#: então não contam como cópia na listagem de duplicatas.
PREFIXO_TRANSCRICOES = f"{DocumentSource.CLIENTE.value}/ocr/"

_EXTENSOES = r"\.(pdf|docx?|txt|html?|md)"


def nome_base(chave: str) -> str:
    """Nome comparável entre formatos: sem pasta, extensão, acento nem pontuação.

    ``cliente/DNAGS/FAQ_NAPA.docx`` e ``cliente/painel/FAQ NAPA.pdf`` viram o
    mesmo ``faq napa``. O sufixo ``(1)`` do navegador e a extensão repetida
    (``x.docx (1).pdf``) também saem: é assim que chega a cópia baixada duas
    vezes.
    """
    nome = chave.rstrip("/").rsplit("/", 1)[-1]
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = nome.lower()
    for _ in range(3):  # "x.docx (1).pdf" → "x.docx (1)" → "x.docx" → "x"
        anterior = nome
        nome = re.sub(rf"{_EXTENSOES}$", "", nome).strip()
        nome = re.sub(r"\(\d+\)$", "", nome).strip()
        if nome == anterior:
            break
    return re.sub(r"[^a-z0-9]+", " ", nome).strip()


def unidade(chave: str) -> str:
    """O documento a que a chave pertence, para comparar nomes.

    FAQ dividido por pergunta (``…/faq/<nome do FAQ>/09-….md``, ver
    src/domain/faq.py) é **um** documento: a pasta. Sem isso, as duas versões
    do FAQ NAPA viravam 19 pares "mesmo nome", um por pergunta, e o envio de uma
    versão nova do FAQ não perceberia que ele já está na base, dividido.
    """
    partes = chave.split("/")
    if len(partes) >= 4 and partes[-3] == "faq":
        return "/".join(partes[:-1]) + "/"
    return chave


def conteudo_hash(etag: str | None) -> str | None:
    """MD5 do conteúdo a partir do ETag, ou ``None`` quando o ETag não é o MD5.

    Envio de parte única (o POST pré-assinado, o ``put_object`` dos scripts, a
    cópia entre buckets) tem ETag igual ao MD5. O de multipart termina em
    ``-N`` e não serve para comparar conteúdo.
    """
    valor = str(etag or "").strip().strip('"').lower()
    if not valor or "-" in valor:
        return None
    return valor


@dataclass(frozen=True)
class Objeto:
    """O mínimo de um objeto do acervo para comparar: chave e ETag."""

    chave: str
    etag: str | None = None


@dataclass
class Conflitos:
    """O que já existe no acervo parecido com o documento que vai entrar."""

    identicos: list[str] = field(default_factory=list)
    mesmo_nome: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.identicos or self.mesmo_nome)


def conflitos_do_envio(
    nome: str, etag: str | None, acervo: Iterable[Objeto], destino: str
) -> Conflitos:
    """Compara o envio com o acervo: mesmo conteúdo e mesmo nome-base.

    O ``destino`` (a chave que o envio vai ocupar) conta como "mesmo nome" se já
    existir: confirmar sobrescreveria o arquivo anterior, e isso merece a mesma
    pergunta que um nome parecido em outra pasta.
    """
    alvo_hash = conteudo_hash(etag)
    alvo_nome = nome_base(nome)
    achados = Conflitos()
    for obj in acervo:
        # Marcador de "pasta" do S3 não é documento; a pasta de um FAQ dividido
        # (o original guardado, ver `pasta_da_original`) é.
        if obj.chave.endswith("/") and not e_pasta_de_faq(obj.chave):
            continue
        if alvo_hash and conteudo_hash(obj.etag) == alvo_hash:
            achados.identicos.append(obj.chave)
        elif obj.chave == destino or (
            alvo_nome and nome_base(unidade(obj.chave)) == alvo_nome
        ):
            achados.mesmo_nome.append(obj.chave)
    return achados


class ConflitoNoAcervo(Exception):
    """O envio esbarra em algo que já está no acervo; a tela decide o que fazer."""

    def __init__(self, mensagem: str, conflitos: Conflitos, *, pode_prosseguir: bool):
        super().__init__(mensagem)
        self.conflitos = conflitos
        self.pode_prosseguir = pode_prosseguir

    def como_dict(self) -> dict:
        return {
            "erro": str(self),
            "conflito": "identico" if self.conflitos.identicos else "mesmo_nome",
            "identicos": self.conflitos.identicos,
            "mesmo_nome": self.conflitos.mesmo_nome,
            "pode_prosseguir": self.pode_prosseguir,
        }


#: O que fazer quando o envio tem o mesmo nome de um documento do acervo.
ACOES_DE_CONFLITO = ("substituir", "manter_ambos")


def decidir_conflito(
    conflitos: Conflitos, acao: object, substituir: object
) -> list[str]:
    """Decide se o envio prossegue e devolve as chaves a mandar para a lixeira.

    * cópia idêntica: **não entra**, com ou sem ação — não há versão nova a
      guardar, só a mesma informação ocupando o dobro das vagas na busca;
    * mesmo nome, sem ação: pergunta à tela;
    * ``manter_ambos``: entra sem remover nada;
    * ``substituir``: entra e as chaves escolhidas vão para a lixeira. Só valem
      chaves que **estão na lista de conflitos** — a lista vinda da tela não pode
      virar um jeito de remover qualquer documento do acervo.
    """
    if conflitos.identicos:
        lugares = ", ".join(conflitos.identicos)
        raise ConflitoNoAcervo(
            f"Este arquivo já está no acervo, com o mesmo conteúdo: {lugares}. "
            "Descarte o envio — uma segunda cópia atrapalha a busca.",
            conflitos,
            pode_prosseguir=False,
        )
    if not conflitos.mesmo_nome:
        return []
    if acao is None or acao == "":
        raise ConflitoNoAcervo(
            "Já existe no acervo documento com o mesmo nome: "
            f"{', '.join(conflitos.mesmo_nome)}. É uma versão nova (substituir) "
            "ou outro documento (manter os dois)?",
            conflitos,
            pode_prosseguir=True,
        )
    if acao not in ACOES_DE_CONFLITO:
        raise PedidoInvalido(f"Ação inválida; use uma de: {', '.join(ACOES_DE_CONFLITO)}.")
    if acao == "manter_ambos":
        return []
    escolhidas = substituir if isinstance(substituir, list) else conflitos.mesmo_nome
    escolhidas = [str(c) for c in escolhidas]
    fora = [c for c in escolhidas if c not in conflitos.mesmo_nome]
    if fora:
        raise PedidoInvalido("Só é possível substituir documentos com o mesmo nome do envio.")
    if not escolhidas:
        raise PedidoInvalido("Escolha ao menos um documento para substituir.")
    return escolhidas


def _conta_como_copia(chave: str) -> bool:
    """Entra na comparação por nome? Site e transcrições ficam de fora.

    As páginas do site repetem nome por natureza (``concursos/2024.html`` e
    ``remocao-interna/2024.html``; o mesmo "Resultado final" a cada ano), e a
    transcrição tem o nome do PDF de propósito. Listá-las seria ruído que
    ensina a ignorar a lista.
    """
    return not chave.startswith((f"{DocumentSource.SITE.value}/", PREFIXO_TRANSCRICOES))


def grupos_de_duplicatas(acervo: Iterable[Objeto]) -> list[dict]:
    """Cópias que já estão no acervo, para a aba "Documentos na base".

    * ``identico``: mesmo conteúdo, em qualquer origem — sempre é sobra;
    * ``mesmo_nome``: conteúdo diferente e mesmo nome-base, só entre documentos
      curados (repositório e painel). Pode ser versão antiga esquecida ou dois
      formatos do mesmo texto; quem decide é a PROGEP.
    """
    objetos = [o for o in acervo if not o.chave.endswith("/")]
    por_hash: dict[str, list[str]] = defaultdict(list)
    for obj in objetos:
        valor = conteudo_hash(obj.etag)
        if valor:
            por_hash[valor].append(obj.chave)
    grupos = [
        {"tipo": "identico", "chaves": sorted(chaves)}
        for chaves in por_hash.values()
        if len(chaves) > 1
    ]
    ja_identicos = {frozenset(g["chaves"]) for g in grupos}

    por_nome: dict[str, list[str]] = defaultdict(list)
    for obj in objetos:
        un = unidade(obj.chave)
        if _conta_como_copia(un) and un not in por_nome[nome_base(un)]:
            por_nome[nome_base(un)].append(un)
    grupos += [
        {"tipo": "mesmo_nome", "chaves": sorted(chaves)}
        for nome, chaves in por_nome.items()
        if nome and len(chaves) > 1 and frozenset(chaves) not in ja_identicos
    ]
    return sorted(grupos, key=lambda g: (g["tipo"] != "identico", g["chaves"]))


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


# --- FAQ dividido por pergunta ---------------------------------------------------
# Um FAQ enviado pelo painel entra na base já dividido (src/domain/faq.py), em
# `<origem>/faq/<nome do FAQ>/`. Para o painel, a pasta é **um** documento:
# lista, remove e substitui o FAQ inteiro.

#: O arquivo original do FAQ, guardado no bucket de preparo — fora de qualquer
#: data source, então não compete na busca com as perguntas divididas. Serve
#: para baixar o original e para reconhecer o reenvio do mesmo arquivo.
FAQ_ORIGINAIS_PREFIX = "faq-originais/"

_PASTA_DE_FAQ = re.compile(r"^(?:%s)/faq/[^/]+/$" % "|".join(s.value for s in DocumentSource))


def chave_original_do_faq(pasta: str, nome_arquivo: str) -> str:
    return f"{FAQ_ORIGINAIS_PREFIX}{pasta}{nome_arquivo}"


def pasta_da_original(chave: str) -> str | None:
    """``faq-originais/cliente/faq/X/X.pdf`` → ``cliente/faq/X/``."""
    if not chave.startswith(FAQ_ORIGINAIS_PREFIX):
        return None
    resto = chave[len(FAQ_ORIGINAIS_PREFIX):]
    pasta = resto.rsplit("/", 1)[0] + "/"
    return pasta if _PASTA_DE_FAQ.match(pasta) else None


def e_pasta_de_faq(chave: str) -> bool:
    return bool(_PASTA_DE_FAQ.match(chave)) and ".." not in chave


def validar_documento_ou_faq(chave: str) -> str:
    """Como :func:`validar_chave_do_acervo`, aceitando também a pasta de um FAQ."""
    bruta = str(chave or "").strip().lstrip("/")
    if bruta.endswith("/"):
        if e_pasta_de_faq(bruta) and not any(
            unicodedata.category(c)[0] == "C" for c in bruta
        ):
            return bruta
        raise PedidoInvalido("Caminho de documento inválido.")
    return validar_chave_do_acervo(bruta)
