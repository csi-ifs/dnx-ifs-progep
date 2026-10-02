"""O que pode ir para um log sobre quem conversou com o assistente.

No WhatsApp a chave da conversa **é o telefone** do servidor, e os log groups
das Lambdas e do runtime não vencem sozinhos. Um log não precisa do número
inteiro para ser útil: os 4 últimos dígitos bastam para casar uma linha com um
relato ("era o …1234"), como já faz a linha ``[entrega]``.
"""

from __future__ import annotations


def mascarar_contato(valor: str) -> str:
    """``"557900001234"`` → ``"…1234"``; identificadores que não são telefone
    (sessão do site, ids de teste) passam como estão."""
    texto = str(valor or "")
    digitos = texto.lstrip("+")
    if len(digitos) >= 8 and digitos.isdigit():
        return f"…{digitos[-4:]}"
    return texto
