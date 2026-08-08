"""Frozen assessment workflow-policy runtime (收据 150 S4).

政策文件=8 条规则的实际内容;每条规则的 digest 由本模块从解释的字节重算,
且必须与 PI manifest 声明的对应 digest 一致——政策内容与批准事实互相钉死。
解释器只认闭集规则种类(未知种类=fail-closed 拒绝),逐命令校验:
create(时点排期窗)/start(评估员分配)/approve_deferral(权限+上限)/close(收尾前提)。
改期(reschedule)在结构上不存在可变更排期的命令,政策以 kind=structurally_forbidden
声明该事实;出现其他种类即拒——防止政策声称一个运行时并不具备的宽松语义。
"""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .content import FrozenContentUnavailable
from .scale_protocol import workflow_policy_digest

ASSESSMENT_WORKFLOW_POLICY_FILE = "assessment_workflow_policy.json"


class WorkflowPolicyViolation(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ScheduleRule(_FrozenModel):
    kind: Literal["unrestricted", "date_range"]
    not_before: date | None = None
    not_after: date | None = None

    @field_validator("not_after")
    @classmethod
    def _range_coherent(cls, value, info):
        not_before = info.data.get("not_before")
        if value is not None and not_before is not None and value < not_before:
            raise ValueError("not_after must not precede not_before")
        return value

    # unrestricted 带窗口字段=作者笔误(想写 date_range),死字段永不执行,
    # 会让政策看起来有窗而实际无窗——装载即拒(审查 P3)。
    def model_post_init(self, _context) -> None:
        if self.kind == "unrestricted" and (
                self.not_before is not None or self.not_after is not None):
            raise ValueError(
                "unrestricted schedule rule must not carry window fields")
        if self.kind == "date_range" and (
                self.not_before is None and self.not_after is None):
            raise ValueError(
                "date_range schedule rule requires at least one bound")


class _DeferralRule(_FrozenModel):
    kind: Literal["admin_bounded"]
    max_deferral_days: int = Field(ge=1, le=365)


class _RescheduleRule(_FrozenModel):
    kind: Literal["structurally_forbidden"]


class _CloseoutRule(_FrozenModel):
    kind: Literal["both_instances_terminal"]


class _AssignmentRule(_FrozenModel):
    kind: Literal["assigned_assessor_only"]


class _PolicyFile(_FrozenModel):
    schema_version: Literal["assessment-workflow-policy.v1"]
    workflow_policy_id: str = Field(min_length=1, max_length=200)
    workflow_policy_version: str = Field(min_length=1, max_length=128)
    pretest_schedule_rule: _ScheduleRule
    posttest_schedule_rule: _ScheduleRule
    followup_schedule_rule: _ScheduleRule
    deferral_authority_rule: _DeferralRule
    reschedule_rule: _RescheduleRule
    closeout_rule: _CloseoutRule
    assessor_assignment_rule: _AssignmentRule


def _rule_digest(kind: str, payload: dict) -> str:
    from .assessment_definitions import digest_for

    return digest_for({
        "schema": f"assessment-workflow-rule.{kind}.v1", "payload": payload})


class FrozenWorkflowPolicy:
    """Compiled policy plus the digests recomputed from interpreted bytes."""

    def __init__(self, policy: _PolicyFile):
        self.policy = policy
        self.rule_digests = {
            "pretest_schedule_rule_digest": _rule_digest(
                "pretest_schedule", policy.pretest_schedule_rule.model_dump(
                    mode="json")),
            "posttest_schedule_rule_digest": _rule_digest(
                "posttest_schedule", policy.posttest_schedule_rule.model_dump(
                    mode="json")),
            "followup_schedule_rule_digest": _rule_digest(
                "followup_schedule", policy.followup_schedule_rule.model_dump(
                    mode="json")),
            "deferral_authority_rule_digest": _rule_digest(
                "deferral_authority",
                policy.deferral_authority_rule.model_dump(mode="json")),
            "reschedule_rule_digest": _rule_digest(
                "reschedule", policy.reschedule_rule.model_dump(mode="json")),
            "closeout_rule_digest": _rule_digest(
                "closeout", policy.closeout_rule.model_dump(mode="json")),
            "assessor_assignment_rule_digest": _rule_digest(
                "assessor_assignment",
                policy.assessor_assignment_rule.model_dump(mode="json")),
        }
        identity = {
            "workflow_policy_id": policy.workflow_policy_id,
            "workflow_policy_version": policy.workflow_policy_version,
            **self.rule_digests,
        }
        self.workflow_policy_digest = workflow_policy_digest(identity)

    def assert_manifest_binding(self, manifest_policy: dict) -> None:
        """政策文件与 PI manifest 的 8 个 digest + 标识逐项一致,否则拒绝执行。"""
        expected = {
            "workflow_policy_id": self.policy.workflow_policy_id,
            "workflow_policy_version": self.policy.workflow_policy_version,
            "workflow_policy_digest": self.workflow_policy_digest,
            **self.rule_digests,
        }
        for field, value in expected.items():
            if manifest_policy.get(field) != value:
                raise WorkflowPolicyViolation(
                    "assessment_workflow_policy_binding_invalid",
                    f"冻结工作流政策与 manifest 声明不一致:{field}",
                )

    def enforce_create(self, *, timepoint: str, scheduled_date: date) -> None:
        rule = {
            "pretest": self.policy.pretest_schedule_rule,
            "posttest": self.policy.posttest_schedule_rule,
            "followup": self.policy.followup_schedule_rule,
        }.get(timepoint)
        if rule is None:
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_timepoint_unknown",
                "冻结政策没有该时点的排期规则")
        if rule.kind == "unrestricted":
            return
        if rule.not_before is not None and scheduled_date < rule.not_before:
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_schedule_violation",
                "排期早于冻结政策允许的窗口")
        if rule.not_after is not None and scheduled_date > rule.not_after:
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_schedule_violation",
                "排期晚于冻结政策允许的窗口")

    def enforce_actor_binding(
            self, *, actor_id: str, assigned_assessor_id: str) -> None:
        """assigned_assessor_only:执行命令必须出自被分配评估员本人。

        服务状态机允许管理员代办;冻结政策声明的语义更严,政策在场时以政策为准
        (声明=执行,审查 P2)。延期批准不在此列——那由 deferral_authority 规则管。
        """
        if (self.policy.assessor_assignment_rule.kind == "assigned_assessor_only"
                and actor_id != assigned_assessor_id):
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_assessor_mismatch",
                "冻结政策要求由被分配评估员本人执行本命令")

    def enforce_deferral(self, *, actor_role: str, today: date,
                         deferred_until: date) -> None:
        rule = self.policy.deferral_authority_rule
        if actor_role != "admin":
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_deferral_forbidden",
                "冻结政策要求管理员批准延期")
        if deferred_until > today + timedelta(days=rule.max_deferral_days):
            raise WorkflowPolicyViolation(
                "assessment_workflow_policy_deferral_too_long",
                f"延期超出冻结政策上限 {rule.max_deferral_days} 天")


def load_workflow_policy(content_dir: str | Path) -> FrozenWorkflowPolicy | None:
    """Load+compile the frozen policy; absent file = None (readiness stays closed)."""
    path = Path(content_dir) / ASSESSMENT_WORKFLOW_POLICY_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FrozenWorkflowPolicy(_PolicyFile.model_validate(data))
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise FrozenContentUnavailable(f"冻结工作流政策不可用:{exc}") from exc
