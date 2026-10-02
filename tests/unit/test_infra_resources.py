"""Unit tests for the pure helpers of the IaC resource module (Task 13.2).

Covers the Pulumi-free logic that must be testable regardless of whether the
``pulumi``/``pulumi_aws`` packages are installed:

* required-resource presence validation with blocking + identification (Req 9.3);
* failure/rollback identification, including ``Tag_APN`` application failure
  (Req 8.2, 9.6);
* least-privilege IAM policies built via :mod:`src.domain.iam` (Req 10.2).
"""

from __future__ import annotations

import pytest

from src.domain.iam import validate_policy
from src.infra import resources as R

pytestmark = pytest.mark.unit


# --- Required-resource presence (Req 9.3) --------------------------------------


def test_defined_keys_match_required_catalog():
    assert R.defined_resource_keys() == R.required_resource_keys()
    assert len(R.REQUIRED_RESOURCE_SPECS) == len(R.required_resource_keys())


def test_validate_passes_when_all_required_present():
    # Should not raise when every required resource is defined.
    R.validate_required_resources(R.required_resource_keys())


def test_validate_blocks_and_identifies_missing_resource():
    present = R.required_resource_keys() - {"knowledge_base", "session_table"}
    with pytest.raises(R.MissingResourceError) as exc:
        R.validate_required_resources(present)
    assert set(exc.value.missing) == {"knowledge_base", "session_table"}
    # The error message identifies the missing resources (Req 9.3).
    assert "knowledge_base" in str(exc.value)
    assert "session_table" in str(exc.value)


def test_validate_blocks_on_empty_definition():
    with pytest.raises(R.MissingResourceError) as exc:
        R.validate_required_resources([])
    assert set(exc.value.missing) == set(R.required_resource_keys())


def test_resource_spec_lookup_and_unknown():
    spec = R.resource_spec("documents_bucket")
    assert spec.service == "s3" and spec.taggable is True
    with pytest.raises(KeyError):
        R.resource_spec("does_not_exist")


# --- Failure / rollback analysis (Req 8.2, 9.6) --------------------------------


def _o(rid: str, status: str, detail: str | None = None) -> R.ProvisionOutcome:
    return R.ProvisionOutcome(resource_id=rid, status=status, detail=detail)


def test_all_provisioned_reports_success():
    rep = R.analyze_provisioning([_o("a", R.PROVISIONED), _o("b", R.PROVISIONED)])
    assert rep.ok is True
    assert rep.preserved == ("a", "b")
    assert rep.failed_resource is None
    assert rep.rolled_back == ()


def test_provisioning_failure_preserves_prior_and_identifies_resource():
    rep = R.analyze_provisioning(
        [_o("a", R.PROVISIONED), _o("b", R.FAILED, "boom"), _o("c", R.PROVISIONED)]
    )
    assert rep.ok is False
    assert rep.failed_resource == "b"
    assert rep.failure_kind == R.FAILURE_PROVISIONING
    # Resources provisioned before the failure are preserved (Req 9.6).
    assert rep.preserved == ("a",)
    assert rep.rolled_back == ()
    assert "b" in rep.message


def test_tag_application_failure_rolls_back_affected_resource():
    rep = R.analyze_provisioning(
        [_o("a", R.PROVISIONED), _o("b", R.TAG_FAILED, "tag err")]
    )
    assert rep.ok is False
    assert rep.failed_resource == "b"
    assert rep.failure_kind == R.FAILURE_TAGGING
    # Affected resource is rolled back; prior ones preserved (Req 8.2).
    assert rep.rolled_back == ("b",)
    assert rep.preserved == ("a",)
    assert "Tag_APN" in rep.message


def test_raise_on_failure_raises_with_report():
    with pytest.raises(R.ProvisioningError) as exc:
        R.raise_on_provisioning_failure([_o("x", R.FAILED)])
    assert exc.value.report.failed_resource == "x"


def test_raise_on_failure_returns_report_when_ok():
    rep = R.raise_on_provisioning_failure([_o("a", R.PROVISIONED)])
    assert rep.ok is True


# --- Least-privilege IAM policies (Req 10.2) -----------------------------------


def test_component_policies_are_least_privilege():
    policies = R.build_component_policies()
    assert set(policies) == {
        "ingestor_role",
        "kb_role",
        "agent_role",
        "api_role",
        "whatsapp_role",
        "monitor_role",
        "acervo_role",
    }
    for key, policy in policies.items():
        result = validate_policy(policy)
        assert result.compliant, f"{key} not least-privilege: {result.reasons}"


def test_policy_documents_have_no_broad_wildcards():
    docs = R.component_policy_documents()
    for key, doc in docs.items():
        for stmt in doc["Statement"]:
            resources = stmt["Resource"]
            resources = [resources] if isinstance(resources, str) else resources
            assert "*" not in resources, f"{key} has broad Resource wildcard"
            actions = stmt["Action"]
            actions = [actions] if isinstance(actions, str) else actions
            for action in actions:
                assert action != "*", f"{key} has all-actions wildcard"
                assert not action.endswith(":*"), f"{key} has service-wide wildcard"


def test_arn_context_scopes_to_named_resources():
    ctx = R.ArnContext(account_id="123456789012", region="us-east-1",
                        session_table="my-sessions")
    docs = R.component_policy_documents(ctx)
    agent = docs["agent_role"]
    all_resources = []
    for stmt in agent["Statement"]:
        res = stmt["Resource"]
        all_resources.extend([res] if isinstance(res, str) else res)
    assert any("my-sessions" in r for r in all_resources)
    assert any("123456789012" in r for r in all_resources)


# --- Controle de custos ----------------------------------------------------------


def test_politica_de_custos_so_usa_curinga_nas_leituras_sem_recurso():
    ctx = R.ArnContext(account_id="123456789012", region="us-east-1")
    doc = R.custos_policy_document(
        ctx, ecr_repository_arn="arn:aws:ecr:us-east-1:123456789012:repository/progep-ifs-agentcore"
    )
    com_curinga = [s for s in doc["Statement"] if s["Resource"] == "*"]
    assert len(com_curinga) == 1
    assert sorted(com_curinga[0]["Action"]) == sorted(R.CUSTOS_WILDCARD_ACTION_EXCEPTIONS)
    # Escrita só na própria tabela.
    escrita = [s for s in doc["Statement"]
               if any(a in ("dynamodb:PutItem", "dynamodb:UpdateItem") for a in _acoes(s))]
    assert [s["Resource"] for s in escrita] == [
        "arn:aws:dynamodb:us-east-1:123456789012:table/progep-ifs-custos"]


def test_painel_le_a_tabela_de_custos_sem_escrever():
    doc = R.component_policy_documents(R.ArnContext(account_id="1", region="us-east-1"))
    leitura = next(s for s in doc["acervo_role"]["Statement"] if s.get("Sid") == "LerCustos")
    assert sorted(_acoes(leitura)) == ["dynamodb:GetItem", "dynamodb:Query"]


def _acoes(statement):
    acoes = statement["Action"]
    return [acoes] if isinstance(acoes, str) else acoes


def test_ciclo_de_vida_do_ecr_nunca_toca_imagem_com_tag():
    regras = R.ECR_LIFECYCLE_POLICY["rules"]
    assert [r["selection"]["tagStatus"] for r in regras] == ["untagged"]
    assert regras[0]["selection"]["countNumber"] >= 7
