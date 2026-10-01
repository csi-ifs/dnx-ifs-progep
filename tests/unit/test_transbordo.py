"""Testes das decisões puras do transbordo humano (Req 3.1, 3.4)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain.transbordo import (
    RespostaOferta,
    classificar_resposta_oferta,
    dentro_do_expediente,
    nome_para_atendente,
)

pytestmark = pytest.mark.unit


def _utc(ano, mes, dia, hora, minuto=0):
    return datetime(ano, mes, dia, hora, minuto, tzinfo=timezone.utc)


# --- Expediente ----------------------------------------------------------------
# 14/09/2026 é segunda-feira. Aracaju = UTC-3.

@pytest.mark.parametrize(
    "momento,esperado",
    [
        (_utc(2026, 9, 14, 12, 0), True),    # 09:00 local — abre
        (_utc(2026, 9, 14, 11, 59), False),  # 08:59 local
        (_utc(2026, 9, 14, 20, 59), True),   # 17:59 local
        (_utc(2026, 9, 14, 21, 0), False),   # 18:00 local — fechou
        (_utc(2026, 9, 18, 15, 0), True),    # sexta 12:00
        (_utc(2026, 9, 19, 15, 0), False),   # sábado
        (_utc(2026, 9, 20, 15, 0), False),   # domingo
    ],
)
def test_expediente_de_segunda_a_sexta_das_9_as_18(momento, esperado):
    assert dentro_do_expediente(momento) is esperado


def test_expediente_usa_o_horario_local_e_nao_o_utc():
    """Sexta 23h em UTC ainda é sexta 20h em Aracaju — fora do expediente, mas
    pelo horário, não pelo dia. Segunda 02h UTC é domingo 23h local."""
    assert dentro_do_expediente(_utc(2026, 9, 15, 2, 0)) is False  # seg 23h local


def test_momento_sem_fuso_e_tratado_como_utc():
    assert dentro_do_expediente(datetime(2026, 9, 14, 12, 0)) is True


# --- Resposta à oferta ---------------------------------------------------------

@pytest.mark.parametrize(
    "texto", ["sim", "Sim!", "SIM", "s", "quero", "Pode sim", "ok", "Por favor",
              "sim, por favor", "claro", "1", "Quero falar com um atendente"],
)
def test_aceites(texto):
    assert classificar_resposta_oferta(texto) is RespostaOferta.ACEITOU


@pytest.mark.parametrize(
    "texto", ["não", "Não, obrigado", "nao", "N", "agora não", "não precisa", "2"],
)
def test_recusas(texto):
    assert classificar_resposta_oferta(texto) is RespostaOferta.RECUSOU


@pytest.mark.parametrize(
    "texto",
    [
        "Não, mas como faço para pedir férias?",
        "sim, e quanto tempo demora?",
        "Qual o prazo da licença capacitação?",
        "",
    ],
)
def test_pergunta_nova_nao_e_resposta_a_oferta(texto):
    """Capturar demais aqui transferiria (ou descartaria) uma pergunta de verdade."""
    assert classificar_resposta_oferta(texto) is RespostaOferta.OUTRA


# --- Nome exibido ao atendente -------------------------------------------------

def test_nome_do_atendente_mostra_so_o_final_do_numero():
    nome = nome_para_atendente("557900001234")
    assert nome.endswith("…1234)")
    assert "5549887" not in nome


def test_nome_sem_numero_nao_quebra():
    assert "????" in nome_para_atendente("")
