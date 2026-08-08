"""量表协议 manifest 装载器(收据 150 S2):PI 冻结事实的文件载体。"""
import json

import pytest

from app import content, scale_protocol


def _write_manifest(tmp_path, data: dict) -> None:
    (tmp_path / scale_protocol.SCALE_PROTOCOL_MANIFEST_FILE).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _category(key: str, **facts) -> dict:
    return {"category_key": key, **facts}


def test_absent_manifest_keeps_the_all_none_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    readiness = scale_protocol.scale_protocol_readiness()
    assert readiness["status"] == "awaiting_pi_definition"
    assert readiness["ready_for_research"] is False
    codes = {issue["code"] for issue in readiness["blocking_issues"]}
    assert "platform.definition_artifact_enforcement.not_ready" not in codes
    assert "platform.workflow_policy_enforcement.not_ready" not in codes
    assert "platform.definition_artifacts.not_ready" in codes


def test_supplied_facts_surface_but_never_claim_readiness(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    _write_manifest(tmp_path, {
        "categories": [
            _category(
                "untrained_standardized_naming",
                instrument_name="某标准化命名测验",
                language="zh-CN",
                license_status="authorized",
                # 文件声称 readiness/未知字段:必须被覆盖/忽略。
                scoring_ready=True,
                injected_unknown_field="x",
            ),
            _category("functional_communication"),
        ],
        "ready_for_research": True,
        "status": "ready_for_research",
    })
    readiness = scale_protocol.scale_protocol_readiness()
    named = next(row for row in readiness["categories"]
                 if row["category_key"] == "untrained_standardized_naming")
    assert named["instrument_name"] == "某标准化命名测验"
    assert named["license_status"] == "authorized"
    assert "injected_unknown_field" not in named
    assert named["scoring_ready"] is False
    assert readiness["ready_for_research"] is False
    assert readiness["status"] == "awaiting_pi_definition"


def test_malformed_manifest_fails_closed_not_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    _write_manifest(tmp_path, {"categories": [
        _category("untrained_standardized_naming")]})
    with pytest.raises(content.FrozenContentUnavailable, match="manifest"):
        scale_protocol.scale_protocol_readiness()

    (tmp_path / scale_protocol.SCALE_PROTOCOL_MANIFEST_FILE).write_text(
        "not-json", encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        scale_protocol.scale_protocol_readiness()


def test_workflow_policy_facts_merge_into_the_policy_block(tmp_path, monkeypatch):
    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    _write_manifest(tmp_path, {
        "categories": [
            _category("untrained_standardized_naming"),
            _category("functional_communication"),
        ],
        "workflow_policy": {
            "workflow_policy_id": "formal-workflow-v1",
            "unknown_policy_field": "ignored",
        },
    })
    readiness = scale_protocol.scale_protocol_readiness()
    assert readiness["workflow_policy"]["workflow_policy_id"] == (
        "formal-workflow-v1")
    assert "unknown_policy_field" not in readiness["workflow_policy"]
    assert readiness["workflow_policy_ready"] is False


def test_ready_requires_the_executable_policy_file_bound(tmp_path, monkeypatch):
    """审查 P2:manifest 政策事实完备而可执行政策文件缺失/不一致时,就绪面
    绝不显示 ready——面板放行与命令 409 不允许自相矛盾。"""
    import json as _json

    from app import assessment_workflow_policy
    from tests.test_assessment_workflow_policy import _policy_data

    monkeypatch.setattr(content, "CONTENT_DIR", tmp_path)
    policy_path = (
        tmp_path / assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE)
    policy_path.write_text(
        _json.dumps(_policy_data(), ensure_ascii=False), encoding="utf-8")
    policy = assessment_workflow_policy.load_workflow_policy(tmp_path)
    _write_manifest(tmp_path, {
        "categories": [
            _category("untrained_standardized_naming"),
            _category("functional_communication"),
        ],
        "workflow_policy": {
            "workflow_policy_id": policy.policy.workflow_policy_id,
            "workflow_policy_version": policy.policy.workflow_policy_version,
            "workflow_policy_digest": policy.workflow_policy_digest,
            **policy.rule_digests,
        },
    })
    bound = scale_protocol.scale_protocol_readiness()
    codes = {issue["code"] for issue in bound["blocking_issues"]}
    assert "workflow_policy.executable_file.not_ready" not in codes

    # 文件被改(digest 漂移)→ 就绪面出阻断且政策面强制不 ready。
    drifted = _json.loads(policy_path.read_text(encoding="utf-8"))
    drifted["deferral_authority_rule"]["max_deferral_days"] = 30
    policy_path.write_text(
        _json.dumps(drifted, ensure_ascii=False), encoding="utf-8")
    mismatch = scale_protocol.scale_protocol_readiness()
    codes = {issue["code"] for issue in mismatch["blocking_issues"]}
    assert "workflow_policy.executable_file.not_ready" in codes
    assert mismatch["workflow_policy_ready"] is False
    assert mismatch["ready_for_research"] is False

    # 文件整个缺失同拒。
    policy_path.unlink()
    missing = scale_protocol.scale_protocol_readiness()
    codes = {issue["code"] for issue in missing["blocking_issues"]}
    assert "workflow_policy.executable_file.not_ready" in codes
