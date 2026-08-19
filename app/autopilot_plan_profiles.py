"""Version-pinned, simulation-only autopilot plan profile resolution.

Profiles select canonical positions; they never copy clinical content or create
an alternate item bank.  A paired-null binding means the existing full-source
canonical plan.  A paired-set binding must resolve through the immutable
registry below and satisfy its canonical parent and simulation boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import autopilot_positions, content, runtime


PROFILE_SCHEMA_VERSION = "autopilot-demo-profile.v1"
WEEK2_SINGLE20_DEMO_VERSION = "week2-single20-demo-v1"
WEEK2_SINGLE20_DEMO_DIGEST = (
    "089c44fc5f20b541b374b24289693e066550acf6999e0b5dc382cd5f10ba71fc"
)
_PROFILE_PATH = content.CONTENT_DIR / "autopilot_demo_profiles_v1.json"
_PROFILE_REGISTRY: dict[str, tuple[Path, str]] = {
    WEEK2_SINGLE20_DEMO_VERSION: (
        _PROFILE_PATH,
        WEEK2_SINGLE20_DEMO_DIGEST,
    ),
}
_TRUE_VALUES = frozenset({"1", "true", "yes"})
SIMULATION_DATA_ENV = "ALLOW_SIMULATION_DATA"
P0A_FEATURE_ENV = "ENABLE_AUTOPILOT_P0A_SIMULATION"


class PlanProfileError(ValueError):
    """Stable, structured failure from the single profile parser/resolver."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> NoReturn:
    raise PlanProfileError(code, message)


class _ProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_schema_version: Literal["autopilot-demo-profile.v1"]
    profile_version_id: Literal["week2-single20-demo-v1"]
    simulation_only: Literal[True]
    usage_scope: Literal["simulation_demo_non_research"]
    week_no: Literal[2]
    phase_type: Literal["正式训练"]
    event_line: Literal["正式训练"]
    parent_item_bank_version_id: str = Field(min_length=1)
    parent_item_bank_definition_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$")
    parent_autopilot_protocol_version_id: str = Field(min_length=1)
    parent_autopilot_protocol_definition_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$")
    parent_source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_source_normalized_text_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$")
    ordered_position_keys: list[str] = Field(min_length=20, max_length=20)
    allowed_task_types: list[Literal["单要素"]] = Field(
        min_length=1, max_length=1)
    plan_position_count: Literal[20]
    completion_scope: Literal["demo_plan_only"]
    profile_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    qc_status: Literal["draft"]


@dataclass(frozen=True)
class AutopilotPlanProfileDefinition:
    profile_schema_version: str
    profile_version_id: str
    profile_definition_digest: str
    simulation_only: bool
    usage_scope: str
    week_no: int
    phase_type: str
    event_line: str
    parent_item_bank_version_id: str
    parent_item_bank_definition_digest: str
    parent_autopilot_protocol_version_id: str
    parent_autopilot_protocol_definition_digest: str
    parent_source_document_sha256: str
    parent_source_normalized_text_sha256: str
    ordered_position_keys: tuple[str, ...]
    allowed_task_types: tuple[str, ...]
    plan_position_count: int
    completion_scope: Literal["demo_plan_only"]
    qc_status: str


@dataclass(frozen=True)
class ResolvedAutopilotPlan:
    profile_version_id: str | None
    profile_definition_digest: str | None
    parent_item_bank_version_id: str
    parent_item_bank_definition_digest: str
    parent_autopilot_protocol_version_id: str
    parent_autopilot_protocol_definition_digest: str
    session_plan: runtime.SessionPlan
    positions: tuple[autopilot_positions.ProtocolPosition, ...]
    simulation_only: bool
    completion_scope: Literal["canonical_full_source", "demo_plan_only"]
    resolved_position_count: int
    resolved_position_content_ready: bool
    structured_readiness_gaps: tuple[autopilot_positions.PositionGap, ...]
    source_unstructured_gaps: tuple[str, ...]
    unsupported_position_count: int
    source_protocol_position_count: int

    @property
    def plan(self) -> runtime.SessionPlan:
        return self.session_plan

    @property
    def ordered_protocol_positions(
        self,
    ) -> tuple[autopilot_positions.ProtocolPosition, ...]:
        return self.positions


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _canonical_profile_digest(definition: dict) -> str:
    payload = {
        key: value
        for key, value in definition.items()
        if key != "profile_definition_digest"
    }
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        _fail("plan_profile_invalid", f"profile 定义不可规范化：{exc}")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _definition_from_schema(schema: _ProfileSchema) -> AutopilotPlanProfileDefinition:
    return AutopilotPlanProfileDefinition(
        profile_schema_version=schema.profile_schema_version,
        profile_version_id=schema.profile_version_id,
        profile_definition_digest=schema.profile_definition_digest,
        simulation_only=schema.simulation_only,
        usage_scope=schema.usage_scope,
        week_no=schema.week_no,
        phase_type=schema.phase_type,
        event_line=schema.event_line,
        parent_item_bank_version_id=schema.parent_item_bank_version_id,
        parent_item_bank_definition_digest=(
            schema.parent_item_bank_definition_digest),
        parent_autopilot_protocol_version_id=(
            schema.parent_autopilot_protocol_version_id),
        parent_autopilot_protocol_definition_digest=(
            schema.parent_autopilot_protocol_definition_digest),
        parent_source_document_sha256=schema.parent_source_document_sha256,
        parent_source_normalized_text_sha256=(
            schema.parent_source_normalized_text_sha256),
        ordered_position_keys=tuple(schema.ordered_position_keys),
        allowed_task_types=tuple(schema.allowed_task_types),
        plan_position_count=schema.plan_position_count,
        completion_scope=schema.completion_scope,
        qc_status=schema.qc_status,
    )


def _load_registered_definition(
    profile_version_id: str,
) -> AutopilotPlanProfileDefinition:
    registered = _PROFILE_REGISTRY.get(profile_version_id)
    if registered is None:
        _fail("plan_profile_unknown", "未知的自动演示计划 profile")
    profile_path, pinned_digest = registered
    try:
        raw = content._load_strict_json_object(  # noqa: SLF001
            profile_path, label="自动演示计划 profile")
        if (
            type(raw.get("simulation_only")) is not bool
            or type(raw.get("week_no")) is not int
            or type(raw.get("plan_position_count")) is not int
        ):
            raise ValueError("bool/int 字段必须使用 exact JSON 类型")
        schema = _ProfileSchema.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        _fail("plan_profile_invalid", f"自动演示计划 profile 不合法：{exc}")
    calculated_digest = _canonical_profile_digest(raw)
    if (
        schema.profile_definition_digest != calculated_digest
        or calculated_digest != pinned_digest
    ):
        _fail(
            "plan_profile_digest_mismatch",
            "自动演示计划 profile 摘要与定义或 registry pin 不一致",
        )
    if schema.profile_version_id != profile_version_id:
        _fail(
            "plan_profile_unknown",
            "自动演示计划 profile 文件与请求 version 不一致",
        )
    definition = _definition_from_schema(schema)
    if len(set(definition.ordered_position_keys)) != len(
            definition.ordered_position_keys):
        _fail("plan_profile_invalid", "自动演示计划 profile 含重复位置")
    return definition


def _validate_canonical_parent(
    bank: content.ItemBank,
    protocol: dict,
) -> tuple[str, str, str, str, int]:
    validation = content.validate_item_bank(bank)
    if validation["errors"]:
        _fail("plan_profile_parent_mismatch", "当前题库定义校验失败")
    protocol_issues = content.validate_autopilot_protocol(protocol)
    if protocol_issues:
        _fail("plan_profile_parent_mismatch", "当前自动协议定义校验失败")
    bank_digest = content.item_bank_definition_digest(bank)
    protocol_digest = content.autopilot_protocol_definition_digest(protocol)
    protocol_version = protocol.get("protocol_version_id")
    if not isinstance(protocol_version, str) or not protocol_version.strip():
        _fail("plan_profile_parent_mismatch", "当前自动协议缺版本身份")
    source_position_count = bank.meta.get("source_protocol_position_count")
    if (
        not isinstance(source_position_count, int)
        or isinstance(source_position_count, bool)
        or source_position_count < 0
    ):
        _fail("plan_profile_parent_mismatch", "当前题库缺合法源题位总数")
    return (
        bank.version_id,
        bank_digest,
        protocol_version,
        protocol_digest,
        source_position_count,
    )


def resolve_requested_definition(
    profile_version_id: str | None,
    *,
    bank: content.ItemBank,
    protocol: dict,
) -> AutopilotPlanProfileDefinition | None:
    """Resolve one explicitly requested immutable definition, without subject checks."""
    if profile_version_id is None:
        return None
    if (
        not isinstance(profile_version_id, str)
        or not profile_version_id
        or profile_version_id != profile_version_id.strip()
    ):
        _fail("plan_profile_unknown", "自动演示计划 profile version 不合法")
    definition = _load_registered_definition(profile_version_id)
    (
        bank_version,
        bank_digest,
        protocol_version,
        protocol_digest,
        _source_position_count,
    ) = _validate_canonical_parent(bank, protocol)
    bank_source_document = bank.meta.get("source_document_sha256")
    bank_source_normalized = bank.meta.get("source_normalized_text_sha256")
    if (
        definition.parent_item_bank_version_id != bank_version
        or definition.parent_item_bank_definition_digest != bank_digest
        or definition.parent_autopilot_protocol_version_id != protocol_version
        or definition.parent_autopilot_protocol_definition_digest
        != protocol_digest
        or definition.parent_source_document_sha256 != bank_source_document
        or definition.parent_source_normalized_text_sha256
        != bank_source_normalized
        or definition.parent_source_document_sha256
        != protocol.get("source_document_sha256")
        or definition.parent_source_normalized_text_sha256
        != protocol.get("source_normalized_text_sha256")
    ):
        _fail(
            "plan_profile_parent_mismatch",
            "自动演示计划 profile 的 canonical parent 身份不一致",
        )
    canonical = _canonical_resolution(
        bank=bank,
        protocol=protocol,
        week_no=definition.week_no,
        event_line=definition.event_line,
    )
    _selected_positions(definition, canonical)
    return definition


def assert_profile_subject_boundary(
    definition: AutopilotPlanProfileDefinition,
    *,
    is_simulation: bool,
) -> None:
    if definition.simulation_only and is_simulation is not True:
        _fail(
            "plan_profile_simulation_required",
            "自动演示计划 profile 只能用于显式 simulation subject",
        )


def assert_profile_context_boundary(
    definition: AutopilotPlanProfileDefinition,
    *,
    week_no: int,
    phase_type: object,
    event_line: object,
) -> None:
    if (
        week_no != definition.week_no
        or _enum_value(phase_type) != definition.phase_type
        or _enum_value(event_line) != definition.event_line
    ):
        _fail(
            "plan_profile_context_mismatch",
            "自动演示计划 profile 与 week/phase/event context 不一致",
        )


def _source_unstructured_keys(bank: content.ItemBank) -> tuple[str, ...]:
    raw = bank.meta.get("source_unstructured_positions")
    if not isinstance(raw, list):
        _fail("plan_profile_parent_mismatch", "当前题库缺源题位缺口列表")
    keys: list[str] = []
    for row in raw:
        key = row.get("source_position_key") if isinstance(row, dict) else None
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            _fail("plan_profile_parent_mismatch", "当前题库源题位缺口不合法")
        keys.append(key)
    if len(keys) != len(set(keys)):
        _fail("plan_profile_parent_mismatch", "当前题库源题位缺口重复")
    return tuple(keys)


def _canonical_resolution(
    *,
    bank: content.ItemBank,
    protocol: dict,
    week_no: int,
    event_line: object,
) -> ResolvedAutopilotPlan:
    (
        bank_version,
        bank_digest,
        protocol_version,
        protocol_digest,
        source_position_count,
    ) = _validate_canonical_parent(bank, protocol)
    try:
        plan = runtime.build_session_plan(
            bank, week_no, _enum_value(event_line))
    except ValueError as exc:
        _fail("plan_profile_parent_mismatch", str(exc))
    positions = autopilot_positions.plan_positions(plan)
    structured_gaps = tuple(
        gap
        for position in positions
        if (gap := autopilot_positions.readiness_gap(bank, position)) is not None
    )
    source_gaps = _source_unstructured_keys(bank)
    if len(positions) + len(source_gaps) != source_position_count:
        _fail(
            "plan_profile_parent_mismatch",
            "canonical structured/source-unstructured 题位与源总数不一致",
        )
    unsupported_count = len(structured_gaps) + len(source_gaps)
    return ResolvedAutopilotPlan(
        profile_version_id=None,
        profile_definition_digest=None,
        parent_item_bank_version_id=bank_version,
        parent_item_bank_definition_digest=bank_digest,
        parent_autopilot_protocol_version_id=protocol_version,
        parent_autopilot_protocol_definition_digest=protocol_digest,
        session_plan=plan,
        positions=positions,
        simulation_only=False,
        completion_scope="canonical_full_source",
        resolved_position_count=len(positions),
        resolved_position_content_ready=unsupported_count == 0,
        structured_readiness_gaps=structured_gaps,
        source_unstructured_gaps=source_gaps,
        unsupported_position_count=unsupported_count,
        source_protocol_position_count=source_position_count,
    )


def _resolution_for_definition(
    definition: AutopilotPlanProfileDefinition,
    *,
    bank: content.ItemBank,
    protocol: dict,
) -> ResolvedAutopilotPlan:
    canonical = _canonical_resolution(
        bank=bank,
        protocol=protocol,
        week_no=definition.week_no,
        event_line=definition.event_line,
    )
    positions = _selected_positions(definition, canonical)
    selected_items = tuple(
        canonical.session_plan.items[position.item_index]
        for position in positions
    )
    if len({item.item_id for item in selected_items}) != len(selected_items):
        _fail("plan_profile_invalid", "profile 位置没有一题一项")
    plan = runtime.SessionPlan(
        canonical.session_plan.item_bank_version_id,
        canonical.session_plan.week_no,
        canonical.session_plan.event_line,
        selected_items,
    )
    structured_gaps = tuple(
        gap
        for position in positions
        if (gap := autopilot_positions.readiness_gap(bank, position)) is not None
    )
    if structured_gaps:
        _fail("plan_profile_invalid", "profile 选择了尚未冻结运行内容的位置")
    return ResolvedAutopilotPlan(
        profile_version_id=definition.profile_version_id,
        profile_definition_digest=definition.profile_definition_digest,
        parent_item_bank_version_id=canonical.parent_item_bank_version_id,
        parent_item_bank_definition_digest=(
            canonical.parent_item_bank_definition_digest),
        parent_autopilot_protocol_version_id=(
            canonical.parent_autopilot_protocol_version_id),
        parent_autopilot_protocol_definition_digest=(
            canonical.parent_autopilot_protocol_definition_digest),
        session_plan=plan,
        positions=positions,
        simulation_only=definition.simulation_only,
        completion_scope=definition.completion_scope,
        resolved_position_count=len(positions),
        resolved_position_content_ready=True,
        structured_readiness_gaps=(),
        source_unstructured_gaps=(),
        unsupported_position_count=0,
        source_protocol_position_count=canonical.source_protocol_position_count,
    )


def _selected_positions(
    definition: AutopilotPlanProfileDefinition,
    canonical: ResolvedAutopilotPlan,
) -> tuple[autopilot_positions.ProtocolPosition, ...]:
    by_key = {position.position_key: position for position in canonical.positions}
    expected_single_keys = tuple(
        position.position_key
        for position in canonical.positions
        if position.task_type == "单要素" and position.response_role == "命名"
    )
    if (
        definition.allowed_task_types != ("单要素",)
        or definition.plan_position_count != 20
        or len(definition.ordered_position_keys) != 20
        or definition.ordered_position_keys != expected_single_keys
    ):
        _fail(
            "plan_profile_invalid",
            "自动演示计划 profile 不是 canonical 单要素 20 题 exact 顺序",
        )
    try:
        return tuple(
            by_key[position_key]
            for position_key in definition.ordered_position_keys
        )
    except KeyError as exc:
        _fail("plan_profile_invalid", f"profile 含未知 canonical 位置：{exc}")


def resolve_requested_profile(
    profile_version_id: str | None,
    *,
    bank: content.ItemBank,
    protocol: dict,
    is_simulation: bool,
    week_no: int,
    phase_type: object,
    event_line: object,
) -> ResolvedAutopilotPlan:
    if profile_version_id is None:
        return _canonical_resolution(
            bank=bank,
            protocol=protocol,
            week_no=week_no,
            event_line=event_line,
        )
    definition = resolve_requested_definition(
        profile_version_id, bank=bank, protocol=protocol)
    assert definition is not None
    assert_profile_subject_boundary(
        definition, is_simulation=is_simulation)
    assert_profile_context_boundary(
        definition,
        week_no=week_no,
        phase_type=phase_type,
        event_line=event_line,
    )
    return _resolution_for_definition(definition, bank=bank, protocol=protocol)


def resolve_bound_profile(
    profile_version_id: str | None,
    profile_definition_digest: str | None,
    *,
    bank: content.ItemBank,
    protocol: dict,
    is_simulation: bool,
    week_no: int,
    phase_type: object,
    event_line: object,
) -> ResolvedAutopilotPlan:
    if (profile_version_id is None) != (profile_definition_digest is None):
        _fail(
            "plan_profile_binding_incomplete",
            "自动演示计划 profile version/digest 必须成对存在",
        )
    if profile_version_id is None:
        return _canonical_resolution(
            bank=bank,
            protocol=protocol,
            week_no=week_no,
            event_line=event_line,
        )
    definition = resolve_requested_definition(
        profile_version_id, bank=bank, protocol=protocol)
    assert definition is not None
    if profile_definition_digest != definition.profile_definition_digest:
        _fail(
            "plan_profile_digest_mismatch",
            "已绑定自动演示计划 profile 摘要不一致",
        )
    assert_profile_subject_boundary(
        definition, is_simulation=is_simulation)
    assert_profile_context_boundary(
        definition,
        week_no=week_no,
        phase_type=phase_type,
        event_line=event_line,
    )
    return _resolution_for_definition(definition, bank=bank, protocol=protocol)


_HEX_DIGITS = frozenset("0123456789abcdef")


def resolve_registered_binding(
    profile_version_id: str | None,
    profile_definition_digest: str | None,
) -> AutopilotPlanProfileDefinition | None:
    """Resolve one stored binding from the immutable registry alone.

    This is the historical-replay boundary.  It deliberately never loads,
    accepts or compares today's item bank or autopilot protocol, and never
    builds a session plan: a row frozen against an older parent must keep
    resolving unchanged after the canonical content moves on.  Callers that
    need a parent comparison make it against their own frozen columns.

    Returns ``None`` for a complete paired-null binding, which is the
    permanent representation of the canonical full-source plan.
    """
    if profile_version_id is None and profile_definition_digest is None:
        return None
    if (profile_version_id is None) != (profile_definition_digest is None):
        _fail(
            "plan_profile_binding_incomplete",
            "自动演示计划 profile version/digest 必须成对存在",
        )
    if (
        not isinstance(profile_version_id, str)
        or not profile_version_id
        or profile_version_id != profile_version_id.strip()
    ):
        _fail("plan_profile_unknown", "自动演示计划 profile version 不合法")
    if (
        not isinstance(profile_definition_digest, str)
        or len(profile_definition_digest) != 64
        or not set(profile_definition_digest) <= _HEX_DIGITS
    ):
        _fail(
            "plan_profile_digest_mismatch",
            "自动演示计划 profile 摘要格式非法",
        )
    definition = _load_registered_definition(profile_version_id)
    if definition.profile_definition_digest != profile_definition_digest:
        _fail(
            "plan_profile_digest_mismatch",
            "已绑定自动演示计划 profile 摘要与不可变注册定义不一致",
        )
    return definition


def _default_bank() -> content.ItemBank:
    return content.load_item_bank_for_week(2)


def _default_protocol() -> dict:
    return content.load_autopilot_protocol(
        content.CONTENT_DIR / "autopilot_protocol_v1.json")


def resolve_for_visit_plan(
    plan: object,
    *,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
) -> ResolvedAutopilotPlan:
    resolved_bank = _default_bank() if bank is None else bank
    resolved_protocol = _default_protocol() if protocol is None else protocol
    return resolve_bound_profile(
        getattr(plan, "autopilot_profile_version_id", None),
        getattr(plan, "autopilot_profile_definition_digest", None),
        bank=resolved_bank,
        protocol=resolved_protocol,
        is_simulation=getattr(plan, "is_simulation"),
        week_no=getattr(plan, "week_no"),
        phase_type=getattr(plan, "phase_type"),
        event_line=getattr(plan, "event_line"),
    )


def resolve_for_session(
    session: object,
    *,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
) -> ResolvedAutopilotPlan:
    resolved_bank = _default_bank() if bank is None else bank
    resolved_protocol = _default_protocol() if protocol is None else protocol
    return resolve_bound_profile(
        getattr(session, "autopilot_profile_version_id", None),
        getattr(session, "autopilot_profile_definition_digest", None),
        bank=resolved_bank,
        protocol=resolved_protocol,
        is_simulation=getattr(session, "is_simulation"),
        week_no=getattr(session, "week_no"),
        phase_type=getattr(session, "phase_type"),
        event_line=getattr(session, "event_line"),
    )


def demo20_runtime_enabled() -> bool:
    """Whether this process explicitly permits the local synthetic demo.

    This is the single deployment-switch predicate for the D1B profile.  It
    intentionally requires both the synthetic-data boundary and the P0a
    autonomous-runtime switch; neither switch is sufficient on its own.
    """
    return all(
        os.environ.get(name, "").strip().lower() in _TRUE_VALUES
        for name in (SIMULATION_DATA_ENV, P0A_FEATURE_ENV)
    )


def resolve_exact_runnable_demo20(
    row: object,
    *,
    bank: content.ItemBank | None = None,
    protocol: dict | None = None,
    require_runtime_enabled: bool = True,
) -> ResolvedAutopilotPlan:
    """Resolve the one exact, runnable caregiver-facing D1B contract.

    The function accepts either a VisitPlan or Session-like persisted row and
    validates the complete immutable profile pair, simulation/classification
    boundary, current canonical parents and exact 20-position completion
    contract.  Callers performing an operational read or start also leave
    ``require_runtime_enabled`` at its default so the same resolution rechecks
    both deployment switches.  Historical/safety projections may set it false
    to describe an already-created row after the switches are removed; that
    never makes the row operational.
    """
    version, digest = _binding_pair(row)
    if (
        version != WEEK2_SINGLE20_DEMO_VERSION
        or digest != WEEK2_SINGLE20_DEMO_DIGEST
    ):
        _fail(
            "plan_profile_not_exact_demo20",
            "当前安排或场次不是精确 20 题本机模拟演示计划",
        )
    if getattr(row, "is_simulation", None) is not True:
        _fail(
            "plan_profile_simulation_required",
            "20 题本机演示只能用于显式 simulation 场次",
        )
    if getattr(row, "data_classification", None) != "simulation":
        _fail(
            "plan_profile_classification_invalid",
            "20 题本机演示必须明确归类为 simulation",
        )
    if require_runtime_enabled and not demo20_runtime_enabled():
        _fail(
            "plan_profile_runtime_not_enabled",
            "20 题本机模拟演示当前未显式开启",
        )

    resolved_bank = _default_bank() if bank is None else bank
    resolved_protocol = _default_protocol() if protocol is None else protocol
    resolved = resolve_bound_profile(
        version,
        digest,
        bank=resolved_bank,
        protocol=resolved_protocol,
        is_simulation=True,
        week_no=getattr(row, "week_no"),
        phase_type=getattr(row, "phase_type"),
        event_line=getattr(row, "event_line"),
    )
    if (
        resolved.profile_version_id != WEEK2_SINGLE20_DEMO_VERSION
        or resolved.profile_definition_digest != WEEK2_SINGLE20_DEMO_DIGEST
        or resolved.simulation_only is not True
        or resolved.completion_scope != "demo_plan_only"
        or resolved.resolved_position_count != 20
        or resolved.resolved_position_content_ready is not True
        or resolved.unsupported_position_count != 0
        or len(resolved.positions) != 20
    ):
        _fail(
            "plan_profile_not_exact_demo20",
            "20 题本机模拟演示计划没有解析为完整可运行范围",
        )
    return resolved


def _binding_pair(row: object) -> tuple[str | None, str | None]:
    version = getattr(row, "autopilot_profile_version_id", None)
    digest = getattr(row, "autopilot_profile_definition_digest", None)
    if (version is None) != (digest is None):
        _fail(
            "plan_profile_binding_incomplete",
            "自动演示计划 profile version/digest 必须成对存在",
        )
    return version, digest


def assert_plan_session_profile_binding(plan: object, session: object) -> None:
    if _binding_pair(plan) != _binding_pair(session):
        _fail(
            "plan_profile_digest_mismatch",
            "VisitPlan 与 Session 的自动演示计划 profile 绑定不一致",
        )
