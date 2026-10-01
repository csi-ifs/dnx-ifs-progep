"""Pulumi entrypoint at the repository root.

Keeps the repo root as the program's working directory so the ``src`` package
(and the Lambda ``FileArchive("./src")``) resolve correctly, then delegates to
the real program in :mod:`src.infra.__main__`.
"""

from src.infra.__main__ import main

main()
