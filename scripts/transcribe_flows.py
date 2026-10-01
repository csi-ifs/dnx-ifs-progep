"""Transcreve os fluxogramas do Mapeamento de Processos da PROGEP (Req 1.5).

A página de mapeamento publica ~94 fluxos **como imagens PNG** (BPMN exportado
do Bizagi). A Knowledge Base do Bedrock não extrai texto de imagem, então elas
não podem ser ingeridas direto: entrariam como documentos vazios. Este script
lê cada fluxograma com o Claude Sonnet 4.5 (visão) e grava uma transcrição em
Markdown — esta sim indexável.

    página → PNG → Claude (visão) → Markdown → S3 → Ingestor → KB

**A transcrição é conteúdo derivado.** O assistente passa a responder com base
na leitura automática de um desenho, então cada arquivo carrega o link do
fluxograma original e um aviso de revisão pendente. Valide com a PROGEP antes
de tratar como fonte oficial.

Uso:
    aws configure export-credentials --format powershell | Invoke-Expression
    python scripts/transcribe_flows.py --out-dir docs/fluxos --limit 3   # amostra
    python scripts/transcribe_flows.py --out-dir docs/fluxos \
        --bucket progep-ifs-documents                                     # tudo + S3
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ingest_site import fetch  # noqa: E402  (reusa o fetch com percent-encoding)
from src.domain.config import USER_AGENT_APN  # noqa: E402

#: Página que lista os fluxos. Os links dos PNG estão no HTML estático (o aviso
#: de "habilite o JavaScript" no portal é genérico e não se aplica ao conteúdo).
MAPEAMENTO_URL = (
    "https://www.ifs.edu.br/guia-do-servidor/315-hotsite-progep/"
    "9741-mapeamento-de-processos.html"
)

#: Prefixo dos documentos transcritos no bucket da KB.
S3_PREFIX = "site/mapeamento-processos"

#: Inference profile do Claude Sonnet 4.5 (mesmo modelo usado pelo agente).
DEFAULT_MODEL = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: Limites de imagem do Bedrock: 3,75 MB e 8000 px por lado.
MAX_IMAGE_BYTES = 3_750_000
MAX_IMAGE_SIDE = 8000

#: Lado máximo antes de o pipeline de visão reduzir a imagem. Fluxograma BPMN
#: tem texto pequeno: um diagrama de 2800 px vira 1568 px e o texto cai para
#: ~55% — ilegível. Aí o modelo *não* marca ``[ilegível]``, ele preenche com
#: texto plausível (medido: raias e etapas inventadas no fluxo de Admissão).
#: Por isso as imagens grandes são fatiadas em pedaços que cabem sem redução.
VISION_MAX_SIDE = 1568

#: Lado máximo de cada fatia, com folga sobre o limite acima.
TILE_MAX_SIDE = 1450

#: Sobreposição entre fatias vizinhas, para nenhuma caixa ficar cortada ao meio.
TILE_OVERLAP = 0.14

#: Fração da largura ocupada pelos rótulos das raias, na margem esquerda.
LANE_STRIP_WIDTH = 0.10

PROMPT = """Você está transcrevendo um fluxograma BPMN oficial do Instituto \
Federal de Sergipe, do mapeamento de processos da Pró-Reitoria de Gestão de \
Pessoas (PROGEP). O resultado alimentará um assistente que responde dúvidas de \
servidores, então precisa ser fiel e completo.

Transcreva a imagem em Markdown, nesta estrutura:

## Resumo
Duas ou três frases explicando o que este processo faz, em linguagem acessível \
a um servidor que não conhece a estrutura interna da PROGEP.

## Setores envolvidos
Liste as raias (lanes) do diagrama, exatamente como aparecem.

## Etapas
Lista numerada, na ordem do fluxo. Em cada etapa indique o setor responsável \
entre colchetes. Exemplo: `1. [CCAD] Cadastrar admissão no SIAPE e SIGRH`.

## Pontos de decisão
Cada losango do diagrama: a pergunta e para onde leva cada resposta.

## Documentos e sistemas citados
Todo documento, formulário, sistema ou base que apareça (SEI, SIAPE, SIGRH, \
E-Pessoal, DOU, etc.).

## Início e fim
O evento inicial e o(s) evento(s) final(is) do processo.

Regras:
- Transcreva **apenas** o que está escrito no diagrama. Não complete com \
conhecimento próprio sobre o processo, não infira etapas que não estão \
desenhadas e não corrija o que parecer errado.
- **Nunca invente texto que você não consegue ler.** Se uma caixa, raia ou \
rótulo estiver ilegível, escreva `[ilegível]` — inclusive no nome de setor. \
Uma transcrição com lacunas admitidas é útil; uma com nomes plausíveis mas \
errados é pior que nenhuma, porque ninguém percebe o erro.
- Copie os nomes de setores, sistemas e etapas **exatamente** como aparecem, \
sem normalizar sigla nem corrigir grafia.
- Se a imagem não for um fluxograma, responda apenas: `NAO_E_FLUXOGRAMA`.
- Não comente a tarefa nem descreva a imagem ("o diagrama mostra..."). \
Comece direto pelo `## Resumo`."""

#: Sigla da coordenadoria embutida no caminho da imagem — mais confiável que o
#: cabeçalho da página, que nem sempre precede o link correspondente.
_SETOR_POR_PATH = {
    "CCAD": "Coordenadoria de Cadastro (CCAD/DAP)",
    "CDP": "Coordenadoria de Desenvolvimento de Pessoas (CDP/DSDPES)",
    "CADM": "Coordenadoria de Administração de Pessoal (CADP/DAP)",
    "COFP": "Coordenadoria de Folha de Pagamento (COFP/DAP)",
    "CSC": "Coordenadoria de Seleção e Contratos (CSC/DSDPES)",
    "COLEN": "Coordenação de Legislação e Normas (COLEN/DNAGS)",
    "DNAGS": "Coordenação de Legislação e Normas (COLEN/DNAGS)",
    "NAPA": "Coordenação de Legislação e Normas (COLEN/DNAGS)",
    "PG": "Programa de Gestão e Desempenho (PGD)",
}


def _slugify(texto: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


@dataclass(frozen=True)
class Fluxo:
    """Um fluxograma publicado no mapeamento de processos."""

    titulo: str
    setor: str
    url: str
    #: Desempate quando dois fluxos distintos têm o mesmo título — acontece de
    #: verdade: "Isenção de Imposto de Renda" existe na CCAD e na COLEN, com
    #: diagramas diferentes. Sem isso um arquivo sobrescreve o outro em silêncio.
    sufixo: str = ""

    @property
    def slug(self) -> str:
        base = _slugify(self.titulo)[:80] or "fluxo"
        return f"{base}-{self.sufixo}" if self.sufixo else base


class _ContentParser(HTMLParser):
    """Coleta, em ordem, o texto e as âncoras de imagem da página."""

    def __init__(self) -> None:
        super().__init__()
        self.stream: list[tuple[str, str]] = []  # ("texto"|"link", valor)
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href") or ""
        self._href = href if re.search(r"\.(png|jpe?g)$", href, re.I) else None
        self._label = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.stream.append(("link", f"{''.join(self._label).strip()}\t{self._href}"))
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)
        text = data.strip()
        if text:
            self.stream.append(("texto", text))


def setor_for(url: str, fallback: str) -> str:
    """Deriva a coordenadoria a partir do caminho da imagem."""
    parts = {p.upper() for p in urlparse(url).path.split("/")}
    for sigla, nome in _SETOR_POR_PATH.items():
        if sigla in parts:
            return nome
    return fallback


#: Preâmbulo dos cabeçalhos de seção, removido para o rótulo ficar legível.
_PREFIXO_SECAO = re.compile(
    r"^Fluxos?\s+(d[eo]s?\s+)?(Processos\s+que\s+Tramitam\s+)?(n[ao]s?\s+|sobre\s+o\s+)?",
    re.I,
)


def parse_fluxos(page_url: str, html_text: str) -> list[Fluxo]:
    """Extrai os fluxogramas listados na página de mapeamento."""
    parser = _ContentParser()
    parser.feed(html_text)

    fluxos: list[Fluxo] = []
    seen: set[str] = set()
    secao = "PROGEP"
    # O cabeçalho da seção vem quebrado em vários nós de texto ("Fluxo dos
    # Processos que Tramitam na" + "Coordenadoria de Cadastro (CCAD)/DAP:"),
    # então acumula os fragmentos até fechar com dois-pontos.
    buffer: list[str] = []
    for kind, value in parser.stream:
        if kind == "texto":
            if re.match(r"^Fluxos?\b|^PROGEP:", value, re.I) and len(value) < 220:
                buffer = [value]
            elif buffer and len(" ".join(buffer)) < 220:
                buffer.append(value)
            else:
                continue
            texto = " ".join(buffer).strip()
            if texto.endswith(":"):
                secao = _PREFIXO_SECAO.sub("", texto.rstrip(":")).strip() or texto
                buffer = []
            continue
        label, href = value.split("\t", 1)
        absolute = urljoin(page_url, href)
        titulo = re.sub(r"^[-\s]+|[-\s]+$", "", label)
        if not titulo or absolute in seen:
            continue
        seen.add(absolute)
        fluxos.append(Fluxo(titulo, setor_for(absolute, secao), absolute))
    return _desambiguar(fluxos)


def _desambiguar(fluxos: list[Fluxo]) -> list[Fluxo]:
    """Garante um slug único por fluxo, sem depender da ordem da página.

    O desempate usa o diretório da imagem (``.../CCAD/x.png`` → ``ccad``), que
    é estável e legível; só cai para um hash da URL se ainda houver empate.
    """
    from collections import Counter
    from hashlib import sha1

    repetidos = {s for s, n in Counter(f.slug for f in fluxos).items() if n > 1}
    if not repetidos:
        return fluxos

    resultado: list[Fluxo] = []
    usados: set[str] = {f.slug for f in fluxos if f.slug not in repetidos}
    for fluxo in fluxos:
        if fluxo.slug not in repetidos:
            resultado.append(fluxo)
            continue
        pasta = _slugify(urlparse(fluxo.url).path.split("/")[-2] if "/" in fluxo.url else "")
        candidato = Fluxo(fluxo.titulo, fluxo.setor, fluxo.url, pasta or "")
        if not pasta or candidato.slug in usados:
            digest = sha1(fluxo.url.encode("utf-8")).hexdigest()[:6]
            candidato = Fluxo(fluxo.titulo, fluxo.setor, fluxo.url, digest)
        usados.add(candidato.slug)
        resultado.append(candidato)
    return resultado


def _encode(img) -> bytes:
    """Serializa em PNG, reduzindo só se estourar o limite de bytes."""
    from PIL import Image

    scale = 1.0
    for _ in range(6):
        candidate = img
        if scale < 1.0:
            candidate = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        candidate.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= MAX_IMAGE_BYTES:
            return data
        scale *= 0.8
    raise ValueError("imagem não coube no limite de bytes do Bedrock")


def _fit(img, max_side: int):
    """Reduz proporcionalmente para caber em ``max_side``."""
    from PIL import Image

    if max(img.size) <= max_side:
        return img
    scale = max_side / max(img.size)
    return img.resize(
        (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
        Image.LANCZOS,
    )


def build_tiles(raw: bytes) -> tuple[list[bytes], int, int]:
    """Fatia o fluxograma em pedaços legíveis pelo modelo.

    Devolve ``(imagens, colunas, linhas)``. A primeira imagem é sempre uma
    **visão geral** do diagrama inteiro (reduzida): ela dá a estrutura e os
    rótulos das raias. As seguintes são as fatias em resolução nativa, na ordem
    esquerda→direita, cima→baixo, com sobreposição.

    Quando o diagrama já cabe sem redução, devolve só ele — sem fatiar.
    """
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as opened:
        img = opened.convert("RGB")

        if max(img.size) <= VISION_MAX_SIDE:
            return [_encode(img)], 1, 1

        cols = max(1, -(-img.width // TILE_MAX_SIDE))
        rows = max(1, -(-img.height // TILE_MAX_SIDE))
        step_x = img.width / cols
        step_y = img.height / rows
        pad_x = step_x * TILE_OVERLAP
        pad_y = step_y * TILE_OVERLAP

        # No BPMN os rótulos das raias ficam girados 90° numa faixa estreita à
        # esquerda. Mesmo em resolução nativa o modelo erra esses nomes (DAP
        # lido como "SIAP"). Mandar a faixa isolada e já rotacionada resolve.
        strip = img.crop((0, 0, max(1, int(img.width * LANE_STRIP_WIDTH)), img.height))
        images = [
            _encode(_fit(img, VISION_MAX_SIDE)),
            _encode(_fit(strip.rotate(-90, expand=True), VISION_MAX_SIDE)),
        ]
        for row in range(rows):
            for col in range(cols):
                box = (
                    int(max(0, col * step_x - pad_x)),
                    int(max(0, row * step_y - pad_y)),
                    int(min(img.width, (col + 1) * step_x + pad_x)),
                    int(min(img.height, (row + 1) * step_y + pad_y)),
                )
                images.append(_encode(_fit(img.crop(box), VISION_MAX_SIDE)))
        return images, cols, rows


def build_client(region: str):
    """Cliente Bedrock com a anotação de billing obrigatória (Req 8.3)."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            user_agent_extra=USER_AGENT_APN,
            retries={"max_attempts": 5, "mode": "adaptive"},
            read_timeout=300,
        ),
    )


def _tiling_note(count: int, cols: int, rows: int) -> str:
    """Explica ao modelo como as imagens enviadas se relacionam."""
    if count == 1:
        return "Você recebeu **uma** imagem: o fluxograma completo."
    return (
        f"Você recebeu {count} imagens do MESMO fluxograma.\n"
        f"- A **imagem 1** é a visão geral reduzida: use-a para entender a "
        f"estrutura e o encadeamento — o texto nela está pequeno demais para "
        f"ser transcrito.\n"
        f"- A **imagem 2** é a faixa da margem esquerda, girada para leitura "
        f"horizontal: são os **rótulos das raias**, de cima para baixo. Tire "
        f"os nomes dos setores **daqui**, não das outras imagens.\n"
        f"- As **imagens 3 a {count}** são recortes em resolução nativa, "
        f"dispostos em {cols} coluna(s) por {rows} linha(s), na ordem "
        f"esquerda→direita e cima→baixo. **Transcreva as etapas a partir "
        f"delas.**\n"
        f"- Os recortes se sobrepõem nas bordas: uma mesma caixa pode aparecer "
        f"em dois recortes. Não a repita na transcrição."
    )


def transcribe(
    client, model_id: str, images: list[bytes], cols: int, rows: int
) -> str:
    """Pede ao modelo a transcrição estruturada de um fluxograma."""
    content: list[dict] = [
        {"image": {"format": "png", "source": {"bytes": img}}} for img in images
    ]
    content.append({"text": f"{_tiling_note(len(images), cols, rows)}\n\n{PROMPT}"})

    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 8192, "temperature": 0},
    )
    blocks = response["output"]["message"]["content"]
    return "".join(b.get("text", "") for b in blocks).strip()


def render_document(fluxo: Fluxo, corpo: str) -> str:
    """Monta o Markdown final, com procedência explícita."""
    return "\n".join(
        [
            f"# {fluxo.titulo}",
            "",
            f"**Setor responsável:** {fluxo.setor}  ",
            "**Fonte:** Mapeamento de Processos da PROGEP — IFS  ",
            f"**Fluxograma original:** {unquote(fluxo.url)}  ",
            f"**Transcrito automaticamente em:** {date.today().isoformat()}",
            "",
            "> Transcrição automática do fluxograma oficial, feita por leitura de"
            " imagem. Em caso de divergência, vale o fluxograma original."
            " Pendente de validação pela PROGEP.",
            "",
            "---",
            "",
            corpo,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("docs/fluxos"))
    parser.add_argument("--bucket", default="", help="Se informado, sobe os .md para o S3")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--model-id", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=0, help="Processa só os N primeiros")
    parser.add_argument("--filter", default="", help="Só fluxos cujo título contém o texto")
    parser.add_argument("--force", action="store_true", help="Refaz transcrições existentes")
    parser.add_argument("--list", action="store_true", help="Só lista os fluxos e sai")
    args = parser.parse_args(argv)

    try:
        # line_buffering: o lote leva ~45 min e, sem isso, redirecionar a saída
        # para arquivo esconde todo o progresso até o processo terminar.
        sys.stdout.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    print(f"[1/3] Lendo {MAPEAMENTO_URL}")
    fluxos = parse_fluxos(MAPEAMENTO_URL, fetch(MAPEAMENTO_URL).decode("utf-8", "replace"))
    if args.filter:
        fluxos = [f for f in fluxos if args.filter.lower() in f.titulo.lower()]
    if args.limit:
        fluxos = fluxos[: args.limit]
    print(f"      {len(fluxos)} fluxograma(s)")

    if args.list:
        for f in fluxos:
            print(f"  [{f.setor}] {f.titulo}\n      {f.slug}.md")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = build_client(args.region)
    s3 = None
    if args.bucket:
        import boto3

        s3 = boto3.client("s3", region_name=args.region)

    print(f"[2/3] Transcrevendo com {args.model_id}")
    ok = skipped = failed = 0
    manifest: list[dict[str, str]] = []

    for i, fluxo in enumerate(fluxos, 1):
        destino = args.out_dir / f"{fluxo.slug}.md"
        if destino.exists() and not args.force:
            print(f"  [{i}/{len(fluxos)}] {fluxo.titulo[:60]} — já existe, pulando")
            skipped += 1
            manifest.append({"titulo": fluxo.titulo, "arquivo": destino.name,
                             "url": fluxo.url, "setor": fluxo.setor})
            continue

        print(f"  [{i}/{len(fluxos)}] {fluxo.titulo[:60]}")
        started = time.monotonic()
        try:
            images, cols, rows = build_tiles(fetch(fluxo.url))
            corpo = transcribe(client, args.model_id, images, cols, rows)
        except Exception as exc:  # a bateria segue mesmo se um fluxo falhar
            print(f"      ERRO: {exc}")
            failed += 1
            continue

        if corpo.strip() == "NAO_E_FLUXOGRAMA" or len(corpo) < 120:
            print("      descartado: modelo não reconheceu um fluxograma")
            failed += 1
            continue

        destino.write_text(render_document(fluxo, corpo), encoding="utf-8")
        manifest.append({"titulo": fluxo.titulo, "arquivo": destino.name,
                         "url": fluxo.url, "setor": fluxo.setor})
        ok += 1
        print(f"      ok ({round(time.monotonic() - started, 1)}s, {len(corpo)} chars)")

        if s3 is not None:
            s3.put_object(
                Bucket=args.bucket,
                Key=f"{S3_PREFIX}/{destino.name}",
                Body=destino.read_bytes(),
                ContentType="text/markdown; charset=utf-8",
            )

    (args.out_dir / "_manifesto.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[3/3] {ok} transcrito(s), {skipped} pulado(s), {failed} falha(s)")
    if s3 is not None and ok:
        print(f"      enviados para s3://{args.bucket}/{S3_PREFIX}/")
        print("      o Ingestor dispara o job da KB automaticamente")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
