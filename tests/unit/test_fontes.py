"""Testes do rótulo das fontes citadas (Req 2.3).

A lista de fontes é montada a partir da chave do S3, não pedida ao modelo — é o
que garante que nenhuma fonte citada seja inventada. Aqui verifica-se que o
rótulo fica legível **sem deturpar** o identificador da norma.
"""

from __future__ import annotations

import pytest

from src.domain.fontes import formatar_fontes, rotulo_da_fonte

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "doc_id,esperado",
    [
        (
            "s3://b/cliente/DAP/05_Portaria_Conjunta_6_2022_Licenca_Capacitacao.pdf",
            "Portaria Conjunta 6 2022 Licenca Capacitacao",
        ),
        ("s3://b/cliente/DNAGS/L8112compilado.pdf", "Lei 8112"),
        ("s3://b/cliente/DAP/02_EC_20_1998.pdf", "Emenda Constitucional 20"),
        (
            "s3://b/site/mapeamento-processos/aposentadoria-voluntaria.md",
            "Fluxo do processo: Aposentadoria voluntaria",
        ),
    ],
)
def test_rotulo_legivel(doc_id, esperado):
    assert rotulo_da_fonte(doc_id) == esperado


def test_numero_da_norma_nao_e_truncado():
    """Regressão: o sufixo de versão comia dígitos de ``D10835`` (um decreto)."""
    assert rotulo_da_fonte("s3://b/cliente/DSDPES/D10835.pdf") == "Decreto 10835"


def test_sufixo_de_versao_com_separador_e_removido():
    assert rotulo_da_fonte("s3://b/c/Nota_Tecnica_24797 (3).pdf") == "Nota Tecnica 24797"


def test_transcricao_e_identificada_como_fluxo():
    """O leitor precisa saber que a fonte é o desenho do processo."""
    rotulo = rotulo_da_fonte("s3://b/site/mapeamento-processos/admissao.md")
    assert rotulo.startswith("Fluxo do processo:")


@pytest.mark.parametrize("doc_id", ["", "s3://b/site/", "s3://b/cliente"])
def test_identificador_sem_nome_util_e_descartado(doc_id):
    assert rotulo_da_fonte(doc_id) == ""


def test_formatar_limita_e_desduplica():
    fontes = formatar_fontes(
        [
            "s3://b/c/L8112compilado.pdf",
            "s3://b/c/L8112compilado.pdf",  # mesma norma, citada duas vezes
            "s3://b/c/D10835.pdf",
            "s3://b/c/L14133.pdf",
            "s3://b/c/L13146.pdf",
        ],
        maximo=3,
    )
    assert fontes.count("•") == 3
    assert fontes.count("Lei 8112") == 1


# --- Acento codificado pelo portal ---------------------------------------------
# O portal grava ``ç`` como ``-C3-A7`` no nome do arquivo. Sem tratar, o servidor
# lê a sopa de letras na lista de fontes da resposta — 17 dos 407 documentos do
# acervo têm nome assim.

@pytest.mark.parametrize(
    "doc_id,esperado",
    [
        (
            "s3://b/site/images/MANIFESTA-C3-87-C3-83O_T-C3-89CNICA_PDP_2020.pdf",
            "MANIFESTAÇÃO TÉCNICA PDP 2020",
        ),
        (
            "s3://b/site/images/CALEND-C3-81RIO_DE_REVIS-C3-95ES_DO_PDP_2021.pdf",
            "CALENDÁRIO DE REVISÕES DO PDP 2021",
        ),
        (
            "s3://b/site/progep/images/Intsru-C3-A7-C3-A3o_Normativa_n.-C2-BA_01-2016.pdf",
            "Intsrução Normativa n.º 01 2016",
        ),
    ],
)
def test_decodifica_acento_do_portal(doc_id, esperado):
    assert rotulo_da_fonte(doc_id) == esperado


@pytest.mark.parametrize(
    "doc_id,esperado",
    [
        # `-2024` tem quatro dígitos: não é byte, e o ano precisa sobreviver.
        ("s3://b/site/Edital_01-2024_Retificado.pdf", "Edital 01 2024 Retificado"),
        # `-20` seria um espaço se fosse percent-encoding; aqui é parte do nome.
        ("s3://b/cliente/Portaria-20-2023.pdf", "Portaria 20 2023"),
        # Sequência UTF-8 inválida (byte de continuação sem líder) fica intacta.
        ("s3://b/cliente/Nota-8A-Tecnica.pdf", "Nota 8A Tecnica"),
    ],
)
def test_nao_confunde_numero_com_byte(doc_id, esperado):
    """A restrição a UTF-8 válido é o que impede estragar nome normal."""
    assert rotulo_da_fonte(doc_id) == esperado


@pytest.mark.parametrize(
    "doc_id,esperado",
    [
        # Visto no WhatsApp em 22/09/2026 como "Cartilha20IFS.compressed".
        (
            "s3://progep-ifs-documents/site/comunicacao/images/Documentos/Manuais/"
            "Cartilha20IFS.compressed.pdf",
            "Cartilha IFS",
        ),
        # O "20" de datas e números não pode virar espaço.
        ("s3://b/cliente/Edital_01-2024.pdf", "Edital 01 2024"),
        ("s3://b/cliente/Portaria_2027-2026_Comissao.pdf", "Portaria 2027 2026 Comissao"),
    ],
)
def test_limpa_resto_de_upload_sem_tocar_em_numeros(doc_id, esperado):
    assert rotulo_da_fonte(doc_id) == esperado


def test_formatar_sem_fontes_nao_polui_a_resposta():
    assert formatar_fontes([]) == ""
    assert formatar_fontes(["s3://b/site/"]) == ""
