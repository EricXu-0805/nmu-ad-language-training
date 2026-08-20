"""量表电子记录（原型道）的冻结定义装载与作答校验。

这条道与正式结局契约（scale_protocol / assessment_*）是两回事，且必须保持两回事：
正式两类结局工具等 PI 冻结三件套，wire 有意不携题词；本道承载的是临床提供、
尚待终确认的人工评价量表（他评/代录问卷），题词经认证接口下发到施测者屏幕，
永不进入前端构建产物。定义包 status 只允许 "prototype"——任何人想把这条道
标成正式结局，装载器直接拒绝，正确做法是走 PI 冻结流程。

计分纪律：只执行源表自己写明的计分说明（如 GDS-15 的反向计分与 ≥8 界值），
源表没定义总分的（SFACS / NPI-Q）绝不发明汇总分。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .content import (
    CONTENT_DIR,
    FrozenContentUnavailable,
    _load_strict_json_object,
)

QUESTIONNAIRE_DIR = "questionnaires"
QUESTIONNAIRE_INDEX_FILE = "questionnaire_index.json"
QUESTIONNAIRE_INDEX_SCHEMA_VERSION = "questionnaire-index.v1"
QUESTIONNAIRE_DEFINITION_SCHEMA_VERSION = "questionnaire-definition.v1"

PHASE_LABELS = ("前测", "后测", "随访", "其他")

# 逐条目字段名的闭集（response_kind 决定用哪些）。
FIELD_VALUE = "value"
FIELD_PRESENT = "present"
FIELD_SEVERITY = "severity"
FIELD_FREQUENCY = "frequency"


class QuestionnaireValidationError(ValueError):
    """一次作答写入/锁定违反了定义包的值域或完整性合同。"""

    def __init__(self, code: str, message: str, *, problems: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.problems = problems or []


class _StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _IndexEntry(_StrictSchema):
    questionnaire_id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _plain_file_name(self):
        if "/" in self.file or "\\" in self.file or self.file.startswith("."):
            raise ValueError("索引条目 file 只能是裸文件名")
        return self


class _Index(_StrictSchema):
    schema_version: Literal["questionnaire-index.v1"]
    note: Optional[str] = None
    questionnaires: list[_IndexEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_unique(self):
        ids = [entry.questionnaire_id for entry in self.questionnaires]
        if len(set(ids)) != len(ids):
            raise ValueError("索引 questionnaire_id 必须唯一")
        return self


class _Provenance(_StrictSchema):
    provided_by: str = Field(min_length=1)
    provided_via: str = Field(min_length=1)
    provided_on: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_confirmation: str = Field(min_length=1)


class _ChoiceField(_StrictSchema):
    allowed: list[str] = Field(min_length=2)
    anchors: dict[str, str]

    @model_validator(mode="after")
    def _anchors_cover_allowed(self):
        if len(set(self.allowed)) != len(self.allowed):
            raise ValueError("allowed 值必须唯一")
        if set(self.anchors) != set(self.allowed):
            raise ValueError("anchors 键必须与 allowed 完全一致")
        return self


class _Element(_StrictSchema):
    element_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1)


class _ElementField(_StrictSchema):
    allowed: list[str] = Field(min_length=2)
    anchors: dict[str, str]
    elements: list[_Element] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        if set(self.anchors) != set(self.allowed):
            raise ValueError("element anchors 键必须与 allowed 完全一致")
        keys = [element.element_key for element in self.elements]
        if len(set(keys)) != len(keys):
            raise ValueError("element_key 必须唯一")
        return self


class _Item(_StrictSchema):
    item_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    no: int = Field(ge=1, le=999)
    text: str = Field(min_length=1)
    name: Optional[str] = None
    score_when: Optional[str] = None


class _Section(_StrictSchema):
    section_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    items: list[_Item] = Field(min_length=1)


class _BinarySumScoring(_StrictSchema):
    kind: Literal["binary_sum"]
    scoring_rule_id: str = Field(min_length=1, max_length=128)
    max_score: int = Field(ge=1)
    cutoff_value: int = Field(ge=0)
    cutoff_operator: Literal[">="]
    cutoff_label: str = Field(min_length=1)
    rule_verbatim: str = Field(min_length=1)


class QuestionnaireDefinition(_StrictSchema):
    schema_version: Literal["questionnaire-definition.v1"]
    questionnaire_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    short_name: str = Field(min_length=1, max_length=32)
    respondent: Literal["observer", "patient_reported"]
    # 本道结构上只装原型：想标成别的值，先走 PI 冻结流程，不是改这里。
    status: Literal["prototype"]
    provenance: _Provenance
    instruction: str = Field(min_length=1)
    response_kind: Literal["ordinal_sections", "binary_scored", "symptom_triplet"]
    value_field: Optional[_ChoiceField] = None
    element_field: Optional[_ElementField] = None
    present_field: Optional[_ChoiceField] = None
    severity_field: Optional[_ChoiceField] = None
    frequency_field: Optional[_ChoiceField] = None
    sections: Optional[list[_Section]] = None
    items: Optional[list[_Item]] = None
    scoring: Optional[_BinarySumScoring] = None
    transcription_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_shape(self):
        kind = self.response_kind
        if kind == "ordinal_sections":
            if not (self.value_field and self.element_field and self.sections):
                raise ValueError("ordinal_sections 需要 value_field/element_field/sections")
            if self.items or self.present_field or self.severity_field or self.frequency_field:
                raise ValueError("ordinal_sections 不接受 items/present/severity/frequency 字段")
            if self.scoring is not None:
                raise ValueError("ordinal_sections 源表未定义计分，scoring 必须为 null")
        elif kind == "binary_scored":
            if not (self.value_field and self.items and self.scoring):
                raise ValueError("binary_scored 需要 value_field/items/scoring")
            if self.sections or self.element_field or self.present_field \
                    or self.severity_field or self.frequency_field:
                raise ValueError("binary_scored 不接受 sections/element/present/severity/frequency")
            for item in self.items:
                if item.score_when not in set(self.value_field.allowed):
                    raise ValueError(f"条目 {item.item_key} 的 score_when 必须在 allowed 内")
        elif kind == "symptom_triplet":
            if not (self.present_field and self.severity_field
                    and self.frequency_field and self.items):
                raise ValueError("symptom_triplet 需要 present/severity/frequency 字段与 items")
            if self.sections or self.value_field or self.element_field:
                raise ValueError("symptom_triplet 不接受 sections/value_field/element_field")
            if self.scoring is not None:
                raise ValueError("symptom_triplet 源表未定义计分，scoring 必须为 null")
            if set(self.present_field.allowed) != {"有", "无"}:
                raise ValueError("present_field 只允许 有/无")
        keys = [item.item_key for item in self.all_items()]
        if len(set(keys)) != len(keys):
            raise ValueError("item_key 必须全表唯一")
        nos = [item.no for item in self.all_items()]
        if len(set(nos)) != len(nos):
            raise ValueError("条目序号必须全表唯一")
        return self

    def all_items(self) -> list[_Item]:
        if self.sections is not None:
            return [item for section in self.sections for item in section.items]
        return list(self.items or [])

    def expected_fields(self) -> dict[tuple[str, str], _ChoiceField]:
        """锁定完整性合同：每个 (item_key, field_key) 必须有终值的闭集。

        symptom_triplet 的 severity/frequency 不在这里——它们是条件必填，
        由 assert_lock_complete 按 present 终值裁定。
        """
        expected: dict[tuple[str, str], _ChoiceField] = {}
        if self.response_kind == "ordinal_sections":
            assert self.value_field is not None and self.element_field is not None
            for item in self.all_items():
                expected[(item.item_key, FIELD_VALUE)] = self.value_field
            element_choice = _ChoiceField(
                allowed=list(self.element_field.allowed),
                anchors=dict(self.element_field.anchors))
            for section in self.sections or []:
                for element in self.element_field.elements:
                    expected[(f"section:{section.section_id}",
                              f"element:{element.element_key}")] = element_choice
        elif self.response_kind == "binary_scored":
            assert self.value_field is not None
            for item in self.all_items():
                expected[(item.item_key, FIELD_VALUE)] = self.value_field
        else:
            assert self.present_field is not None
            for item in self.all_items():
                expected[(item.item_key, FIELD_PRESENT)] = self.present_field
        return expected

    def conditional_fields(self) -> dict[tuple[str, str], _ChoiceField]:
        """symptom_triplet 的 severity/frequency：present=有 才必填。"""
        if self.response_kind != "symptom_triplet":
            return {}
        assert self.severity_field is not None and self.frequency_field is not None
        conditional: dict[tuple[str, str], _ChoiceField] = {}
        for item in self.all_items():
            conditional[(item.item_key, FIELD_SEVERITY)] = _ChoiceField(
                allowed=list(self.severity_field.allowed),
                anchors=dict(self.severity_field.anchors))
            conditional[(item.item_key, FIELD_FREQUENCY)] = _ChoiceField(
                allowed=list(self.frequency_field.allowed),
                anchors=dict(self.frequency_field.anchors))
        return conditional

    def value_domain(self, item_key: str, field_key: str) -> _ChoiceField | None:
        domain = self.expected_fields().get((item_key, field_key))
        if domain is not None:
            return domain
        return self.conditional_fields().get((item_key, field_key))


def load_questionnaire_registry(
        content_dir: str | Path | None = None) -> dict[str, "LoadedQuestionnaire"]:
    """装载全部注册问卷；索引字节钉 + 严格 schema，任何缺陷 fail-closed。"""
    base = Path(content_dir) if content_dir is not None else CONTENT_DIR
    directory = base / QUESTIONNAIRE_DIR
    index_path = directory / QUESTIONNAIRE_INDEX_FILE
    if not index_path.exists():
        # 与生产题库同口径：尚未交付是现状，不是错误。
        return {}
    try:
        index = _Index.model_validate(
            _load_strict_json_object(index_path, label="问卷索引"))
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(f"问卷索引不可用：{exc}") from exc

    registry: dict[str, LoadedQuestionnaire] = {}
    for entry in index.questionnaires:
        path = directory / entry.file
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise FrozenContentUnavailable(
                f"问卷数据包 {entry.file} 读取失败：{exc}") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if actual != entry.content_sha256:
            raise FrozenContentUnavailable(
                f"问卷数据包 {entry.file} 与索引登记哈希不一致（字节钉拒绝装载）")
        try:
            definition = QuestionnaireDefinition.model_validate(
                _load_strict_json_object(path, label=f"问卷数据包 {entry.file}"))
        except FrozenContentUnavailable:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise FrozenContentUnavailable(
                f"问卷数据包 {entry.file} 结构不合法：{exc}") from exc
        if definition.questionnaire_id != entry.questionnaire_id:
            raise FrozenContentUnavailable(
                f"问卷数据包 {entry.file} 声明的 questionnaire_id 与索引不一致")
        registry[entry.questionnaire_id] = LoadedQuestionnaire(
            definition=definition, content_sha256=actual)
    return registry


class LoadedQuestionnaire(BaseModel):
    model_config = ConfigDict(frozen=True)
    definition: QuestionnaireDefinition
    content_sha256: str


def validate_value_write(
        definition: QuestionnaireDefinition,
        item_key: str, field_key: str, value: str | None) -> None:
    """草稿逐值校验：键必须在定义内，值必须在闭集内；None=清除，永远合法。"""
    domain = definition.value_domain(item_key, field_key)
    if domain is None:
        raise QuestionnaireValidationError(
            "questionnaire_field_unknown",
            f"条目/字段 ({item_key}, {field_key}) 不在定义 "
            f"{definition.questionnaire_id} 内")
    if value is None:
        return
    if value not in set(domain.allowed):
        raise QuestionnaireValidationError(
            "questionnaire_value_out_of_domain",
            f"({item_key}, {field_key}) 的值必须是 {domain.allowed} 之一")


def assert_lock_complete(
        definition: QuestionnaireDefinition,
        final_values: dict[tuple[str, str], str]) -> None:
    """锁定完整性：必填全有、值全在域内、symptom_triplet 无矛盾。"""
    problems: list[str] = []
    for (item_key, field_key), domain in definition.expected_fields().items():
        value = final_values.get((item_key, field_key))
        if value is None:
            problems.append(f"缺少 ({item_key}, {field_key})")
        elif value not in set(domain.allowed):
            problems.append(f"({item_key}, {field_key}) 值 {value!r} 越域")
    if definition.response_kind == "symptom_triplet":
        conditional = definition.conditional_fields()
        for item in definition.all_items():
            present = final_values.get((item.item_key, FIELD_PRESENT))
            severity = final_values.get((item.item_key, FIELD_SEVERITY))
            frequency = final_values.get((item.item_key, FIELD_FREQUENCY))
            if present == "有":
                if severity is None:
                    problems.append(f"条目 {item.item_key} 记为“有”但缺严重度")
                elif severity not in set(
                        conditional[(item.item_key, FIELD_SEVERITY)].allowed):
                    problems.append(f"条目 {item.item_key} 严重度越域")
                if frequency is None:
                    problems.append(f"条目 {item.item_key} 记为“有”但缺频率")
                elif frequency not in set(
                        conditional[(item.item_key, FIELD_FREQUENCY)].allowed):
                    problems.append(f"条目 {item.item_key} 频率越域")
            elif present == "无":
                if severity is not None or frequency is not None:
                    problems.append(
                        f"条目 {item.item_key} 记为“无”却带严重度/频率——先清除再锁定")
    # 域外键：终值集合里出现定义外的键 = 数据被绕过校验写入，锁定拒绝。
    known = set(definition.expected_fields()) | set(definition.conditional_fields())
    for key in final_values:
        if key not in known:
            problems.append(f"存在定义外的作答键 {key}")
    if problems:
        raise QuestionnaireValidationError(
            "questionnaire_lock_incomplete",
            "作答尚不满足锁定完整性合同", problems=problems)


def compute_scoring(
        definition: QuestionnaireDefinition,
        final_values: dict[tuple[str, str], str]) -> dict | None:
    """只执行源表写明的计分说明；未定义计分的表返回 None。"""
    scoring = definition.scoring
    if scoring is None:
        return None
    assert scoring.kind == "binary_sum"
    total = 0
    for item in definition.all_items():
        value = final_values.get((item.item_key, FIELD_VALUE))
        if value is None:
            raise QuestionnaireValidationError(
                "questionnaire_scoring_incomplete",
                f"计分前提被破坏：条目 {item.item_key} 缺终值")
        if value == item.score_when:
            total += 1
    if total > scoring.max_score:
        raise QuestionnaireValidationError(
            "questionnaire_scoring_overflow",
            f"计分溢出：{total} > {scoring.max_score}")
    cutoff_met = total >= scoring.cutoff_value
    return {
        "computed_total": float(total),
        "cutoff_met": cutoff_met,
        # 源表只定义了达界标签；未达界不发明标签。
        "computed_flag": scoring.cutoff_label if cutoff_met else None,
        "scoring_rule_id": scoring.scoring_rule_id,
    }
