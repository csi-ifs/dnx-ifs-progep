"""Testes do reconhecimento de mensagens sociais (Req 6.2).

O risco maior não é deixar passar uma cortesia — é **capturar uma pergunta** e
responder "de nada" a quem queria informação. Por isso a maioria dos casos aqui
verifica o que NÃO deve ser classificado como social.
"""

from __future__ import annotations

import pytest

from src.domain.social import TipoSocial, classificar_social

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Oi", TipoSocial.SAUDACAO),
        ("bom dia", TipoSocial.SAUDACAO),
        ("Boa tarde!", TipoSocial.SAUDACAO),
        ("Olá, tudo bem?", TipoSocial.SAUDACAO),
        ("Obrigado", TipoSocial.AGRADECIMENTO),
        ("obrigada!", TipoSocial.AGRADECIMENTO),
        ("Vlw", TipoSocial.AGRADECIMENTO),
        ("Muito obrigado 🙏", TipoSocial.AGRADECIMENTO),
        ("Tchau", TipoSocial.DESPEDIDA),
        ("era só isso", TipoSocial.DESPEDIDA),
        # Visto em 25/09/2026: foi para a busca e voltou "não encontrei".
        ("Não tenho mais dúvidas", TipoSocial.DESPEDIDA),
        ("sem mais dúvidas", TipoSocial.DESPEDIDA),
        ("Ok", TipoSocial.CONFIRMACAO),
        ("Entendi.", TipoSocial.CONFIRMACAO),
        ("beleza", TipoSocial.CONFIRMACAO),
    ],
)
def test_reconhece_mensagens_sociais(texto, esperado):
    assert classificar_social(texto) is esperado


@pytest.mark.parametrize(
    "texto",
    [
        # Cortesia + pergunta continua sendo pergunta.
        "Obrigado, mas como faço para pedir férias?",
        "Bom dia! Como solicito licença capacitação?",
        "Ok, e quanto tempo demora o processo?",
        "Oi, preciso saber sobre aposentadoria",
        "Entendi, mas e no caso de docente?",
        # Perguntas normais que contêm palavras parecidas.
        "Como funciona o auxílio saúde?",
        "Quais documentos preciso apresentar?",
        "Quero saber sobre o plano de saúde GEAP",
        # Vazio e ruído.
        "",
        "   ",
        "?",
    ],
)
def test_nao_captura_perguntas(texto):
    assert classificar_social(texto) is None


def test_mensagem_longa_nunca_e_social():
    """Acima do limite há conteúdo demais para ser só cortesia."""
    texto = "obrigado " * 10
    assert len(texto) > 40
    assert classificar_social(texto) is None


@pytest.mark.parametrize("texto", ["OBRIGADO", "ObRiGaDo", "obrigado...", "Obrigado!!!"])
def test_ignora_caixa_e_pontuacao(texto):
    assert classificar_social(texto) is TipoSocial.AGRADECIMENTO


@pytest.mark.parametrize("texto", ["Olá", "Ola", "OLÁ"])
def test_ignora_acento(texto):
    assert classificar_social(texto) is TipoSocial.SAUDACAO


# --- Combinações de cortesias -------------------------------------------------
# "Entendi, obrigada" chegou de um teste real e ia para a Base_de_Conhecimento,
# porque o casamento exigia a frase inteira numa lista fixa.

@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Entendi, obrigada!", TipoSocial.AGRADECIMENTO),
        ("ok obrigado", TipoSocial.AGRADECIMENTO),
        ("certo, valeu", TipoSocial.AGRADECIMENTO),
        ("muito obrigado mesmo", TipoSocial.AGRADECIMENTO),
        ("obrigado pela ajuda", TipoSocial.AGRADECIMENTO),
        ("entendi, então obrigada", TipoSocial.AGRADECIMENTO),
        ("tudo bem e você?", TipoSocial.SAUDACAO),
    ],
)
def test_reconhece_cortesias_combinadas(texto, esperado):
    assert classificar_social(texto) is esperado


def test_despedida_vence_agradecimento():
    """Em "obrigado, tchau" o que importa é o fim: a despedida encerra a conversa."""
    assert classificar_social("obrigado, tchau") is TipoSocial.DESPEDIDA


@pytest.mark.parametrize(
    "texto",
    [
        # Cortesia + conteúdo continua sendo pergunta, mesmo com conector no meio.
        "obrigado pela ajuda com férias",
        "tudo bem sobre férias?",
        "e o auxílio saúde?",
        "ok e sobre o adicional?",
    ],
)
def test_conteudo_apos_cortesia_nao_e_social(texto):
    assert classificar_social(texto) is None
