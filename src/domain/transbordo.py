"""Transbordo para atendimento humano pelo WhatsApp (Req 3.1, 3.4, 6.1).

Lógica pura das duas decisões que o transbordo toma **antes** de tocar no
Amazon Connect:

1. **Há atendente para oferecer?** Só no expediente da PROGEP. Fora dele, abrir
   um atendimento deixaria o servidor esperando uma pessoa que não vai aparecer
   — pior que dizer o horário e deixar ele voltar.
2. **O servidor aceitou?** A oferta é feita com uma pergunta de sim/não, e a
   resposta chega como mensagem livre de WhatsApp.

**Por que com confirmação, e não automático:** o assistente devolve "não
encontrei" também para pergunta fora do escopo ("cardápio do restaurante").
Transferir tudo isso encheria a fila de casos que nenhum atendente da PROGEP
resolve.

**A regra de ouro é a mesma das cortesias: não capturar demais.** "Não, mas
como faço para pedir férias?" é pergunta nova, não recusa. Por isso a resposta
precisa ser **inteiramente** sim ou não; qualquer outra coisa descarta a oferta e
segue como pergunta ao assistente.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from src.domain.social import _normalizar

#: Fuso de Aracaju. **Offset fixo, não ``zoneinfo``**: o Brasil não tem horário
#: de verão desde 2019, e a imagem da Lambda não garante a base ``tzdata`` — um
#: ``ZoneInfoNotFoundError`` em produção desligaria o transbordo inteiro.
FUSO_PROGEP = timezone(timedelta(hours=-3))

#: Expediente do atendimento humano: segunda a sexta, das 9h às 18h. Feriados
#: não são considerados — num feriado a oferta é feita e o servidor espera.
HORA_INICIO = 9
HORA_FIM = 18
DIAS_UTEIS = frozenset(range(0, 5))  # segunda (0) a sexta (4)

#: Descrição do expediente para as mensagens ao servidor.
EXPEDIENTE_TEXTO = "de segunda a sexta, das 9h às 18h"


class RespostaOferta(str, Enum):
    """Como o servidor respondeu à oferta de atendente."""

    ACEITOU = "aceitou"
    RECUSOU = "recusou"
    #: Não é sim nem não — em geral, uma pergunta nova.
    OUTRA = "outra"


_ACEITES = frozenset({
    "sim", "s", "ss", "sim sim", "quero", "sim quero", "quero sim", "pode",
    "pode ser", "pode sim", "sim pode", "pode transferir", "sim pode transferir",
    "ok", "okay", "claro", "claro que sim", "com certeza", "por favor",
    "sim por favor", "quero por favor", "gostaria", "sim gostaria",
    "gostaria sim", "aceito", "isso", "certo", "beleza", "blz", "bora", "1",
    "sim obrigado", "sim obrigada", "yes", "quero falar", "sim quero falar",
    "quero falar com um atendente", "quero falar com atendente",
})

_RECUSAS = frozenset({
    "nao", "n", "nn", "nao obrigado", "nao obrigada", "agora nao",
    "nao precisa", "nao quero", "nao valeu", "nao vlw", "dispenso",
    "deixa", "deixa pra la", "deixa para la", "nao precisa obrigado",
    "nao precisa obrigada", "tudo bem nao precisa", "2", "no",
})


def dentro_do_expediente(agora: datetime | None = None) -> bool:
    """``True`` se há atendimento humano neste momento.

    ``agora`` sem fuso é tratado como UTC — é o que ``datetime.utcnow()`` e o
    relógio da Lambda entregam.
    """
    momento = agora or datetime.now(timezone.utc)
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    local = momento.astimezone(FUSO_PROGEP)
    return local.weekday() in DIAS_UTEIS and HORA_INICIO <= local.hour < HORA_FIM


def classificar_resposta_oferta(texto: str) -> RespostaOferta:
    """Lê a resposta do servidor à pergunta "quer falar com um atendente?"."""
    normalizado = _normalizar(texto or "")
    if normalizado in _ACEITES:
        return RespostaOferta.ACEITOU
    if normalizado in _RECUSAS:
        return RespostaOferta.RECUSOU
    return RespostaOferta.OUTRA


def nome_para_atendente(telefone: str) -> str:
    """Nome exibido ao atendente no Connect.

    Leva só o final do número: identifica a conversa na fila sem expor o
    telefone inteiro na tela de quem atende. O número completo vai nos
    atributos do contato, para quem precisar retornar a ligação.
    """
    digitos = "".join(c for c in telefone if c.isdigit())
    final = digitos[-4:] if digitos else "????"
    return f"Servidor WhatsApp (…{final})"
