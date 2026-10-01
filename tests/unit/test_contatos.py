"""Testes do catálogo de contatos da PROGEP (pedido do cliente, 28/09/2026)."""

from __future__ import annotations

import pytest

from src.domain.contatos import (
    DEPARTAMENTOS,
    FORA,
    GERAL,
    formatar_contato,
    interpretar_classificacao,
    lista_completa,
    prompt_classificacao,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "saida,esperado",
    [
        ("DAP", ("DAP",)),
        ("dsdpes", ("DSDPES",)),
        ("DNAGS.", ("DNAGS",)),
        ("Setor: NPG", ("NPG",)),
        # Saúde vai para os dois setores, como o cliente pediu.
        ("SAUDE", ("SIASS", "DAP")),
        ("FORA", (FORA,)),
        ("PROGEP", (GERAL,)),
    ],
)
def test_interpreta_o_codigo(saida, esperado):
    assert interpretar_classificacao(saida) == esperado


@pytest.mark.parametrize("saida", ["", "   ", "não sei", "RH", None])
def test_saida_irreconhecivel_vira_o_contato_geral(saida):
    """Indicar o setor errado é pior que indicar o geral."""
    assert interpretar_classificacao(saida) == (GERAL,)


def test_contato_de_um_setor_traz_email_e_telefone():
    texto = formatar_contato(("DSDPES",))
    assert "dsdpes.progep@ifs.edu.br" in texto
    assert "ramal 1447 ou 1425" in texto
    assert "este setor" in texto


def test_contato_de_dois_setores_usa_o_plural():
    texto = formatar_contato(("SIASS", "DAP"))
    assert "estes setores" in texto
    assert texto.index("siass@") < texto.index("dap.progep@")


def test_codigo_desconhecido_cai_no_geral():
    assert "progep@ifs.edu.br" in formatar_contato(("XYZ",))
    assert "progep@ifs.edu.br" in formatar_contato(())


def test_lista_completa_tem_todos_os_setores():
    texto = lista_completa()
    for d in DEPARTAMENTOS.values():
        assert d.email in texto


def test_prompt_lista_todos_os_codigos_aceitos():
    """Um código que o prompt não oferece nunca seria escolhido."""
    prompt = prompt_classificacao()
    for codigo in (*[c for c in DEPARTAMENTOS if c != "SIASS"], "SAUDE", FORA):
        assert f"- {codigo}:" in prompt
