"""冻结工作流政策运行时(收据 150 S4):闭集解释器+manifest 绑定+逐命令校验。"""
from datetime import date
import json

import pytest

from app import assessment_bundles, assessment_workflow_policy, content
from tests.test_assessment_api import assessment_clients  # noqa: F401
from tests.test_assessment_bundles import _package


def _policy_data(**overrides) -> dict:
    data = {
        "schema_version": "assessment-workflow-policy.v1",
        "workflow_policy_id": "formal-workflow-v1",
        "workflow_policy_version": "v1",
        "pretest_schedule_rule": {
            "kind": "date_range",
            "not_before": "2026-09-01",
            "not_after": "2026-09-14",
        },
        "posttest_schedule_rule": {"kind": "unrestricted"},
        "followup_schedule_rule": {"kind": "unrestricted"},
        "deferral_authority_rule": {
            "kind": "admin_bounded", "max_deferral_days": 14},
        "reschedule_rule": {"kind": "structurally_forbidden"},
        "closeout_rule": {"kind": "both_instances_terminal"},
        "assessor_assignment_rule": {"kind": "assigned_assessor_only"},
    }
    data.update(overrides)
    return data


def _write_policy(tmp_path, data: dict) -> None:
    (tmp_path / assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE
     ).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_absent_file_is_none_and_defects_fail_closed(tmp_path):
    assert assessment_workflow_policy.load_workflow_policy(tmp_path) is None

    _write_policy(tmp_path, _policy_data(
        reschedule_rule={"kind": "allowed_any_time"}))
    with pytest.raises(content.FrozenContentUnavailable):
        assessment_workflow_policy.load_workflow_policy(tmp_path)

    (tmp_path / assessment_workflow_policy.ASSESSMENT_WORKFLOW_POLICY_FILE
     ).write_text("not-json", encoding="utf-8")
    with pytest.raises(content.FrozenContentUnavailable):
        assessment_workflow_policy.load_workflow_policy(tmp_path)


def test_digests_recompute_and_manifest_binding_is_exact(tmp_path):
    _write_policy(tmp_path, _policy_data())
    policy = assessment_workflow_policy.load_workflow_policy(tmp_path)
    assert policy is not None
    manifest_policy = {
        "workflow_policy_id": "formal-workflow-v1",
        "workflow_policy_version": "v1",
        "workflow_policy_digest": policy.workflow_policy_digest,
        **policy.rule_digests,
    }
    policy.assert_manifest_binding(manifest_policy)

    tampered = dict(manifest_policy)
    tampered["closeout_rule_digest"] = "sha256:" + "f" * 64
    with pytest.raises(assessment_workflow_policy.WorkflowPolicyViolation,
                       match="closeout_rule_digest"):
        policy.assert_manifest_binding(tampered)

    # 规则内容变化 → digest 变化(内容寻址,声称无效)。
    _write_policy(tmp_path, _policy_data(deferral_authority_rule={
        "kind": "admin_bounded", "max_deferral_days": 30}))
    changed = assessment_workflow_policy.load_workflow_policy(tmp_path)
    assert (changed.rule_digests["deferral_authority_rule_digest"]
            != policy.rule_digests["deferral_authority_rule_digest"])


def test_per_command_enforcement(tmp_path):
    _write_policy(tmp_path, _policy_data())
    policy = assessment_workflow_policy.load_workflow_policy(tmp_path)

    policy.enforce_create(
        timepoint="pretest", scheduled_date=date(2026, 9, 7))
    for bad_day in (date(2026, 8, 31), date(2026, 9, 15)):
        with pytest.raises(assessment_workflow_policy.WorkflowPolicyViolation,
                           match="窗口"):
            policy.enforce_create(timepoint="pretest", scheduled_date=bad_day)
    policy.enforce_create(
        timepoint="posttest", scheduled_date=date(2030, 1, 1))
    with pytest.raises(assessment_workflow_policy.WorkflowPolicyViolation,
                       match="时点"):
        policy.enforce_create(
            timepoint="baseline", scheduled_date=date(2026, 9, 7))

    policy.enforce_deferral(
        actor_role="admin", today=date(2026, 9, 1),
        deferred_until=date(2026, 9, 10))
    with pytest.raises(assessment_workflow_policy.WorkflowPolicyViolation,
                       match="管理员"):
        policy.enforce_deferral(
            actor_role="researcher", today=date(2026, 9, 1),
            deferred_until=date(2026, 9, 10))
    with pytest.raises(assessment_workflow_policy.WorkflowPolicyViolation,
                       match="上限"):
        policy.enforce_deferral(
            actor_role="admin", today=date(2026, 9, 1),
            deferred_until=date(2026, 9, 30))


def test_training_isolation_refuses_overlapping_naming_words():
    """量表一词表撞上真实训练词(周2 题库含胡萝卜)即拒;不相交词表放行。"""
    clean = _package("iso-clean-v1")
    assessment_bundles.assert_training_isolation(
        (clean,), content.CONTENT_DIR)

    dirty = _package("iso-dirty-v1")
    for definition in dirty["definitions"]:
        if definition["category_key"] == "untrained_standardized_naming":
            definition["items"][0]["word"] = "胡萝卜"
    with pytest.raises(content.FrozenContentUnavailable, match="未训练词"):
        assessment_bundles.assert_training_isolation(
            (dirty,), content.CONTENT_DIR)


def test_http_create_enforces_the_frozen_schedule_window(  # noqa: F811
        tmp_path, monkeypatch, assessment_clients):
    """HTTP 面:政策文件存在时 create 逐命令过冻结排期窗(409 带政策 code)。"""
    from datetime import timedelta

    staged = tmp_path / "content-policy"
    staged.mkdir()
    _write_policy(staged, _policy_data(pretest_schedule_rule={
        "kind": "date_range",
        "not_before": (date.today() + timedelta(days=30)).isoformat(),
        "not_after": (date.today() + timedelta(days=44)).isoformat(),
    }))
    monkeypatch.setattr(content, "CONTENT_DIR", staged)

    denied = assessment_clients.researcher.post(
        "/patients/SIM-ASSESSMENT-API/assessment-events",
        json={
            "timepoint": "pretest",
            "scheduled_date": date.today().isoformat(),
            "idempotency_key": "policy-window-create-0001",
        },
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == (
        "assessment_workflow_policy_schedule_violation")

    allowed = assessment_clients.researcher.post(
        "/patients/SIM-ASSESSMENT-API/assessment-events",
        json={
            "timepoint": "pretest",
            "scheduled_date": (
                date.today() + timedelta(days=35)).isoformat(),
            "idempotency_key": "policy-window-create-0002",
        },
    )
    assert allowed.status_code == 200, allowed.text
