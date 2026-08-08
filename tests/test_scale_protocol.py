import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.main as app_main_module
from app import db as app_db
from app.assessment_contract import AssessmentDefinitionBundle
from app.assessment_definitions import (
    build_synthetic_bundle,
    bundle_digest,
    definition_digest,
)
from app.db import get_session
from app.main import app
from app.scale_protocol import (
    evaluate_scale_protocol_manifest,
    scale_protocol_readiness,
    workflow_policy_digest,
)


DIGEST = "sha256:" + "a" * 64


def _complete_category(category: dict) -> None:
    for field in (
        "definition_id",
        "instrument_id",
        "instrument_name",
        "instrument_version",
        "language",
        "form",
        "license_source",
        "scoring_algorithm_id",
        "scoring_algorithm_version",
        "score_rounding_rule",
        "respondent_role",
        "assessor_role",
        "assessor_qualification",
        "pretest_time_window",
        "posttest_time_window",
        "followup_time_window",
    ):
        category[field] = f"frozen-{field}"
    for field in (
        "definition_digest",
        "item_set_digest",
        "administration_protocol_digest",
        "response_schema_digest",
        "result_schema_digest",
        "missingness_rule_digest",
        "stopping_rule_digest",
        "scoring_algorithm_digest",
    ):
        category[field] = DIGEST
    category["license_status"] = "authorized"
    for field in (
        "digital_presentation_permitted",
        "spoken_administration_permitted",
        "automatic_scoring_permitted",
        "item_response_storage_permitted",
        "result_storage_permitted",
        "result_export_permitted",
    ):
        category[field] = True
    category["score_min"] = 0
    category["score_max"] = 100
    category["score_direction"] = "higher_is_better"
    for field in (
        "pi_approval",
        "clinical_approval",
        "statistics_approval",
        "copyright_approval",
    ):
        category[field] = {
            "approved_by": f"named-{field}",
            "approved_at": "2026-07-19T12:00:00+08:00",
            "scope_digest": DIGEST,
        }


def _complete_definition_manifest(manifest: dict) -> None:
    manifest["definition_bundle_id"] = "frozen-two-outcome-bundle-v1"
    manifest["definition_bundle_digest"] = DIGEST
    for category in manifest["categories"]:
        _complete_category(category)


def _complete_workflow_policy(manifest: dict) -> None:
    policy = manifest["workflow_policy"]
    policy["workflow_policy_id"] = "frozen-formal-assessment-workflow"
    policy["workflow_policy_version"] = "v1"
    for field in (
        "pretest_schedule_rule_digest",
        "posttest_schedule_rule_digest",
        "followup_schedule_rule_digest",
        "deferral_authority_rule_digest",
        "reschedule_rule_digest",
        "closeout_rule_digest",
        "assessor_assignment_rule_digest",
    ):
        policy[field] = DIGEST
    policy["workflow_policy_digest"] = workflow_policy_digest(policy)
    for field in ("pi_approval", "clinical_approval", "statistics_approval"):
        policy[field] = {
            "approved_by": f"named-workflow-{field}",
            "approved_at": "2026-07-19T12:00:00+08:00",
            "scope_digest": policy["workflow_policy_digest"],
        }


def _approved_runtime_bundle() -> AssessmentDefinitionBundle:
    synthetic = build_synthetic_bundle()
    definitions = []
    for original in synthetic.snapshot.definitions:
        provisional = original.model_copy(update={
            "definition_digest": "sha256:" + "0" * 64,
            "result_export_permitted": True,
        })
        definitions.append(provisional.model_copy(update={
            "definition_digest": definition_digest(provisional),
        }))
    exact = (definitions[0], definitions[1])
    return AssessmentDefinitionBundle(
        bundle_id="approved-synthetic-runtime-contract-v1",
        bundle_digest=bundle_digest(
            "approved-synthetic-runtime-contract-v1",
            exact,
            formal_research_approved=True,
        ),
        definitions=exact,
        formal_research_approved=True,
    )


def _bind_manifest_to_bundle(
        manifest: dict, bundle: AssessmentDefinitionBundle) -> None:
    manifest["definition_bundle_id"] = bundle.bundle_id
    manifest["definition_bundle_digest"] = bundle.bundle_digest
    rows = {row["category_key"]: row for row in manifest["categories"]}
    artifact_fields = (
        "definition_id", "instrument_id", "instrument_version",
        "definition_digest", "item_set_digest",
        "administration_protocol_digest", "response_schema_digest",
        "result_schema_digest", "missingness_rule_digest",
        "stopping_rule_digest", "scoring_algorithm_id",
        "scoring_algorithm_version", "scoring_algorithm_digest",
        "score_min", "score_max", "score_direction", "score_rounding_rule",
        "automatic_scoring_permitted", "item_response_storage_permitted",
        "result_storage_permitted", "result_export_permitted",
    )
    for definition in bundle.definitions:
        category = rows[definition.category_key]
        _complete_category(category)
        for field in artifact_fields:
            category[field] = getattr(definition, field)
        for approval_field in (
            "pi_approval", "clinical_approval", "statistics_approval",
            "copyright_approval",
        ):
            category[approval_field]["scope_digest"] = definition.definition_digest


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(app_db, "engine", engine)
    SQLModel.metadata.create_all(engine)

    def override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override
    test_client = TestClient(app)
    yield test_client
    test_client.close()
    app.dependency_overrides.clear()


def test_scale_protocol_v4_is_explicitly_fail_closed_across_independent_gates():
    status = scale_protocol_readiness()

    assert status["schema_version"] == "scale-protocol-readiness.v4"
    assert status["status"] == "awaiting_pi_definition"
    assert status["definition_ready"] is False
    assert status["definition_artifact_enforcement_ready"] is True
    assert status["definition_artifacts_ready"] is False
    assert status["formal_result_contract_ready"] is True
    assert status["workflow_policy_ready"] is False
    assert status["workflow_contract_ready"] is True
    assert status["workflow_policy_enforcement_ready"] is False
    assert status["workflow_ready"] is False
    assert status["ready_for_research"] is False
    assert status["instance_creation_enabled"] is False
    assert status["automatic_scoring_enabled"] is False
    assert status["training_metrics_are_formal_scale_results"] is False
    assert [row["category_key"] for row in status["categories"]] == [
        "untrained_standardized_naming",
        "functional_communication",
    ]
    assert all(row["instrument_id"] is None for row in status["categories"])
    assert all(row["scoring_ready"] is False for row in status["categories"])
    required_empty_fields = {
        "definition_id", "instrument_id", "instrument_name", "instrument_version",
        "definition_digest", "language", "form", "license_source",
        "license_status", "digital_presentation_permitted",
        "spoken_administration_permitted", "automatic_scoring_permitted",
        "item_response_storage_permitted",
        "result_storage_permitted", "result_export_permitted",
        "item_set_digest", "administration_protocol_digest",
        "response_schema_digest", "result_schema_digest", "missingness_rule_digest",
        "stopping_rule_digest", "scoring_algorithm_id",
        "scoring_algorithm_version", "scoring_algorithm_digest",
        "score_min", "score_max", "score_direction", "score_rounding_rule",
        "respondent_role", "assessor_role", "assessor_qualification",
        "pretest_time_window", "posttest_time_window",
        "followup_time_window", "pi_approval", "clinical_approval",
        "statistics_approval", "copyright_approval",
    }
    for category in status["categories"]:
        assert required_empty_fields <= category.keys()
        assert all(category[field] is None for field in required_empty_fields)
    assert status["definition_bundle_id"] is None
    assert status["definition_bundle_digest"] is None
    assert all(value is None for value in status["workflow_policy"].values())


def test_scale_protocol_readiness_returns_a_defensive_copy():
    first = scale_protocol_readiness()
    first["categories"][0]["instrument_name"] = "invented"
    first["blocking_issues"].clear()

    second = scale_protocol_readiness()
    assert second["categories"][0]["instrument_name"] is None
    assert second["blocking_issues"]


def test_partial_manifest_remains_visible_but_blocked():
    partial = scale_protocol_readiness()
    partial["categories"][0].update({
        "instrument_id": "locally-frozen-id",
        "instrument_name": "locally-frozen-name",
        "instrument_version": "v1",
        "definition_digest": DIGEST,
        "scoring_ready": True,
    })

    evaluated = evaluate_scale_protocol_manifest(partial)

    first = evaluated["categories"][0]
    assert first["instrument_name"] == "locally-frozen-name"
    assert first["scoring_ready"] is False
    assert evaluated["ready_for_research"] is False
    assert any(issue["field"] == "license_source"
               for issue in evaluated["blocking_issues"])


def test_claimed_ready_flags_are_ignored_when_required_facts_are_missing():
    spoofed = scale_protocol_readiness()
    spoofed["status"] = "ready_for_research"
    spoofed["definition_ready"] = True
    spoofed["definition_artifact_enforcement_ready"] = True
    spoofed["definition_artifacts_ready"] = True
    spoofed["formal_result_contract_ready"] = True
    spoofed["workflow_policy_ready"] = True
    spoofed["workflow_contract_ready"] = True
    spoofed["workflow_policy_enforcement_ready"] = True
    spoofed["workflow_ready"] = True
    spoofed["ready_for_research"] = True
    spoofed["instance_creation_enabled"] = True
    spoofed["automatic_scoring_enabled"] = True
    spoofed["blocking_issues"] = []
    for category in spoofed["categories"]:
        category["scoring_ready"] = True

    evaluated = evaluate_scale_protocol_manifest(spoofed)

    assert evaluated["status"] == "awaiting_pi_definition"
    assert evaluated["definition_ready"] is False
    assert evaluated["definition_artifact_enforcement_ready"] is True
    assert evaluated["definition_artifacts_ready"] is False
    assert evaluated["formal_result_contract_ready"] is True
    assert evaluated["workflow_policy_ready"] is False
    assert evaluated["workflow_contract_ready"] is True
    assert evaluated["workflow_policy_enforcement_ready"] is False
    assert evaluated["workflow_ready"] is False
    assert evaluated["ready_for_research"] is False
    assert evaluated["automatic_scoring_enabled"] is False
    assert evaluated["instance_creation_enabled"] is False
    assert all(category["scoring_ready"] is False
               for category in evaluated["categories"])
    assert evaluated["blocking_issues"]


def test_every_missing_manifest_fact_has_a_category_and_field_blocker():
    status = scale_protocol_readiness()
    blocker_pairs = {
        (issue["category_key"], issue["field"])
        for issue in status["blocking_issues"]
    }

    for category in status["categories"]:
        key = category["category_key"]
        assert (key, "language") in blocker_pairs
        assert (key, "automatic_scoring_permitted") in blocker_pairs
        assert (key, "item_response_storage_permitted") in blocker_pairs
        assert (key, "result_schema_digest") in blocker_pairs
        assert (key, "missingness_rule_digest") in blocker_pairs
        assert (key, "scoring_algorithm_digest") in blocker_pairs
        assert (key, "assessor_qualification") in blocker_pairs
        assert (key, "followup_time_window") in blocker_pairs
        assert (key, "copyright_approval") in blocker_pairs
    assert ("platform", "formal_result_contract") not in blocker_pairs
    assert ("platform", "workflow_contract") not in blocker_pairs
    # S1+S3 已实现字节复核+逐题录音授权收据:该层默认就绪,不再出阻断。
    assert ("platform", "definition_artifact_enforcement") not in blocker_pairs
    assert ("platform", "workflow_policy_enforcement") in blocker_pairs
    assert ("platform", "definition_artifacts") in blocker_pairs
    assert ("workflow_policy", "deferral_authority_rule_digest") in blocker_pairs
    assert ("workflow_policy", "assessor_assignment_rule_digest") in blocker_pairs


def test_complete_definition_facts_do_not_claim_policy_artifacts_or_platform_layers():
    manifest = scale_protocol_readiness()
    _complete_definition_manifest(manifest)

    evaluated = evaluate_scale_protocol_manifest(manifest)

    assert evaluated["status"] == "awaiting_workflow_policy"
    assert evaluated["definition_ready"] is True
    assert evaluated["definition_artifact_enforcement_ready"] is True
    assert evaluated["definition_artifacts_ready"] is False
    assert evaluated["formal_result_contract_ready"] is True
    assert evaluated["workflow_policy_ready"] is False
    assert evaluated["workflow_contract_ready"] is True
    assert evaluated["workflow_policy_enforcement_ready"] is False
    assert evaluated["workflow_ready"] is False
    assert evaluated["ready_for_research"] is False
    assert evaluated["automatic_scoring_enabled"] is False
    assert evaluated["training_metrics_are_formal_scale_results"] is False
    assert all(category["scoring_ready"] is True
               for category in evaluated["categories"])
    blocker_codes = {issue["code"] for issue in evaluated["blocking_issues"]}
    assert "platform.definition_artifacts.not_ready" in blocker_codes
    assert "platform.formal_result_contract.not_ready" not in blocker_codes
    assert "platform.workflow_contract.not_ready" not in blocker_codes
    assert ("platform.definition_artifact_enforcement.not_ready"
            not in blocker_codes)
    assert "platform.workflow_policy_enforcement.not_ready" in blocker_codes
    assert "workflow_policy.closeout_rule_digest.not_ready" in blocker_codes


def test_invalid_digest_approval_or_score_range_keeps_manifest_blocked():
    manifest = scale_protocol_readiness()
    _complete_definition_manifest(manifest)
    first = manifest["categories"][0]
    first["item_set_digest"] = "not-a-sha256"
    first["pi_approval"]["approved_at"] = "2026-07-19"
    first["clinical_approval"]["scope_digest"] = "sha256:" + "b" * 64
    first["score_min"] = 100
    first["score_max"] = 0

    evaluated = evaluate_scale_protocol_manifest(manifest)
    first_blockers = {
        issue["field"] for issue in evaluated["blocking_issues"]
        if issue["category_key"] == first["category_key"]
    }

    assert {
        "item_set_digest", "pi_approval", "clinical_approval", "score_range",
    } <= first_blockers
    assert evaluated["ready_for_research"] is False


def test_claimed_digests_cannot_replace_runtime_artifact_and_policy_enforcement():
    bundle = _approved_runtime_bundle()
    manifest = scale_protocol_readiness()
    _bind_manifest_to_bundle(manifest, bundle)
    _complete_workflow_policy(manifest)

    # 参数化保留原语义:enforcement 层未实现时,声称摘要+完整 manifest 依然
    # 打不开任何一层——该守卫作为 evaluator 契约永久成立。
    evaluated = evaluate_scale_protocol_manifest(
        manifest,
        registered_definition_bundles=(bundle,),
        formal_result_contract_implemented=True,
        workflow_contract_implemented=True,
        definition_artifact_enforcement_implemented=False,
        workflow_policy_enforcement_implemented=False,
    )

    assert evaluated["status"] == "awaiting_platform_implementation"
    assert evaluated["definition_ready"] is True
    assert evaluated["definition_artifact_enforcement_ready"] is False
    assert evaluated["definition_artifacts_ready"] is False
    assert evaluated["workflow_policy_ready"] is True
    assert evaluated["workflow_contract_ready"] is True
    assert evaluated["workflow_policy_enforcement_ready"] is False
    assert evaluated["workflow_ready"] is False
    assert evaluated["ready_for_research"] is False
    assert {
        "platform.definition_artifact_enforcement.not_ready",
        "platform.workflow_policy_enforcement.not_ready",
        "platform.definition_artifacts.not_ready",
    } <= {issue["code"] for issue in evaluated["blocking_issues"]}

    # 当前默认(S1 字节复核+S3 授权收据已落地):制品层就绪,但工作流政策
    # 运行时(S4)未实现,ready_for_research 仍关。
    current_default = evaluate_scale_protocol_manifest(
        manifest,
        registered_definition_bundles=(bundle,),
        formal_result_contract_implemented=True,
        workflow_contract_implemented=True,
    )
    assert current_default["definition_artifact_enforcement_ready"] is True
    assert current_default["definition_artifacts_ready"] is True
    assert current_default["workflow_policy_enforcement_ready"] is False
    assert current_default["ready_for_research"] is False


def test_v4_only_opens_after_policy_exact_artifacts_and_code_layers_match():
    bundle = _approved_runtime_bundle()
    manifest = scale_protocol_readiness()
    _bind_manifest_to_bundle(manifest, bundle)
    _complete_workflow_policy(manifest)

    evaluated = evaluate_scale_protocol_manifest(
        manifest,
        registered_definition_bundles=(bundle,),
        formal_result_contract_implemented=True,
        workflow_contract_implemented=True,
        definition_artifact_enforcement_implemented=True,
        workflow_policy_enforcement_implemented=True,
    )

    assert evaluated["status"] == "ready_for_research"
    assert evaluated["definition_ready"] is True
    assert evaluated["definition_artifact_enforcement_ready"] is True
    assert evaluated["definition_artifacts_ready"] is True
    assert evaluated["formal_result_contract_ready"] is True
    assert evaluated["workflow_policy_ready"] is True
    assert evaluated["workflow_contract_ready"] is True
    assert evaluated["workflow_policy_enforcement_ready"] is True
    assert evaluated["workflow_ready"] is True
    assert evaluated["ready_for_research"] is True
    assert evaluated["instance_creation_enabled"] is True
    assert evaluated["automatic_scoring_enabled"] is True
    assert evaluated["blocking_issues"] == []

    drifted = bundle.model_copy(update={
        "bundle_digest": "sha256:" + "f" * 64,
    })
    refused = evaluate_scale_protocol_manifest(
        manifest,
        registered_definition_bundles=(drifted,),
        formal_result_contract_implemented=True,
        workflow_contract_implemented=True,
        definition_artifact_enforcement_implemented=True,
        workflow_policy_enforcement_implemented=True,
    )
    assert refused["status"] == "awaiting_definition_artifacts"
    assert refused["definition_ready"] is True
    assert refused["definition_artifacts_ready"] is False
    assert refused["instance_creation_enabled"] is False
    assert {issue["code"] for issue in refused["blocking_issues"]} == {
        "platform.definition_artifacts.not_ready",
    }


def test_workflow_digest_and_approval_scope_drift_remain_fail_closed():
    manifest = scale_protocol_readiness()
    _complete_definition_manifest(manifest)
    _complete_workflow_policy(manifest)
    manifest["workflow_policy"]["closeout_rule_digest"] = (
        "sha256:" + "b" * 64)

    evaluated = evaluate_scale_protocol_manifest(
        manifest,
        formal_result_contract_implemented=True,
        workflow_contract_implemented=True,
        definition_artifact_enforcement_implemented=True,
        workflow_policy_enforcement_implemented=True,
    )

    blockers = {
        issue["field"] for issue in evaluated["blocking_issues"]
        if issue["category_key"] == "workflow_policy"
    }
    assert "workflow_policy_digest" in blockers
    assert evaluated["workflow_policy_ready"] is False
    assert evaluated["workflow_ready"] is False
    assert evaluated["ready_for_research"] is False


def test_unfrozen_protocol_blocks_freeform_scale_results_by_default(
        client, monkeypatch):
    monkeypatch.delenv("NMU_ALLOW_LEGACY_UNVERIFIED_SCALE_ENTRY", raising=False)
    assert client.post("/patients", json={
        "patient_id": "P-SCALE-GATE",
        "is_simulation_subject": True,
    }).status_code == 200

    readiness = client.get("/content/scale-protocol")
    assert readiness.status_code == 200
    assert readiness.json()["ready_for_research"] is False
    blocked = client.post("/patients/P-SCALE-GATE/scales", json={
        "phase_type": "前测",
        "scale_name": "arbitrary-free-form",
        "score": 42,
    })
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "scale_protocol_not_frozen"


def test_definition_ready_manifest_without_workflow_still_blocks_legacy_scale(
        client, monkeypatch):
    monkeypatch.delenv("NMU_ALLOW_LEGACY_UNVERIFIED_SCALE_ENTRY", raising=False)
    ready_manifest = scale_protocol_readiness()
    _complete_definition_manifest(ready_manifest)
    ready_manifest = evaluate_scale_protocol_manifest(ready_manifest)
    assert ready_manifest["definition_ready"] is True
    assert ready_manifest["ready_for_research"] is False
    monkeypatch.setattr(
        app_main_module.scale_protocol,
        "scale_protocol_readiness",
        lambda: ready_manifest,
    )
    assert client.post("/patients", json={
        "patient_id": "P-SCALE-FORMAL-CONTRACT",
        "is_simulation_subject": True,
    }).status_code == 200

    blocked = client.post("/patients/P-SCALE-FORMAL-CONTRACT/scales", json={
        "phase_type": "前测",
        "scale_name": "arbitrary-free-form",
        "score": 42,
    })

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "formal_scale_workflow_unavailable"
    assert client.get("/patients/P-SCALE-FORMAL-CONTRACT/scales").json() == []


def test_scale_gate_reports_workflow_and_legacy_container_layers_precisely(
        client, monkeypatch):
    monkeypatch.delenv("NMU_ALLOW_LEGACY_UNVERIFIED_SCALE_ENTRY", raising=False)
    assert client.post("/patients", json={
        "patient_id": "P-SCALE-WORKFLOW",
        "is_simulation_subject": True,
    }).status_code == 200
    base = {
        "status": "awaiting_platform_implementation",
        "definition_ready": True,
        "formal_result_contract_ready": True,
        "workflow_ready": False,
        "ready_for_research": False,
    }
    monkeypatch.setattr(
        app_main_module.scale_protocol,
        "scale_protocol_readiness",
        lambda: dict(base),
    )
    payload = {
        "phase_type": "前测",
        "scale_name": "legacy-free-form",
        "score": 1,
    }
    blocked = client.post("/patients/P-SCALE-WORKFLOW/scales", json=payload)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "formal_scale_workflow_unavailable"

    monkeypatch.setattr(
        app_main_module.scale_protocol,
        "scale_protocol_readiness",
        lambda: {**base, "workflow_ready": True, "ready_for_research": True},
    )
    legacy = client.post("/patients/P-SCALE-WORKFLOW/scales", json=payload)
    assert legacy.status_code == 409
    assert legacy.json()["detail"]["code"] == "legacy_scale_container_forbidden"


def test_legacy_environment_switch_cannot_reopen_freeform_scale_entry(
        client, monkeypatch):
    monkeypatch.setenv("NMU_ALLOW_LEGACY_UNVERIFIED_SCALE_ENTRY", "1")
    assert client.post("/patients", json={
        "patient_id": "P-SCALE-LEGACY",
        "is_simulation_subject": True,
    }).status_code == 200

    created = client.post("/patients/P-SCALE-LEGACY/scales", json={
        "phase_type": "前测",
        "scale_name": "legacy-unverified-only",
        "score": 7,
    })

    assert created.status_code == 409
    assert created.json()["detail"]["code"] == "scale_protocol_not_frozen"
    assert client.get("/patients/P-SCALE-LEGACY/scales").json() == []
