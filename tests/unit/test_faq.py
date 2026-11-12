"""Testes da divisão de FAQ por pergunta (src/domain/faq.py).

O defeito que motivou o módulo: a resposta de uma pergunta cortada entre dois
trechos da base. Os testes cobrem os formatos reais do acervo — numerado com
ponto e com hífen, rotulado com "Pergunta:", pergunta quebrada em duas linhas
no PDF, sumário antes do corpo — e o que **não** pode virar pergunta.
"""

from __future__ import annotations

import pytest

from src.domain.faq import MAX_CARACTERES, dividir, documentos, texto_de_html
from src.domain.fontes import rotulo_da_fonte

pytestmark = pytest.mark.unit

PROBATORIO = """\
1. O que é estágio probatório?
É o período de avaliação.
2. Quem é responsável por avaliar o servidor?
Para servidores nomeados a partir de 07/02/2025:
A avaliação envolve chefia, autoavaliação e pares.
Para servidores TAEs nomeados até 06/02/2025:
A avaliação é realizada pela chefia imediata e por dois pares.
3. O que acontece se não houver servidores suficientes para realizar a avaliação por
pares?
A chefia indica outros servidores.
"""


def test_resposta_fica_inteira_com_os_dois_regimes():
    _, perguntas = dividir(PROBATORIO)
    assert [p.pergunta for p in perguntas] == [
        "O que é estágio probatório?",
        "Quem é responsável por avaliar o servidor?",
        "O que acontece se não houver servidores suficientes para realizar a avaliação por pares?",
    ]
    assert "07/02/2025" in perguntas[1].resposta and "06/02/2025" in perguntas[1].resposta


def test_inciso_numerado_dentro_da_resposta_nao_vira_pergunta():
    texto = "1. Quais os prazos?\nSão eles:\n1. trinta dias;\n2. sessenta dias.\n2. E depois?\nFim."
    _, perguntas = dividir(texto)
    assert [p.pergunta for p in perguntas] == ["Quais os prazos?", "E depois?"]
    assert "sessenta dias" in perguntas[0].resposta


def test_formato_tema_e_pergunta_rotulada():
    """FAQ NAPA: "N. Tema", linha em branco, marcador solto, "Pergunta: …?" quebrada."""
    texto = (
        "1. Remoção a Pedido\n\nPergunta: O que é a remoção a pedido?\nResposta: É o deslocamento.\n"
        "2. Docente em Dedicação Exclusiva e o desenvolvimento de outra atividade\nremunerada\n\uf0b7\n"
        "Pergunta: Há impedimentos para o docente\ncom Dedicação Exclusiva (DE)?\nResposta: Há exceções.\n"
    )
    _, perguntas = dividir(texto)
    assert len(perguntas) == 2
    assert perguntas[0].pergunta == "Remoção a Pedido — O que é a remoção a pedido?"
    assert perguntas[1].pergunta.endswith("com Dedicação Exclusiva (DE)?")
    assert perguntas[1].resposta.startswith("Resposta: Há exceções.")


def test_sumario_no_inicio_e_descartado():
    """FAQ da Perícia: as perguntas aparecem no sumário e de novo no corpo."""
    sumario = "Sumário\n1 - Como enviar o atestado?\n2 - Devo receber declaração?\n3 - Pode ser automático?\n"
    corpo = (
        "1 - Como enviar o atestado?\nPelo SOUGov, em até 5 dias, anexando o documento digitalizado.\n"
        "2 - Devo receber declaração?\nSim, emitida pelo sistema após o atendimento presencial.\n"
        "3 - Pode ser automático?\nSim, para atestados de até 5 dias no ano civil.\n"
    )
    nomes = [n for n, _ in documentos("FAQ Perícia", "teste", sumario + "texto " * 60 + "\n" + corpo)]
    _, perguntas = dividir(sumario + corpo)
    assert all(len(p.resposta) > 20 for p in perguntas)
    assert len(perguntas) == 3
    assert not any(n.startswith("00-") for n in nomes)


def test_titulo_afirmativo_numerado_e_salto_de_numero():
    texto = "13. Como proceder?\nAssim.\n14. Falecimento do servidor antes da perícia.\nArquiva-se.\n16. E o laudo?\nRetifica-se."
    _, perguntas = dividir(texto.replace("13.", "1.").replace("14.", "2.").replace("16.", "4."))
    assert [p.pergunta for p in perguntas] == [
        "Como proceder?", "Falecimento do servidor antes da perícia.", "E o laudo?",
    ]


def test_sem_perguntas_reconhecidas_nao_gera_arquivos():
    assert documentos("X", "y", "Um texto corrido sem perguntas numeradas.") == []


def test_cada_arquivo_traz_titulo_e_pergunta():
    arquivos = dict(documentos("FAQ – Estágio Probatório", "FAQ.pdf", PROBATORIO))
    corpo = arquivos["02-quem-e-responsavel-por-avaliar-o-servidor.md"]
    assert corpo.startswith("# FAQ – Estágio Probatório")
    assert "## Quem é responsável por avaliar o servidor?" in corpo
    assert "Pergunta 2 de 3" in corpo


def test_resposta_longa_e_dividida_repetindo_a_pergunta():
    longa = "1. Qual a regra?\n" + "\n\n".join(f"Parágrafo {i}. " + "x" * 900 for i in range(10))
    arquivos = documentos("FAQ", "y", longa)
    assert len(arquivos) > 1
    assert all(len(c) <= MAX_CARACTERES + 300 for _, c in arquivos)
    assert all("## Qual a regra?" in c and "parte" in c for _, c in arquivos)


def test_paragrafo_unico_gigante_e_cortado_nas_frases():
    texto = "1. Qual a regra?\n" + " ".join(f"Frase número {i} com algum texto." for i in range(400))
    arquivos = documentos("FAQ", "y", texto)
    assert len(arquivos) > 1
    assert all(len(c) <= MAX_CARACTERES + 300 for _, c in arquivos)


def test_texto_de_html_ignora_script_e_separa_blocos():
    html = "<html><script>var x=1;</script><p>1. O QUE É?</p><p>Resposta.</p></html>"
    assert texto_de_html(html) == "1. O QUE É?\nResposta."


def test_rotulo_da_fonte_e_o_nome_do_faq():
    assert rotulo_da_fonte(
        "s3://b/cliente/faq/FAQ - ESTÁGIO PROBATÓRIO/09-quem-e-responsavel.md"
    ) == "FAQ ESTÁGIO PROBATÓRIO"
