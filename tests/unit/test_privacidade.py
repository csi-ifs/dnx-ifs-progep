"""O telefone do servidor nunca vai inteiro para um log."""

import pytest

from src.domain.privacidade import mascarar_contato


@pytest.mark.parametrize(
    "valor, esperado",
    [
        ("557900001234", "…1234"),
        ("+557900001234", "…1234"),
        ("sess-3", "sess-3"),  # sessão do site / ids de teste passam
        ("teste-custos", "teste-custos"),
        ("", ""),
        (None, ""),
    ],
)
def test_mascarar_contato(valor, esperado):
    assert mascarar_contato(valor) == esperado
