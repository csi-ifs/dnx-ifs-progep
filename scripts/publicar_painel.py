"""Publica o painel de acervo: build, config.json, sync e invalidação.

O ``config.json`` é gerado a partir dos **outputs do Pulumi**, não escrito à
mão: um identificador de pool errado num artefato já publicado só apareceria na
tela de login de quem tentasse entrar, sem nada apontando a causa.

Ele fica **fora do bundle** de propósito — a mesma build serve outra conta sem
recompilar.

Uso:
    aws login
    python scripts/publicar_painel.py
    python scripts/publicar_painel.py --pular-build   # só reenvia o dist atual
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
FRONTEND = RAIZ / "frontend"
DIST = FRONTEND / "dist"

#: Arquivos que **não** podem ficar em cache longo. O `index.html` aponta para o
#: bundle com hash no nome; se ele ficasse cacheado, o navegador continuaria
#: carregando a versão antiga mesmo depois da invalidação dos assets.
SEM_CACHE = ("index.html", "config.json")


def _saida_pulumi(nome: str) -> str:
    resultado = subprocess.run(
        ["pulumi", "stack", "output", nome, "--cwd", str(RAIZ)],
        capture_output=True,
        text=True,
        env={**os.environ, "PULUMI_CONFIG_PASSPHRASE": "progep-ifs-local"},
    )
    if resultado.returncode != 0:
        raise SystemExit(f"output '{nome}' não encontrado: {resultado.stderr.strip()}")
    return resultado.stdout.strip()


def _rodar(comando: list[str], cwd: Path) -> None:
    print(f"  $ {' '.join(comando)}")
    if subprocess.run(comando, cwd=cwd, shell=(os.name == "nt")).returncode != 0:
        raise SystemExit(f"falhou: {' '.join(comando)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pular-build", action="store_true")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    print("== outputs do Pulumi ==")
    bucket = _saida_pulumi("painel_bucket")
    distribuicao = _saida_pulumi("painel_distribuicao_id")
    url = _saida_pulumi("painel_url")
    config = {
        "regiao": _saida_pulumi("region"),
        "poolId": _saida_pulumi("cognito_user_pool_id"),
        "clienteId": _saida_pulumi("cognito_client_id"),
        # `invoke_url` já vem com o stage; a barra final duplicaria a do caminho.
        "api": _saida_pulumi("api_url").rstrip("/"),
    }
    for chave, valor in config.items():
        print(f"  {chave}: {valor}")

    if not args.pular_build:
        print("\n== build ==")
        _rodar(["npm", "install"], FRONTEND)
        _rodar(["npm", "run", "build"], FRONTEND)
    if not DIST.exists():
        raise SystemExit(f"{DIST} não existe — rode sem --pular-build.")

    (DIST / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n== publicando ==")
    # Duas passadas: os arquivos com hash no nome ganham cache longo (o nome
    # muda quando o conteúdo muda), o resto não é cacheado. Uma passada só
    # obrigaria a escolher entre servir versão velha ou não ter cache nenhum.
    _rodar(
        ["aws", "s3", "sync", str(DIST), f"s3://{bucket}", "--delete",
         "--cache-control", "public,max-age=31536000,immutable",
         *sum((["--exclude", n] for n in SEM_CACHE), [])],
        RAIZ,
    )
    for nome in SEM_CACHE:
        if (DIST / nome).exists():
            _rodar(
                ["aws", "s3", "cp", str(DIST / nome), f"s3://{bucket}/{nome}",
                 "--cache-control", "no-cache"],
                RAIZ,
            )

    print("\n== invalidando o cache ==")
    _rodar(
        ["aws", "cloudfront", "create-invalidation",
         "--distribution-id", distribuicao, "--paths", "/*",
         "--query", "Invalidation.Status", "--output", "text"],
        RAIZ,
    )

    print(f"\nPainel publicado: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
