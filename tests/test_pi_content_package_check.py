"""PI 三件套检查器：它必须用真实装载器判定，且在缺席时给出可执行清单。"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import json

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location(
        "check_pi_content_package", ROOT / "scripts" / "check_pi_content_package.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_absent_pieces_are_reported_as_not_delivered_rather_than_as_errors(tmp_path):
    module = _module()
    report = module.inspect(tmp_path)
    assert [entry["present"] for entry in report["files"]] == [False, False, False]
    # 缺席是"尚未交付"，不是部署缺陷——这条口径写在 scale_protocol 的装载器里
    assert any("不存在" in str(entry["detail"]) for entry in report["loaders"])
    assert report["ready_for_research"] is False


def test_the_report_names_the_exact_fields_pi_still_has_to_freeze(tmp_path):
    module = _module()
    text = module.render(module.inspect(tmp_path))
    # 两张表各自的字段清单必须点名，而不是只说"还没就绪"
    for field in ("instrument_version", "license_status", "score_direction",
                  "pretest_time_window", "copyright_approval"):
        assert field in text, field
    for category in ("untrained_standardized_naming", "functional_communication",
                     "workflow_policy"):
        assert category in text, category
    # 也要说清工程侧不代填
    assert "不代填" in text


def test_exit_code_says_not_ready_without_pretending_to_be_broken(tmp_path):
    module = _module()
    assert module.main(["--content-dir", str(tmp_path)]) == 1


@pytest.mark.parametrize("relative", ["scale_protocol_manifest.json", "assessment_definitions/bundle_index.json",
                                     "assessment_workflow_policy.json"])
def test_each_malformed_piece_is_actually_loaded_from_candidate(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{malformed")
    module = _module()
    report = module.inspect(tmp_path)
    assert any(row["piece"] == relative and row["broken"] for row in report["loaders"])
    assert module.main(["--content-dir", str(tmp_path)]) == 2


def _complete_candidate(tmp_path):
    from copy import deepcopy
    from app import assessment_bundles, assessment_workflow_policy, scale_protocol
    from tests.test_assessment_bundles import _package, _stage
    from tests.test_assessment_workflow_policy import _policy_data, _write_policy
    from tests.test_scale_protocol import _bind_manifest_to_bundle, _complete_workflow_policy
    package = _package(approved=True)
    for definition in package["definitions"]:
        definition["permissions"]["result_export_permitted"] = True
    _stage(tmp_path, {"candidate.json": package}, active=package["bundle_id"])
    bundle = assessment_bundles.compile_bundle_package(package)
    manifest = deepcopy(scale_protocol._MANIFEST)
    _bind_manifest_to_bundle(manifest, bundle.snapshot)
    _complete_workflow_policy(manifest)
    _write_policy(tmp_path, _policy_data())
    policy = assessment_workflow_policy.load_workflow_policy(tmp_path)
    manifest["workflow_policy"].update({
        "workflow_policy_id": policy.policy.workflow_policy_id,
        "workflow_policy_version": policy.policy.workflow_policy_version,
        "workflow_policy_digest": policy.workflow_policy_digest,
        **policy.rule_digests,
    })
    for key in ("pi_approval", "clinical_approval", "statistics_approval"):
        manifest["workflow_policy"][key]["scope_digest"] = policy.workflow_policy_digest
    (tmp_path / "scale_protocol_manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_candidate_is_ready_without_using_or_installing_callers_registry(tmp_path, monkeypatch):
    from app import assessment_definitions, content
    _complete_candidate(tmp_path)
    before = assessment_definitions.registered_bundles()
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path / "wrong-global-directory")
    report = _module().inspect(tmp_path)
    assert report["ready_for_research"] is True, report["blocking_issues"]
    assert all(row["loaded"] for row in report["loaders"])
    assert assessment_definitions.registered_bundles() == before
    assert _module().main(["--content-dir", str(tmp_path)]) == 0


def test_empty_candidate_cannot_borrow_a_ready_default_directory(tmp_path, monkeypatch):
    from app import content
    complete = tmp_path / "complete"
    complete.mkdir()
    _complete_candidate(complete)
    monkeypatch.setattr(content, "CONTENT_DIR", complete)
    report = _module().inspect(tmp_path / "empty")
    assert report["ready_for_research"] is False
    assert not any(row["present"] for row in report["files"])


def test_candidate_rejects_training_word_overlap_even_when_package_hash_matches(tmp_path):
    import hashlib
    _complete_candidate(tmp_path)
    path = tmp_path / "assessment_definitions/candidate.json"
    package = json.loads(path.read_text())
    for definition in package["definitions"]:
        if definition["category_key"] == "untrained_standardized_naming":
            definition["items"][0]["word"] = "胡萝卜"
    path.write_text(json.dumps(package))
    index_path = tmp_path / "assessment_definitions/bundle_index.json"
    index = json.loads(index_path.read_text())
    index["bundles"][0]["content_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    index_path.write_text(json.dumps(index))
    report = _module().inspect(tmp_path)
    assert not report["ready_for_research"]
    assert any(row["broken"] for row in report["loaders"])


def test_candidate_manifest_policy_digest_mismatch_stays_blocked(tmp_path):
    _complete_candidate(tmp_path)
    path = tmp_path / "assessment_workflow_policy.json"
    policy = json.loads(path.read_text())
    policy["deferral_authority_rule"]["max_deferral_days"] += 1
    path.write_text(json.dumps(policy))
    report = _module().inspect(tmp_path)
    assert not report["ready_for_research"]
    assert not report["gates"]["workflow_policy_ready"]


def test_malformed_policy_cannot_reuse_a_complete_candidate_readiness(tmp_path):
    _complete_candidate(tmp_path)
    (tmp_path / "assessment_workflow_policy.json").write_text("{malformed")
    module = _module()
    report = module.inspect(tmp_path)
    assert not report["ready_for_research"]
    assert any(row["piece"] == "assessment_workflow_policy.json" and row["broken"]
               for row in report["loaders"])
    assert module.main(["--content-dir", str(tmp_path)]) == 2
