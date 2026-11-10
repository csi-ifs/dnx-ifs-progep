"""Testes das decisões do painel de acervo.

O risco maior aqui não é recusar um envio válido — é **deixar entrar na base**
algo que ninguém confirmou, ou apagar o que não devia. Por isso a maioria dos
casos verifica o que a lógica precisa recusar.
"""

from __future__ import annotations

import pytest

from src.domain.acervo import (
    GRUPO_ADMIN,
    MAX_TITULO,
    NaoAutorizado,
    PedidoInvalido,
    chave_de_preparo,
    chave_publicada,
    chave_removida,
    ConflitoNoAcervo,
    Conflitos,
    Objeto,
    conflitos_do_envio,
    conteudo_hash,
    decidir_conflito,
    exige_admin,
    nome_base,
    nome_seguro,
    procedencia,
    validar_chave_do_acervo,
    validar_envio,
    validar_titulo,
)
from src.domain.config import (
    MAX_DOC_SIZE_BYTES,
    PAINEL_PREFIX,
    PREPARO_PREFIX,
    REMOVIDOS_PREFIX,
)

pytestmark = pytest.mark.unit


# --- Autorização ---------------------------------------------------------------

def test_admin_passa():
    exige_admin([GRUPO_ADMIN, "outro"])


@pytest.mark.parametrize("grupos", [[], None, ["servidores"], ["Administradores"]])
def test_sem_o_grupo_nao_altera_o_acervo(grupos):
    """Esconder a aba na interface nunca protegeu endpoint.

    O caso ``["Administradores"]`` é proposital: comparação exata, sem
    normalizar caixa — um grupo parecido não é o grupo.
    """
    with pytest.raises(NaoAutorizado):
        exige_admin(grupos)


# --- Nome de arquivo -----------------------------------------------------------

@pytest.mark.parametrize(
    "nome",
    [
        "Resolução CS 348.pdf",
        "IN nº 01-2019 (Licença Capacitação).pdf",
        "FAQ_NAPA.md",
        "planilha de cargos.docx",
        "nota.txt",
        "pagina.html",
    ],
)
def test_preserva_nome_real_do_acervo(nome):
    """Acento e espaço ficam: é do nome que sai o rótulo da fonte citada."""
    assert nome_seguro(nome) == nome


@pytest.mark.parametrize(
    "nome",
    [
        ".oculto.pdf",
        "",
        "   ",
        "sem-extensao",
        "planilha.xlsx",          # formato que a base não indexa
        "script.exe",
        "a" * 201 + ".pdf",
    ],
)
def test_recusa_nome_perigoso_ou_inaceitavel(nome):
    with pytest.raises(PedidoInvalido):
        nome_seguro(nome)


@pytest.mark.parametrize(
    "nome",
    [
        "pasta/subpasta/Resolução.pdf",
        "../../site/x.pdf",        # escreveria num prefixo que a base varre
        "..\\..\\site\\x.pdf",
    ],
)
def test_travessia_de_caminho_nao_sobrevive_ao_saneamento(nome):
    """Caminho é saneado, não recusado — o que importa é o resultado.

    A garantia afirmada diretamente: sobra só o nome do arquivo, então não há
    como um envio escrever fora da área de preparo.
    """
    limpo = nome_seguro(nome)
    assert "/" not in limpo and "\\" not in limpo and ".." not in limpo


# --- Envio ---------------------------------------------------------------------

def test_envio_valido():
    assert validar_envio({"nome": "Resolução.pdf", "tamanho": 1024}) == (
        "Resolução.pdf",
        1024,
    )


@pytest.mark.parametrize(
    "corpo",
    [
        {"nome": "x.pdf", "tamanho": 0},
        {"nome": "x.pdf", "tamanho": -1},
        {"nome": "x.pdf", "tamanho": "muito"},
        {"nome": "x.pdf", "tamanho": MAX_DOC_SIZE_BYTES + 1},
        "não é objeto",
    ],
)
def test_recusa_envio_invalido(corpo):
    with pytest.raises(PedidoInvalido):
        validar_envio(corpo)


def test_teto_de_tamanho_acompanha_o_da_ingestao():
    """Recusar aqui poupa subir um arquivo que a base descartaria em silêncio."""
    assert validar_envio({"nome": "x.pdf", "tamanho": MAX_DOC_SIZE_BYTES})[1] == (
        MAX_DOC_SIZE_BYTES
    )


# --- Título --------------------------------------------------------------------

def test_titulo_normaliza_espaco():
    assert validar_titulo("  Resolução   CS 348/2025 ") == "Resolução CS 348/2025"


@pytest.mark.parametrize("valor,esperado", [(None, None), ("", None), ("   ", None)])
def test_titulo_ausente_vira_none(valor, esperado):
    assert validar_titulo(valor) is esperado


def test_titulo_recusa_quebra_de_linha():
    """Vira cabeçalho HTTP no metadado do objeto: quebra permite injeção.

    Regressão de ordem: normalizar antes de conferir engoliria o ``\\n`` e a
    verificação passaria sempre.
    """
    with pytest.raises(PedidoInvalido):
        validar_titulo("Resolução\nX-Injetado: 1")


def test_titulo_longo_demais():
    with pytest.raises(PedidoInvalido):
        validar_titulo("a" * (MAX_TITULO + 1))


# --- Chaves --------------------------------------------------------------------

def test_preparo_e_por_usuario():
    """Sem isso, dois administradores com o mesmo nome de arquivo se
    sobrescreveriam — e um confirmaria o arquivo do outro sem saber."""
    a = chave_de_preparo("sub-alice", "CS 348.pdf")
    b = chave_de_preparo("sub-bob", "CS 348.pdf")
    assert a != b
    assert a.startswith(PREPARO_PREFIX) and b.startswith(PREPARO_PREFIX)


def test_preparo_exige_usuario():
    with pytest.raises(PedidoInvalido):
        chave_de_preparo("", "x.pdf")


def test_publicada_fica_sob_o_prefixo_do_painel():
    assert chave_publicada("CS 348.pdf") == f"{PAINEL_PREFIX}CS 348.pdf"


def test_lixeira_preserva_o_caminho_de_origem():
    """É o que permite desfazer: sem o caminho, dois arquivos de mesmo nome
    vindos de pastas diferentes colidiriam na lixeira."""
    assert chave_removida("cliente/DAP/x.pdf") == f"{REMOVIDOS_PREFIX}cliente/DAP/x.pdf"
    assert chave_removida("site/x.pdf") != chave_removida("cliente/DAP/x.pdf")


# --- Validação da chave a remover ----------------------------------------------
# Esta é a fronteira perigosa: o papel da Lambda alcança o bucket inteiro.

@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("cliente/DAP/x.pdf", "cliente/DAP/x.pdf"),
        ("/cliente/DAP/x.pdf", "cliente/DAP/x.pdf"),
        ("s3://progep-ifs-documents/site/edital.pdf", "site/edital.pdf"),
    ],
)
def test_aceita_chave_do_acervo(entrada, esperado):
    assert validar_chave_do_acervo(entrada) == esperado


@pytest.mark.parametrize(
    "entrada",
    [
        "",
        "   ",
        "preparo/alice/x.pdf",        # área de preparo não é acervo
        "removidos/cliente/x.pdf",    # já removido
        "cliente/../preparo/x.pdf",   # começa com prefixo válido e aponta fora
        "site/",                      # prefixo inteiro, não documento
        "qualquer/outro.pdf",
    ],
)
def test_recusa_chave_fora_do_acervo(entrada):
    with pytest.raises(PedidoInvalido):
        validar_chave_do_acervo(entrada)


# --- Procedência ---------------------------------------------------------------

def test_conteudo_do_crawler_avisa_que_volta():
    """A tela precisa dizer a verdade: apagar `site/` dura até o próximo crawl."""
    p = procedencia("site/guia-do-servidor.html")
    assert p.regenerave is True
    assert p.aviso


@pytest.mark.parametrize(
    "chave,origem",
    [
        (f"{PAINEL_PREFIX}CS 348.pdf", "painel"),
        ("cliente/DAP/portaria.pdf", "repositório da PROGEP"),
        ("site/edital.pdf", "site institucional"),
    ],
)
def test_origem_visivel_na_lista(chave, origem):
    assert procedencia(chave).origem == origem


def test_documento_do_repositorio_e_removivel_em_definitivo():
    """Carga única: apagar vale de verdade, e é o que o cliente pediu."""
    assert procedencia("cliente/DNAGS/CS_348.pdf").regenerave is False


# --- Cópias ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "chave,esperado",
    [
        ("cliente/DNAGS/FAQ_NAPA.docx", "faq napa"),
        ("cliente/painel/FAQ NAPA.pdf", "faq napa"),
        ("cliente/painel/Projeto_Mente_em_foco_versão_final.docx (1).pdf",
         "projeto mente em foco versao final"),
        ("cliente/DAP/Resolução CS 348.PDF", "resolucao cs 348"),
    ],
)
def test_nome_base(chave, esperado):
    assert nome_base(chave) == esperado


def test_etag_de_multipart_nao_e_hash_do_conteudo():
    assert conteudo_hash('"ABC123"') == "abc123"
    assert conteudo_hash('"abc123-4"') is None
    assert conteudo_hash(None) is None


def test_conflitos_separam_identico_de_mesmo_nome():
    acervo = [
        Objeto("cliente/DAP/outro_nome.pdf", '"aaa"'),
        Objeto("cliente/DAP/CS_348.docx", '"bbb"'),
        Objeto("cliente/DAP/sem_relacao.pdf", '"ccc"'),
    ]
    achados = conflitos_do_envio("CS 348.pdf", '"aaa"', acervo, "cliente/painel/CS 348.pdf")
    assert achados.identicos == ["cliente/DAP/outro_nome.pdf"]
    assert achados.mesmo_nome == ["cliente/DAP/CS_348.docx"]


def test_destino_ocupado_conta_como_mesmo_nome():
    destino = "cliente/painel/a.pdf"
    achados = conflitos_do_envio("a.pdf", '"novo"', [Objeto(destino, '"velho"')], destino)
    assert achados.mesmo_nome == [destino]


def test_sem_conflito_segue():
    assert decidir_conflito(Conflitos(), None, None) == []


def test_identico_nunca_prossegue():
    with pytest.raises(ConflitoNoAcervo) as erro:
        decidir_conflito(Conflitos(identicos=["x"]), "manter_ambos", None)
    assert erro.value.pode_prosseguir is False


def test_mesmo_nome_sem_acao_pergunta():
    with pytest.raises(ConflitoNoAcervo) as erro:
        decidir_conflito(Conflitos(mesmo_nome=["x"]), None, None)
    assert erro.value.pode_prosseguir is True


def test_substituir_sem_lista_substitui_todos_os_do_conflito():
    assert decidir_conflito(Conflitos(mesmo_nome=["x", "y"]), "substituir", None) == ["x", "y"]


@pytest.mark.parametrize("acao,lista", [("apagar_tudo", None), ("substituir", ["z"]),
                                        ("substituir", [])])
def test_acao_ou_lista_invalida(acao, lista):
    with pytest.raises(PedidoInvalido):
        decidir_conflito(Conflitos(mesmo_nome=["x"]), acao, lista)
