"""Component wiring for the Assistente PROGEP-IFS (integration layer).

This package contains the orchestration/"wiring" that connects the pure-logic
domain (:mod:`src.domain`) with the thin I/O adapters (:mod:`src.adapters`) into
end-to-end flows. The individual flow modules are imported directly (e.g.
``from src.flows.site_flow import SiteFlow``) to keep this package's import side
effects minimal.
"""
