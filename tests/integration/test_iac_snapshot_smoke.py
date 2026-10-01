"""Snapshot & smoke checks over the pure IaC model (Task 13.3).

A real ``pulumi preview`` requires a configured Pulumi backend/stack and the
``pulumi``/``pulumi_aws`` CLIs & packages, which are not available in this
environment. These tests therefore act as *snapshot* and *smoke* checks over the
**pure, Pulumi-free IaC model** (:mod:`src.infra.provider`,
:mod:`src.infra.resources` and :data:`src.domain.config.APN_TAG`) without ever
invoking the Pulumi CLI. They assert the same invariants a preview snapshot would
guard:

* **Single global tagging definition (Req 8.1, 8.4)** — :func:`build_default_tags`
  always carries the ``Tag_APN`` (``aws-apn-id: pc:...``) and the APN tag cannot
  be overridden by caller-supplied ``extra_tags``.
* **100% of taggable resources covered (Req 8.4, 9.4)** — every taggable
  :class:`ResourceSpec` inherits the provider ``default_tags``, so the provider
  args carry ``default_tags.tags`` equal to the single global APN set.
* **Two origin data sources supported (Req 1.5)** — ``DocumentSource`` exposes
  SITE and CLIENTE and the ingestion model references both.
* **Single stack/environment per execution (Req 9.2)** —
  :func:`enforce_single_environment` returns the one env and refuses zero/many.
* **All resources defined in code (Req 9.1, supported by 9.3)** —
  ``defined_resource_keys() == required_resource_keys()`` and a missing key is
  blocked with :class:`MissingResourceError`.
* **Secret scanner (Req 10.1)** — no hardcoded secrets/credentials in ``src/``.
* **Idempotency (Req 9.5)** — the pure builders are deterministic (a proxy for
  "re-running the IaC without changes yields no diff").
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.domain.config import APN_TAG, DocumentSource
from src.domain.ingestion import resolve_available_sources
from src.infra.provider import (
    MissingEnvironmentError,
    MultipleEnvironmentsError,
    apn_default_tags,
    build_default_tags,
    build_provider_args,
    enforce_single_environment,
)
from src.infra import resources as R

pytestmark = pytest.mark.integration


# --- Global single tagging definition (Req 8.1, 8.4) ----------------------------


def test_default_tags_always_contain_apn_tag():
    """build_default_tags always carries the single global APN tag (Req 8.4)."""
    tags = build_default_tags()
    assert APN_TAG.items() <= tags.items()
    assert tags["aws-apn-id"].startswith("pc:")
    # The canonical single tagging definition equals the APN tag set.
    assert apn_default_tags() == dict(APN_TAG)


def test_apn_tag_cannot_be_overridden_by_extra_tags():
    """extra_tags may add tags but can never override the APN tag (Req 8.1)."""
    hijack = {"aws-apn-id": "pc:ATTACKER", "Environment": "prod"}
    tags = build_default_tags(hijack)
    # APN tag wins; the malicious override is ignored.
    assert tags["aws-apn-id"] == APN_TAG["aws-apn-id"]
    # Non-conflicting extra tags are preserved alongside the APN tag.
    assert tags["Environment"] == "prod"


def test_apn_default_tags_returns_defensive_copy():
    """Mutating the returned map must not corrupt the shared APN constant."""
    tags = apn_default_tags()
    tags["aws-apn-id"] = "pc:MUTATED"
    assert APN_TAG["aws-apn-id"] != "pc:MUTATED"
    assert apn_default_tags()["aws-apn-id"] == APN_TAG["aws-apn-id"]


# --- 100% taggable resources covered by the single definition (Req 8.4, 9.4) ----


def test_all_taggable_resources_are_covered_by_provider_default_tags():
    """Every taggable resource inherits the provider default APN tags (Req 9.4)."""
    taggable = [s for s in R.REQUIRED_RESOURCE_SPECS if s.taggable]
    non_taggable = [s for s in R.REQUIRED_RESOURCE_SPECS if not s.taggable]
    # There are taggable resources; non-taggable resources are handled via the
    # tagging-exception registry (Req 8.6) and are explicitly known.
    assert taggable, "expected at least one taggable resource in the catalog"
    assert {s.key for s in non_taggable} == {"kb_data_source"}

    # The provider args carry the single global tag set applied to all resources.
    args = build_provider_args()
    assert args["default_tags"]["tags"] == build_default_tags()
    assert args["default_tags"]["tags"] == dict(APN_TAG)


def test_taggable_resources_span_the_expected_services():
    """Smoke: the taggable catalog covers the solution's core AWS services."""
    services = {s.service for s in R.REQUIRED_RESOURCE_SPECS if s.taggable}
    expected = {
        "s3",
        "s3vectors",
        "bedrock",
        "dynamodb",
        "cognito",
        "apigateway",
        "lambda",
        "connect",
        "socialmessaging",
        "secretsmanager",
        "cloudwatch",
        "cloudtrail",
        "iam",
    }
    assert expected <= services


# --- Two origin data sources supported (Req 1.5) --------------------------------


def test_two_data_sources_site_and_cliente_supported():
    """The ingestion model supports exactly the site + cliente data sources."""
    values = {src.value for src in DocumentSource}
    assert values == {"site", "cliente"}
    assert DocumentSource.SITE.value == "site"
    assert DocumentSource.CLIENTE.value == "cliente"


def test_ingestion_references_both_data_sources():
    """When both sources are available, both are ingested (Req 1.5)."""
    resolution = resolve_available_sources(
        [DocumentSource.SITE, DocumentSource.CLIENTE]
    )
    assert set(resolution.to_ingest) == {"site", "cliente"}
    assert resolution.unavailable == ()
    assert resolution.notes == ()


# --- Single stack / environment per execution (Req 9.2) -------------------------


def test_single_environment_returns_the_one_env():
    assert enforce_single_environment(["prod"]) == "prod"
    # Duplicates of the same env collapse to a single distinct environment.
    assert enforce_single_environment(["prod", "prod"]) == "prod"


def test_zero_environments_is_rejected():
    with pytest.raises(MissingEnvironmentError):
        enforce_single_environment([])
    with pytest.raises(MissingEnvironmentError):
        enforce_single_environment(["", "   "])


def test_multiple_environments_are_rejected():
    with pytest.raises(MultipleEnvironmentsError):
        enforce_single_environment(["prod", "staging"])


# --- All resources defined in code (Req 9.1, supported by 9.3) ------------------


def test_all_required_resources_are_defined_in_code():
    """defined == required: the program declares every resource (Req 9.1)."""
    assert R.defined_resource_keys() == R.required_resource_keys()
    # No orphan/duplicate keys in the catalog.
    assert len(R.REQUIRED_RESOURCE_SPECS) == len(R.required_resource_keys())


def test_missing_resource_blocks_provisioning():
    """A missing required resource blocks with identification (Req 9.3 -> 9.1)."""
    present = R.required_resource_keys() - {"knowledge_base"}
    with pytest.raises(R.MissingResourceError) as exc:
        R.validate_required_resources(present)
    assert "knowledge_base" in exc.value.missing


# --- Secret scanner over the src/ tree (Req 10.1) -------------------------------

#: Values that look secret-ish but are known, non-secret constants (APN billing
#: annotations). They must never be flagged by the scanner.
_ALLOWLISTED_SUBSTRINGS = (
    APN_TAG["aws-apn-id"],  # aws-apn-id billing tag (public APN identifier)
    "APN_1.1/pc_",  # User-Agent APN annotation prefix
)

#: Patterns that indicate a hardcoded secret/credential literal. Each requires an
#: actual embedded value (quoted literal or fixed key format) so that references
#: to variables (e.g. ``aws_secret_access_key=creds.secret_access_key``) and enum
#: labels (e.g. ``SECRET_ACCESS = "secret_access"``) are not false positives.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # AWS access key IDs (AKIA/ASIA + 16 uppercase alphanumerics).
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # PEM private key blocks.
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # A secret access key assigned to a string literal.
    (
        "aws-secret-access-key-literal",
        re.compile(r"aws_secret_access_key\s*=\s*[\"'][A-Za-z0-9/+=]{16,}[\"']"),
    ),
    # Generic password / api-key / auth-token assigned to a quoted literal.
    (
        "hardcoded-credential-literal",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|auth[_-]?token)\b"
            r"\s*[:=]\s*[\"'][^\"']{8,}[\"']"
        ),
    ),
)


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"src tree not found at {root}"
    return root


def _scan_line(line: str) -> list[str]:
    """Return the names of secret patterns matched by ``line`` (allowlist-aware)."""
    scrubbed = line
    for safe in _ALLOWLISTED_SUBSTRINGS:
        scrubbed = scrubbed.replace(safe, "")
    return [name for name, pat in _SECRET_PATTERNS if pat.search(scrubbed)]


def test_no_hardcoded_secrets_in_source_tree():
    """Scan src/ for hardcoded secrets/credentials; assert none present (Req 10.1)."""
    findings: list[str] = []
    for path in sorted(_src_root().rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name in _scan_line(line):
                findings.append(f"{path}:{lineno}: {name}: {line.strip()}")

    assert not findings, "hardcoded secrets detected:\n" + "\n".join(findings)


def test_scanner_detects_a_planted_secret():
    """Self-check: the scanner would catch real credential patterns."""
    assert _scan_line('aws_secret_access_key = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLE"')
    assert _scan_line("key = AKIAIOSFODNN7EXAMPLE")
    assert _scan_line('password = "hunter2super"')
    assert _scan_line("-----BEGIN RSA PRIVATE KEY-----")
    # The APN constants are not secrets.
    assert not _scan_line(f'APN_TAG = {{"aws-apn-id": "{APN_TAG["aws-apn-id"]}"}}')
    assert not _scan_line('USER_AGENT_APN = "APN_1.1/pc_7b0pljcsg0gii1a5gn5vfca0v$"')


# --- Idempotency: deterministic builders are a no-diff proxy (Req 9.5) ----------


def test_build_default_tags_is_deterministic():
    assert build_default_tags() == build_default_tags()
    assert build_default_tags({"Team": "progep"}) == build_default_tags({"Team": "progep"})


def test_build_provider_args_is_deterministic():
    assert build_provider_args(region="us-east-1") == build_provider_args(region="us-east-1")


def test_component_policy_documents_are_deterministic():
    """Re-rendering the IAM policy documents yields identical results (Req 9.5)."""
    assert R.component_policy_documents() == R.component_policy_documents()
    ctx = R.ArnContext(region="us-east-1", account_id="123456789012")
    assert R.component_policy_documents(ctx) == R.component_policy_documents(ctx)
