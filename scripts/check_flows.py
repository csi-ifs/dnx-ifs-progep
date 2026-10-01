"""Varredura de qualidade das transcrições de fluxograma, antes de ingerir.

A transcrição é leitura automática de imagem: ela erra em silêncio. Este script
não valida o conteúdo (só a PROGEP pode), mas separa os arquivos que merecem
olhada humana antes de virarem fonte de resposta do assistente.

Sinais checados:

* **curto demais** — leitura falhou e o modelo devolveu pouca coisa;
* **seções faltando** — a estrutura pedida no prompt não veio completa;
* **``[ilegível]``** — o modelo admitiu não ler (bom sinal de honestidade,
  mas o arquivo precisa de conferência);
* **setor desconhecido** — sigla fora das coordenadorias reais da PROGEP, que
  é o sintoma clássico de raia inventada;
* **etapas repetidas** — sobreposição entre fatias vazando para a transcrição.

Uso:
    python scripts/check_flows.py --dir docs/fluxos
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

#: Siglas que realmente existem na estrutura da PROGEP (mais os órgãos externos
#: que aparecem nos fluxos). Qualquer outra sigla em "Setores envolvidos" é
#: suspeita de invenção — foi assim que apareceram "DAC", "SUP" e "CDPT".
SETORES_CONHECIDOS = {
    "CCAD", "CDP", "CADP", "CADM", "COFP", "CSC", "COLEN", "DNAGS", "DAP",
    "DSDPES", "PROGEP", "CGU", "SIASS", "CPPD", "CIS", "NAPA",
    "GABINETE", "REITORIA", "REITOR", "SERVIDOR", "PROTOCOLO", "ARQUIVO",
    "CHEFIA", "CAMPUS", "DIRETORIA", "COMISSAO", "SETOR", "PRODIN", "DGP",
    "SEI", "SIAPE", "MEC", "MGI", "TCU", "AGU", "GEAP", "JUSTICA", "SUSEP",
}

SECOES_ESPERADAS = (
    "## Resumo",
    "## Setores envolvidos",
    "## Etapas",
    "## Pontos de decisão",
    "## Documentos e sistemas citados",
    "## Início e fim",
)

#: Abaixo disso a transcrição quase certamente falhou.
MIN_CHARS = 900


def siglas_do_bloco(texto: str) -> set[str]:
    """Siglas declaradas no bloco 'Setores envolvidos'.

    Só considera tokens que **já vêm em caixa alta** no texto: maiusculizar
    tudo transformaria "da", "de", "junta médica" em siglas e afogaria o
    relatório em falso positivo.
    """
    match = re.search(r"## Setores envolvidos\n(.*?)(?=\n## |\Z)", texto, re.S)
    if not match:
        return set()
    siglas: set[str] = set()
    for linha in match.group(1).splitlines():
        item = linha.strip().lstrip("-*").strip()
        if not item or item.startswith("["):
            continue
        for palavra in re.findall(r"\b[A-ZÀ-Ú]{2,7}\b", item):
            siglas.add(palavra)
    return siglas


def etapas_repetidas(texto: str) -> list[tuple[str, int]]:
    """Etapas idênticas que aparecem mais de uma vez (vazamento de fatia)."""
    match = re.search(r"## Etapas\n(.*?)(?=\n## |\Z)", texto, re.S)
    if not match:
        return []
    etapas = [
        re.sub(r"^\d+\.\s*", "", linha.strip()).strip()
        for linha in match.group(1).splitlines()
        if re.match(r"^\s*\d+\.", linha)
    ]
    return [(t, n) for t, n in Counter(etapas).items() if n > 1 and len(t) > 12]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("docs/fluxos"))
    parser.add_argument(
        "--strict", action="store_true",
        help="Sai com código 1 se qualquer arquivo tiver sinal de alerta",
    )
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    arquivos = sorted(p for p in args.dir.glob("*.md"))
    if not arquivos:
        print(f"nenhum .md em {args.dir}")
        return 1

    textos = {p: p.read_text(encoding="utf-8") for p in arquivos}

    # Uma coordenadoria real aparece em vários fluxos; uma raia inventada pela
    # leitura de imagem aparece uma vez só. Essa frequência é um sinal melhor
    # que qualquer lista fixa — e se auto-calibra quando o corpus cresce.
    frequencia: Counter[str] = Counter()
    for texto in textos.values():
        frequencia.update(siglas_do_bloco(texto))

    alertas: dict[str, list[str]] = {}
    total_ilegivel = 0

    for caminho in arquivos:
        texto = textos[caminho]
        problemas: list[str] = []

        corpo = texto.split("---", 2)[-1]
        if len(corpo) < MIN_CHARS:
            problemas.append(f"curto demais ({len(corpo)} chars)")

        faltando = [s for s in SECOES_ESPERADAS if s not in texto]
        if faltando:
            problemas.append("seções faltando: " + ", ".join(s[3:] for s in faltando))

        n_ilegivel = texto.count("[ilegível]")
        total_ilegivel += n_ilegivel
        if n_ilegivel:
            problemas.append(f"{n_ilegivel}x [ilegível]")

        # Suspeita = sigla que não está na lista conhecida E aparece em um
        # único fluxo de todo o conjunto.
        desconhecidas = {
            s for s in siglas_do_bloco(texto)
            if s not in SETORES_CONHECIDOS and frequencia[s] <= 1
        }
        if desconhecidas:
            problemas.append("setor único no corpus: " + ", ".join(sorted(desconhecidas)))

        # Etapa repetida é normal em BPMN ("Receber e analisar processo" existe
        # em várias raias); só alerta quando a repetição é volumosa.
        repetidas = etapas_repetidas(texto)
        excedente = sum(n - 1 for _, n in repetidas)
        if excedente >= 3:
            problemas.append(f"{excedente} etapa(s) duplicada(s)")

        if problemas:
            alertas[caminho.name] = problemas

    print(f"{len(arquivos)} transcrição(ões) em {args.dir}")
    print(f"{len(arquivos) - len(alertas)} sem alerta, {len(alertas)} para conferir")
    print(f"{total_ilegivel} marcação(ões) de [ilegível] no conjunto\n")

    for nome, problemas in sorted(alertas.items()):
        print(f"  {nome}")
        for p in problemas:
            print(f"      - {p}")

    return 1 if (args.strict and alertas) else 0


if __name__ == "__main__":
    sys.exit(main())
