"""Fail-closed manifest for the two formal outcome categories.

The source material freezes *categories*, not instrument names, item sets, or
scoring rules.  A category becomes scoring-ready only when every operational,
licensing, governance, and statistical fact below has been frozen.  Readiness
is always derived here; a stored or supplied ``scoring_ready`` flag is never
trusted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Callable, Final, Iterable

from .assessment_contract import AssessmentDefinitionBundle


SCALE_PROTOCOL_SCHEMA_VERSION: Final = "scale-protocol-readiness.v4"

# Code-release facts.  A manifest or HTTP client can never promote these.
FORMAL_RESULT_CONTRACT_IMPLEMENTED: Final = True
WORKFLOW_CONTRACT_IMPLEMENTED: Final = True
# The generic state machine and evidence tables are implemented, but a future
# licensed runtime must additionally prove that its item/protocol/scorer bytes
# were content-addressed and that the frozen scheduling/assignment/deferral/
# closeout policy is actually executed.  Presence of claimed digests in a
# manifest is not that proof.  These stay false until those server-side
# adapters exist and have independent release evidence.
DEFINITION_ARTIFACT_ENFORCEMENT_IMPLEMENTED: Final = False
WORKFLOW_POLICY_ENFORCEMENT_IMPLEMENTED: Final = False

_FORMAL_RESULT_CONTRACT_BLOCKER: Final = {
    "code": "platform.formal_result_contract.not_ready",
    "category_key": "platform",
    "field": "formal_result_contract",
    "message": (
        "平台：正式量表 AssessmentInstance、ItemResponse 与 ScoringEvidence "
        "结果及计分证据合同尚未实现。"
    ),
}
_WORKFLOW_BLOCKER: Final = {
    "code": "platform.workflow_contract.not_ready",
    "category_key": "platform",
    "field": "workflow_contract",
    "message": (
        "平台：独立评估事件的 due / in-progress / completed-or-deferred → "
        "closeout → switch 工作流尚未完成代码与回归验证。"
    ),
}
_DEFINITION_ARTIFACT_BLOCKER: Final = {
    "code": "platform.definition_artifacts.not_ready",
    "category_key": "platform",
    "field": "definition_artifacts",
    "message": (
        "平台：尚未安装与 PI 冻结事实逐字段一致、经校验且获正式研究批准的两表运行时定义包。"
    ),
}
_DEFINITION_ARTIFACT_ENFORCEMENT_BLOCKER: Final = {
    "code": "platform.definition_artifact_enforcement.not_ready",
    "category_key": "platform",
    "field": "definition_artifact_enforcement",
    "message": (
        "平台：正式两表运行时包尚未实现对条目、施测协议、响应结构与计分器"
        "实际制品的服务器侧内容寻址复核；逐题录音仍缺少绑定"
        "patient / event / instance / item / revision 的不可复用服务端收据与源文件校验。"
    ),
}
_WORKFLOW_POLICY_ENFORCEMENT_BLOCKER: Final = {
    "code": "platform.workflow_policy_enforcement.not_ready",
    "category_key": "platform",
    "field": "workflow_policy_enforcement",
    "message": (
        "平台：冻结的排期、评估员分配、改期、延期批准与收尾政策尚未安装为"
        "可执行且逐命令校验的服务器运行时。"
    ),
}

_SHA256_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_LICENSE_STATUSES: Final = {"authorized"}
_SCORE_DIRECTIONS: Final = {"higher_is_better", "lower_is_better"}

_TEXT_FIELDS: Final[tuple[str, ...]] = (
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
)
_DIGEST_FIELDS: Final[tuple[str, ...]] = (
    "definition_digest",
    "item_set_digest",
    "administration_protocol_digest",
    "response_schema_digest",
    "result_schema_digest",
    "missingness_rule_digest",
    "stopping_rule_digest",
    "scoring_algorithm_digest",
)
_PERMISSION_FIELDS: Final[tuple[str, ...]] = (
    "digital_presentation_permitted",
    "spoken_administration_permitted",
    "automatic_scoring_permitted",
    "item_response_storage_permitted",
    "result_storage_permitted",
    "result_export_permitted",
)
_APPROVAL_FIELDS: Final[tuple[str, ...]] = (
    "pi_approval",
    "clinical_approval",
    "statistics_approval",
    "copyright_approval",
)

_FIELD_LABELS: Final[dict[str, str]] = {
    "definition_id": "定义标识",
    "instrument_id": "量表标识",
    "instrument_name": "量表名称",
    "instrument_version": "量表版本",
    "definition_digest": "完整定义摘要",
    "language": "语言版本",
    "form": "量表表型",
    "license_source": "授权来源",
    "license_status": "授权状态",
    "digital_presentation_permitted": "数字化呈现许可",
    "spoken_administration_permitted": "语音播报许可",
    "automatic_scoring_permitted": "自动计分许可",
    "item_response_storage_permitted": "条目作答存储许可",
    "result_storage_permitted": "结果存储许可",
    "result_export_permitted": "结果导出许可",
    "item_set_digest": "条目集摘要",
    "administration_protocol_digest": "施测协议摘要",
    "response_schema_digest": "作答结构摘要",
    "result_schema_digest": "结果结构摘要",
    "missingness_rule_digest": "缺失值规则摘要",
    "stopping_rule_digest": "停止规则摘要",
    "scoring_algorithm_id": "计分算法标识",
    "scoring_algorithm_version": "计分算法版本",
    "scoring_algorithm_digest": "计分算法摘要",
    "score_min": "最低分",
    "score_max": "最高分",
    "score_range": "分数范围",
    "score_direction": "分数方向",
    "score_rounding_rule": "舍入规则",
    "respondent_role": "作答者角色",
    "assessor_role": "施测者角色",
    "assessor_qualification": "施测者资质",
    "pretest_time_window": "前测时间窗",
    "posttest_time_window": "后测时间窗",
    "followup_time_window": "随访时间窗",
    "pi_approval": "PI 批准事实",
    "clinical_approval": "临床批准事实",
    "statistics_approval": "统计批准事实",
    "copyright_approval": "版权批准事实",
}

_WORKFLOW_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "workflow_policy_id",
    "workflow_policy_version",
)
_WORKFLOW_DIGEST_FIELDS: Final[tuple[str, ...]] = (
    "workflow_policy_digest",
    "pretest_schedule_rule_digest",
    "posttest_schedule_rule_digest",
    "followup_schedule_rule_digest",
    "deferral_authority_rule_digest",
    "reschedule_rule_digest",
    "closeout_rule_digest",
    "assessor_assignment_rule_digest",
)
_WORKFLOW_APPROVAL_FIELDS: Final[tuple[str, ...]] = (
    "pi_approval",
    "clinical_approval",
    "statistics_approval",
)
_WORKFLOW_FIELD_LABELS: Final[dict[str, str]] = {
    "workflow_policy_id": "工作流政策标识",
    "workflow_policy_version": "工作流政策版本",
    "workflow_policy_digest": "工作流政策摘要",
    "pretest_schedule_rule_digest": "前测排期规则摘要",
    "posttest_schedule_rule_digest": "后测排期规则摘要",
    "followup_schedule_rule_digest": "随访排期规则摘要",
    "deferral_authority_rule_digest": "延期批准权限规则摘要",
    "reschedule_rule_digest": "改期规则摘要",
    "closeout_rule_digest": "结项规则摘要",
    "assessor_assignment_rule_digest": "评估员分配规则摘要",
    "pi_approval": "PI 工作流批准事实",
    "clinical_approval": "临床工作流批准事实",
    "statistics_approval": "统计工作流批准事实",
}


def _empty_category(category_key: str, label: str) -> dict:
    """Build one deliberately incomplete category without candidate content."""
    row = {
        "category_key": category_key,
        "label": label,
        "required": True,
        "scoring_ready": False,
        "license_status": None,
        "digital_presentation_permitted": None,
        "spoken_administration_permitted": None,
        "automatic_scoring_permitted": None,
        "item_response_storage_permitted": None,
        "result_storage_permitted": None,
        "result_export_permitted": None,
        "score_min": None,
        "score_max": None,
        "score_direction": None,
    }
    for field in (*_TEXT_FIELDS, *_DIGEST_FIELDS, *_APPROVAL_FIELDS):
        row[field] = None
    return row


def _empty_workflow_policy() -> dict:
    policy: dict[str, object | None] = {}
    for field in (
        *_WORKFLOW_TEXT_FIELDS,
        *_WORKFLOW_DIGEST_FIELDS,
        *_WORKFLOW_APPROVAL_FIELDS,
    ):
        policy[field] = None
    return policy


_MANIFEST: Final[dict] = {
    "schema_version": SCALE_PROTOCOL_SCHEMA_VERSION,
    "definition_bundle_id": None,
    "definition_bundle_digest": None,
    "categories": [
        _empty_category(
            "untrained_standardized_naming", "未训练词标准化命名测验"),
        _empty_category("functional_communication", "功能沟通量表"),
    ],
    # Scheduling, deferral authority, reassignment and closeout are protocol
    # facts.  They must be supplied and approved by the study owners; the
    # application must not invent them from UI defaults.
    "workflow_policy": _empty_workflow_policy(),
}


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _finite_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _approval_fact_is_valid(value: object) -> bool:
    """An auditable approval is a fact, not a free-standing true/false flag."""
    if not isinstance(value, dict):
        return False
    if not _has_text(value.get("approved_by")):
        return False
    if not _has_sha256(value.get("scope_digest")):
        return False
    approved_at = value.get("approved_at")
    if not isinstance(approved_at, str):
        return False
    try:
        parsed = datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _digest_for(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def workflow_policy_digest(policy: dict) -> str:
    """Content address the operational rules, excluding self/approvals."""
    return _digest_for({
        "schema": "formal-assessment-workflow-policy.v1",
        **{field: policy.get(field) for field in _WORKFLOW_TEXT_FIELDS},
        **{
            field: policy.get(field)
            for field in _WORKFLOW_DIGEST_FIELDS
            if field != "workflow_policy_digest"
        },
    })


def _workflow_policy_blocking_fields(policy: dict) -> list[str]:
    blocked: list[str] = []
    for field in _WORKFLOW_TEXT_FIELDS:
        if not _has_text(policy.get(field)):
            blocked.append(field)
    for field in _WORKFLOW_DIGEST_FIELDS:
        if not _has_sha256(policy.get(field)):
            blocked.append(field)
    for field in _WORKFLOW_APPROVAL_FIELDS:
        if not _approval_fact_is_valid(policy.get(field)):
            blocked.append(field)

    claimed_digest = policy.get("workflow_policy_digest")
    prerequisite_fields = (
        *_WORKFLOW_TEXT_FIELDS,
        *(
            field for field in _WORKFLOW_DIGEST_FIELDS
            if field != "workflow_policy_digest"
        ),
    )
    if (
        all(field not in blocked for field in prerequisite_fields)
        and claimed_digest != workflow_policy_digest(policy)
        and "workflow_policy_digest" not in blocked
    ):
        blocked.append("workflow_policy_digest")
    for field in _WORKFLOW_APPROVAL_FIELDS:
        fact = policy.get(field)
        if (
            _approval_fact_is_valid(fact)
            and fact["scope_digest"] != claimed_digest
            and field not in blocked
        ):
            blocked.append(field)
    return blocked


def _field_checks() -> dict[str, Callable[[object], bool]]:
    checks: dict[str, Callable[[object], bool]] = {
        field: _has_text for field in _TEXT_FIELDS
    }
    checks.update({field: _has_sha256 for field in _DIGEST_FIELDS})
    checks.update({field: lambda value: value is True
                   for field in _PERMISSION_FIELDS})
    checks.update({field: _approval_fact_is_valid
                   for field in _APPROVAL_FIELDS})
    checks["license_status"] = (
        lambda value: isinstance(value, str) and value in _LICENSE_STATUSES)
    checks["score_min"] = _finite_number
    checks["score_max"] = _finite_number
    checks["score_direction"] = (
        lambda value: isinstance(value, str) and value in _SCORE_DIRECTIONS)
    return checks


_FIELD_CHECKS: Final = _field_checks()


def _category_blocking_fields(category: dict) -> list[str]:
    blocked = [
        field for field, predicate in _FIELD_CHECKS.items()
        if not predicate(category.get(field))
    ]
    score_min = category.get("score_min")
    score_max = category.get("score_max")
    if (_finite_number(score_min) and _finite_number(score_max)
            and score_min >= score_max):
        blocked.append("score_range")
    definition_digest = category.get("definition_digest")
    for field in _APPROVAL_FIELDS:
        fact = category.get(field)
        if (_approval_fact_is_valid(fact)
                and fact["scope_digest"] != definition_digest
                and field not in blocked):
            blocked.append(field)
    return blocked


_ARTIFACT_MATCH_FIELDS: Final[tuple[str, ...]] = (
    "definition_id",
    "instrument_id",
    "instrument_version",
    "definition_digest",
    "item_set_digest",
    "administration_protocol_digest",
    "response_schema_digest",
    "result_schema_digest",
    "missingness_rule_digest",
    "stopping_rule_digest",
    "scoring_algorithm_id",
    "scoring_algorithm_version",
    "scoring_algorithm_digest",
    "score_min",
    "score_max",
    "score_direction",
    "score_rounding_rule",
    "automatic_scoring_permitted",
    "item_response_storage_permitted",
    "result_storage_permitted",
    "result_export_permitted",
)


def definition_artifacts_match(
    manifest: dict,
    bundles: Iterable[AssessmentDefinitionBundle],
) -> bool:
    """Require one approved runtime bundle to match every frozen fact exactly."""
    installed = tuple(bundles)
    if len(installed) != 1:
        return False
    bundle = installed[0]
    if bundle.formal_research_approved is not True:
        return False
    if (
        manifest.get("definition_bundle_id") != bundle.bundle_id
        or manifest.get("definition_bundle_digest") != bundle.bundle_digest
    ):
        return False
    categories = manifest.get("categories")
    if not isinstance(categories, list):
        return False
    by_category = {
        row.get("category_key"): row
        for row in categories
        if isinstance(row, dict)
    }
    if len(by_category) != 2:
        return False
    for definition in bundle.definitions:
        category = by_category.get(definition.category_key)
        if category is None:
            return False
        for field in _ARTIFACT_MATCH_FIELDS:
            if category.get(field) != getattr(definition, field):
                return False
    return True


def evaluate_scale_protocol_manifest(
    manifest: dict,
    *,
    registered_definition_bundles: Iterable[AssessmentDefinitionBundle] = (),
    formal_result_contract_implemented: bool = FORMAL_RESULT_CONTRACT_IMPLEMENTED,
    workflow_contract_implemented: bool = WORKFLOW_CONTRACT_IMPLEMENTED,
    definition_artifact_enforcement_implemented: bool = (
        DEFINITION_ARTIFACT_ENFORCEMENT_IMPLEMENTED),
    workflow_policy_enforcement_implemented: bool = (
        WORKFLOW_POLICY_ENFORCEMENT_IMPLEMENTED),
) -> dict:
    """Return a defensive, readiness-derived representation of ``manifest``.

    This evaluator intentionally overwrites claimed readiness, status, and
    blocker values.  It is also useful when a partially completed manifest is
    shown for governance review: completed facts remain visible while the
    category stays blocked.
    """
    result = deepcopy(manifest)
    categories = result.get("categories")
    if not isinstance(categories, list):
        raise ValueError("scale protocol categories must be a list")
    expected_keys = {
        "untrained_standardized_naming",
        "functional_communication",
    }
    actual_keys = [row.get("category_key") for row in categories
                   if isinstance(row, dict)]
    if (len(categories) != 2 or len(actual_keys) != 2
            or set(actual_keys) != expected_keys or len(set(actual_keys)) != 2):
        raise ValueError("scale protocol must contain the two frozen categories")

    blocking_issues: list[dict] = []
    for category in categories:
        if category.get("required") is not True:
            raise ValueError("both scale protocol categories must be required")
        blocked_fields = _category_blocking_fields(category)
        category["scoring_ready"] = not blocked_fields
        for field in blocked_fields:
            category_key = category["category_key"]
            blocking_issues.append({
                "code": f"{category_key}.{field}.not_ready",
                "category_key": category_key,
                "field": field,
                "message": (
                    f"{category['label']}：{_FIELD_LABELS[field]}尚未形成可验证的冻结事实。"
                ),
            })

    bundle_identity_ready = True
    if not _has_text(result.get("definition_bundle_id")):
        bundle_identity_ready = False
        blocking_issues.append({
            "code": "platform.definition_bundle_id.not_ready",
            "category_key": "platform",
            "field": "definition_bundle_id",
            "message": "平台：正式两表定义包标识尚未冻结。",
        })
    if not _has_sha256(result.get("definition_bundle_digest")):
        bundle_identity_ready = False
        blocking_issues.append({
            "code": "platform.definition_bundle_digest.not_ready",
            "category_key": "platform",
            "field": "definition_bundle_digest",
            "message": "平台：正式两表定义包摘要尚未冻结。",
        })

    definition_ready = (
        bundle_identity_ready
        and all(category["scoring_ready"] for category in categories)
    )

    workflow_policy = result.get("workflow_policy")
    if workflow_policy is None:
        workflow_policy = _empty_workflow_policy()
        result["workflow_policy"] = workflow_policy
    if not isinstance(workflow_policy, dict):
        raise ValueError("scale protocol workflow_policy must be an object")
    workflow_policy_blocked = _workflow_policy_blocking_fields(workflow_policy)
    for field in workflow_policy_blocked:
        blocking_issues.append({
            "code": f"workflow_policy.{field}.not_ready",
            "category_key": "workflow_policy",
            "field": field,
            "message": (
                f"正式评估工作流：{_WORKFLOW_FIELD_LABELS[field]}"
                "尚未形成可验证的冻结事实。"
            ),
        })
    workflow_policy_ready = not workflow_policy_blocked

    definition_artifact_enforcement_ready = bool(
        definition_artifact_enforcement_implemented)
    workflow_policy_enforcement_ready = bool(
        workflow_policy_enforcement_implemented)
    if not definition_artifact_enforcement_ready:
        blocking_issues.append(deepcopy(
            _DEFINITION_ARTIFACT_ENFORCEMENT_BLOCKER))
    if not workflow_policy_enforcement_ready:
        blocking_issues.append(deepcopy(
            _WORKFLOW_POLICY_ENFORCEMENT_BLOCKER))

    definition_artifacts_ready = (
        definition_ready
        and definition_artifact_enforcement_ready
        and definition_artifacts_match(result, registered_definition_bundles)
    )
    if not definition_artifacts_ready:
        blocking_issues.append(deepcopy(_DEFINITION_ARTIFACT_BLOCKER))

    # These two flags are release facts passed by trusted application code;
    # claimed fields in ``manifest`` are deliberately ignored.
    formal_result_contract_ready = bool(formal_result_contract_implemented)
    workflow_contract_ready = bool(workflow_contract_implemented)
    if not formal_result_contract_ready:
        blocking_issues.append(deepcopy(_FORMAL_RESULT_CONTRACT_BLOCKER))
    if not workflow_contract_ready:
        blocking_issues.append(deepcopy(_WORKFLOW_BLOCKER))
    workflow_ready = (
        workflow_policy_ready
        and workflow_contract_ready
        and workflow_policy_enforcement_ready
    )

    ready_for_research = (
        definition_ready
        and definition_artifacts_ready
        and formal_result_contract_ready
        and workflow_ready
    )
    if not definition_ready:
        status = "awaiting_pi_definition"
    elif not workflow_policy_ready:
        status = "awaiting_workflow_policy"
    elif (
        not formal_result_contract_ready
        or not workflow_contract_ready
        or not definition_artifact_enforcement_ready
        or not workflow_policy_enforcement_ready
    ):
        status = "awaiting_platform_implementation"
    elif not definition_artifacts_ready:
        status = "awaiting_definition_artifacts"
    else:
        status = "ready_for_research"

    result["schema_version"] = SCALE_PROTOCOL_SCHEMA_VERSION
    result["status"] = status
    result["definition_ready"] = definition_ready
    result["definition_artifact_enforcement_ready"] = (
        definition_artifact_enforcement_ready)
    result["definition_artifacts_ready"] = definition_artifacts_ready
    result["formal_result_contract_ready"] = formal_result_contract_ready
    result["workflow_policy_ready"] = workflow_policy_ready
    result["workflow_contract_ready"] = workflow_contract_ready
    result["workflow_policy_enforcement_ready"] = (
        workflow_policy_enforcement_ready)
    result["workflow_ready"] = workflow_ready
    result["ready_for_research"] = ready_for_research
    result["instance_creation_enabled"] = ready_for_research
    result["automatic_scoring_enabled"] = ready_for_research
    result["training_metrics_are_formal_scale_results"] = False
    result["blocking_issues"] = blocking_issues
    return result


def scale_protocol_readiness() -> dict:
    """Return the current manifest with all readiness facts derived afresh."""
    from .assessment_definitions import AssessmentDefinitionError, current_bundle

    try:
        installed = (current_bundle().snapshot,)
    except AssessmentDefinitionError:
        installed = ()
    return evaluate_scale_protocol_manifest(
        _MANIFEST,
        registered_definition_bundles=installed,
    )
