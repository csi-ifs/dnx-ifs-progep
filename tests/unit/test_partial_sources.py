"""Unit tests for partial-source availability resolution (Task 2.4).

These example-based tests cover the pure-logic helper
:func:`src.domain.ingestion.resolve_available_sources`, which decides which
origin sources to ingest from and records the unavailability of any missing
source.

Requisitos cobertos:
* Req 1.5: as fontes de origem suportadas são a documentação/editais do site
  (``DocumentSource.SITE``) e o repositório do cliente (``DocumentSource.CLIENTE``).
* Req 1.6: quando apenas uma das fontes está disponível, a ingestão ocorre a
  partir da fonte disponível e a indisponibilidade da outra é registrada.
"""

from __future__ import annotations

import pytest

from src.domain.config import DocumentSource
from src.domain.ingestion import (
    SourceAvailabilityResolution,
    resolve_available_sources,
)


@pytest.mark.unit
def test_both_sources_available_ingests_both_records_none():
    """Req 1.5, 1.6: com ambas as fontes disponíveis, ambas são ingeridas."""
    result = resolve_available_sources([DocumentSource.SITE, DocumentSource.CLIENTE])

    assert isinstance(result, SourceAvailabilityResolution)
    assert result.to_ingest == ("site", "cliente")
    assert result.unavailable == ()
    assert result.notes == ()


@pytest.mark.unit
def test_only_site_available_records_cliente_unavailability():
    """Req 1.6: só o site disponível -> ingere site e registra cliente indisponível."""
    result = resolve_available_sources([DocumentSource.SITE])

    assert result.to_ingest == ("site",)
    assert result.unavailable == ("cliente",)
    assert len(result.notes) == 1
    assert "cliente" in result.notes[0]


@pytest.mark.unit
def test_only_cliente_available_records_site_unavailability():
    """Req 1.6: só o cliente disponível -> ingere cliente e registra site indisponível."""
    result = resolve_available_sources([DocumentSource.CLIENTE])

    assert result.to_ingest == ("cliente",)
    assert result.unavailable == ("site",)
    assert len(result.notes) == 1
    assert "site" in result.notes[0]


@pytest.mark.unit
def test_no_sources_available_records_both_unavailable():
    """Req 1.6: nenhuma fonte disponível -> nada ingerido e ambas registradas."""
    result = resolve_available_sources([])

    assert result.to_ingest == ()
    assert result.unavailable == ("site", "cliente")
    assert len(result.notes) == 2


@pytest.mark.unit
def test_accepts_string_source_values():
    """Req 1.5: aceita valores string das fontes além dos membros do enum."""
    result = resolve_available_sources(["site"])

    assert result.to_ingest == ("site",)
    assert result.unavailable == ("cliente",)


@pytest.mark.unit
def test_accepts_case_insensitive_string_values():
    """Req 1.5: normaliza a fonte informada independentemente de maiúsculas."""
    result = resolve_available_sources(["CLIENTE"])

    assert result.to_ingest == ("cliente",)
    assert result.unavailable == ("site",)


@pytest.mark.unit
def test_duplicate_sources_are_ignored():
    """Fontes duplicadas não alteram o resultado da resolução."""
    result = resolve_available_sources([DocumentSource.SITE, "site", DocumentSource.SITE])

    assert result.to_ingest == ("site",)
    assert result.unavailable == ("cliente",)


@pytest.mark.unit
def test_unknown_source_raises_value_error():
    """Req 1.5: uma fonte fora das suportadas é rejeitada explicitamente."""
    with pytest.raises(ValueError):
        resolve_available_sources(["desconhecida"])
