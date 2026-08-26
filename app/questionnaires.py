"""量表电子记录（原型道）的冻结定义装载与作答校验。

这条道与正式结局契约（scale_protocol / assessment_*）是两回事，且必须保持两回事：
正式两类结局工具等 PI 冻结三件套，wire 有意不携题词；本道承载的是临床提供、
尚待终确认的人工评价量表（他评/代录问卷），题词经认证接口下发到施测者屏幕，
永不进入前端构建产物。定义包 status 只允许 "prototype"——任何人想把这条道
标成正式结局，装载器直接拒绝，正确做法是走 PI 冻结流程。

计分纪律：只执行源表自己写明的计分说明（如 GDS-15 的反向计分与 ≥8 界值），
源表没定义总分的（SFACS / NPI-Q）绝不发明汇总分。

examiner_scored（ACE-III / 动物流畅性）是检查者当场判分的电子记录：每项存的是
检查者录入的分值或计数（十进制整数串），分档换算与域/总分加总只在锁定时按
源表分档表执行；纸笔任务只记分，不重绘。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

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


class _IntegerDomain(BaseModel):
    """计分框/计数框的值域：闭区间内的十进制整数串（规范形，"05" 不算）。"""
    model_config = ConfigDict(frozen=True)
    minimum: int
    maximum: int


_CANONICAL_INT = re.compile(r"0|[1-9][0-9]*")


def _canonical_int(value: str) -> int | None:
    if _CANONICAL_INT.fullmatch(value) is None:
        return None
    return int(value)


def _domain_accepts(domain: "_ChoiceField | _IntegerDomain", value: str) -> bool:
    if isinstance(domain, _ChoiceField):
        return value in set(domain.allowed)
    number = _canonical_int(value)
    return number is not None and domain.minimum <= number <= domain.maximum


def _domain_hint(domain: "_ChoiceField | _IntegerDomain") -> str:
    if isinstance(domain, _ChoiceField):
        return f"档位必须是 {'、'.join(domain.allowed)} 之一"
    return f"数值必须是 {domain.minimum}–{domain.maximum} 之间的整数"


class _ScoreBin(_StrictSchema):
    """分档表一行：总数落在 [min, max] 记 score 分；max=null 表示「及以上」。"""
    min: int = Field(ge=0)
    max: Optional[int] = Field(default=None, ge=0)
    score: int = Field(ge=0)


def _validate_bins(bins: list[_ScoreBin], maximum: int) -> None:
    ordered = sorted(bins, key=lambda b: b.min, reverse=True)
    if ordered[0].max is not None:
        raise ValueError("分档表最高档 max 必须为 null（及以上）")
    if ordered[-1].min != 0:
        raise ValueError("分档表必须从 0 起")
    if ordered[0].min > maximum:
        raise ValueError("分档表最高档起点超过条目 max")
    for row in ordered[1:]:
        if row.max is None:
            raise ValueError("只有最高档可以开放上限")
        if row.max < row.min:
            raise ValueError("分档 max 不能小于 min")
    for upper, lower in zip(ordered, ordered[1:]):
        if lower.max != upper.min - 1:
            raise ValueError("分档表必须连续且不重叠")
    scores = [row.score for row in ordered]
    if scores != sorted(scores, reverse=True) or len(set(scores)) != len(scores):
        raise ValueError("分档得分必须随总数严格递减")
    if ordered[-1].score != 0:
        raise ValueError("分档表最低档必须记 0 分")


class _ExaminerEntry(_StrictSchema):
    """检查者录入方式：score=直接记分；count=记总数（可按 bins 换算）；choice=闭集选项。"""
    kind: Literal["score", "count", "choice"]
    max: Optional[int] = Field(default=None, ge=1)
    scored: bool
    bins: Optional[list[_ScoreBin]] = Field(default=None, min_length=2)
    choice: Optional[_ChoiceField] = None

    @model_validator(mode="after")
    def _shape(self):
        if self.kind == "score":
            if self.max is None or not self.scored \
                    or self.bins is not None or self.choice is not None:
                raise ValueError("score 条目需要 max，scored 必须为 true，不接受 bins/choice")
        elif self.kind == "count":
            if self.max is None or self.choice is not None:
                raise ValueError("count 条目需要 max，不接受 choice")
            if self.bins is not None:
                if not self.scored:
                    raise ValueError("不计分的 count 条目不接受 bins")
                _validate_bins(self.bins, self.max)
        else:
            if self.choice is None or self.max is not None \
                    or self.scored or self.bins is not None:
                raise ValueError("choice 条目需要 choice，scored 必须为 false，不接受 max/bins")
        return self

    def max_points(self) -> int:
        if not self.scored:
            return 0
        if self.bins:
            return max(row.score for row in self.bins)
        assert self.max is not None
        return self.max

    def points(self, value: str) -> int:
        """终值（已过值域校验）→ 计入总分的分值；不计分条目恒为 0。"""
        if not self.scored:
            return 0
        number = _canonical_int(value)
        assert number is not None
        if self.bins:
            for row in self.bins:
                if number >= row.min and (row.max is None or number <= row.max):
                    return row.score
            raise AssertionError("分档表未覆盖该总数")
        return number

    def domain(self) -> "_ChoiceField | _IntegerDomain":
        if self.choice is not None:
            return self.choice
        assert self.max is not None
        return _IntegerDomain(minimum=0, maximum=self.max)


class _ExaminerItem(_StrictSchema):
    item_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    no: int = Field(ge=1, le=999)
    domain_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    text: str = Field(min_length=1)
    entry: _ExaminerEntry


class _ExaminerDomain(_StrictSchema):
    domain_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    max_score: Optional[int] = Field(default=None, ge=1)


class _ExaminerPanel(_StrictSchema):
    domains: list[_ExaminerDomain] = Field(min_length=1)
    items: list[_ExaminerItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self):
        keys = [domain.domain_key for domain in self.domains]
        if len(set(keys)) != len(keys):
            raise ValueError("domain_key 必须唯一")
        points: dict[str, int] = {key: 0 for key in keys}
        for item in self.items:
            if item.domain_key not in points:
                raise ValueError(f"条目 {item.item_key} 的 domain_key 不在 domains 内")
            points[item.domain_key] += item.entry.max_points()
        for domain in self.domains:
            if not any(item.domain_key == domain.domain_key for item in self.items):
                raise ValueError(f"认知域 {domain.domain_key} 没有条目")
            if domain.max_score is not None and points[domain.domain_key] != domain.max_score:
                raise ValueError(
                    f"认知域 {domain.domain_key} 的条目分值上限之和 "
                    f"{points[domain.domain_key]} ≠ max_score {domain.max_score}")
        return self

    def domain_of(self, item: _ExaminerItem) -> _ExaminerDomain:
        return next(d for d in self.domains if d.domain_key == item.domain_key)


class _Cutoff(_StrictSchema):
    operator: Literal["<=", ">="]
    value: int = Field(ge=0)
    label: str = Field(min_length=1)

    def met(self, total: int) -> bool:
        return total <= self.value if self.operator == "<=" else total >= self.value


class _StratifiedCutoff(_StrictSchema):
    """界值按某个 choice 条目的选项分组；组值为 null = 源表没给该组界值，不判定。"""
    by_item: str = Field(min_length=1)
    groups: dict[str, Optional[_Cutoff]] = Field(min_length=1)
    unjudged_label: str = Field(min_length=1)


class _ExaminerSumScoring(_StrictSchema):
    kind: Literal["examiner_sum"]
    scoring_rule_id: str = Field(min_length=1, max_length=128)
    max_score: Optional[int] = Field(default=None, ge=1)
    cutoff: Optional[_Cutoff] = None
    stratified_cutoff: Optional[_StratifiedCutoff] = None
    rule_verbatim: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_cutoff_rule(self):
        if self.cutoff is not None and self.stratified_cutoff is not None:
            raise ValueError("cutoff 与 stratified_cutoff 只能二选一")
        return self


_Scoring = Annotated[Union[_BinarySumScoring, _ExaminerSumScoring],
                     Field(discriminator="kind")]


class QuestionnaireDefinition(_StrictSchema):
    schema_version: Literal["questionnaire-definition.v1"]
    questionnaire_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    short_name: str = Field(min_length=1, max_length=32)
    respondent: Literal["observer", "patient_reported", "examiner_administered"]
    # 本道结构上只装原型：想标成别的值，先走 PI 冻结流程，不是改这里。
    status: Literal["prototype"]
    provenance: _Provenance
    instruction: str = Field(min_length=1)
    response_kind: Literal[
        "ordinal_sections", "binary_scored", "symptom_triplet", "examiner_scored"]
    value_field: Optional[_ChoiceField] = None
    element_field: Optional[_ElementField] = None
    present_field: Optional[_ChoiceField] = None
    severity_field: Optional[_ChoiceField] = None
    frequency_field: Optional[_ChoiceField] = None
    sections: Optional[list[_Section]] = None
    items: Optional[list[_Item]] = None
    examiner_panel: Optional[_ExaminerPanel] = None
    scoring: Optional[_Scoring] = None
    transcription_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _kind_shape(self):
        kind = self.response_kind
        if kind == "ordinal_sections":
            if not (self.value_field and self.element_field and self.sections):
                raise ValueError("ordinal_sections 需要 value_field/element_field/sections")
            if self.items or self.present_field or self.severity_field \
                    or self.frequency_field or self.examiner_panel:
                raise ValueError(
                    "ordinal_sections 不接受 items/present/severity/frequency/examiner_panel 字段")
            if self.scoring is not None:
                raise ValueError("ordinal_sections 源表未定义计分，scoring 必须为 null")
        elif kind == "binary_scored":
            if not (self.value_field and self.items and self.scoring):
                raise ValueError("binary_scored 需要 value_field/items/scoring")
            if self.scoring.kind != "binary_sum":
                raise ValueError("binary_scored 的 scoring.kind 必须是 binary_sum")
            if self.sections or self.element_field or self.present_field \
                    or self.severity_field or self.frequency_field or self.examiner_panel:
                raise ValueError(
                    "binary_scored 不接受 sections/element/present/severity/frequency/examiner_panel")
            for item in self.items:
                if item.score_when not in set(self.value_field.allowed):
                    raise ValueError(f"条目 {item.item_key} 的 score_when 必须在 allowed 内")
        elif kind == "symptom_triplet":
            if not (self.present_field and self.severity_field
                    and self.frequency_field and self.items):
                raise ValueError("symptom_triplet 需要 present/severity/frequency 字段与 items")
            if self.sections or self.value_field or self.element_field or self.examiner_panel:
                raise ValueError(
                    "symptom_triplet 不接受 sections/value_field/element_field/examiner_panel")
            if self.scoring is not None:
                raise ValueError("symptom_triplet 源表未定义计分，scoring 必须为 null")
            if set(self.present_field.allowed) != {"有", "无"}:
                raise ValueError("present_field 只允许 有/无")
        elif kind == "examiner_scored":
            if not (self.examiner_panel and self.scoring):
                raise ValueError("examiner_scored 需要 examiner_panel/scoring")
            if self.scoring.kind != "examiner_sum":
                raise ValueError("examiner_scored 的 scoring.kind 必须是 examiner_sum")
            if self.value_field or self.element_field or self.present_field \
                    or self.severity_field or self.frequency_field \
                    or self.sections or self.items:
                raise ValueError(
                    "examiner_scored 不接受 value/element/present/severity/frequency/sections/items")
            self._examiner_scoring_consistent(self.examiner_panel, self.scoring)
        else:
            raise ValueError(f"未知的 response_kind {kind}")
        keys = [item.item_key for item in self.all_items()]
        if len(set(keys)) != len(keys):
            raise ValueError("item_key 必须全表唯一")
        nos = [item.no for item in self.all_items()]
        if len(set(nos)) != len(nos):
            raise ValueError("条目序号必须全表唯一")
        return self

    @staticmethod
    def _examiner_scoring_consistent(panel: _ExaminerPanel,
                                     scoring: _ExaminerSumScoring) -> None:
        if scoring.max_score is not None:
            if any(domain.max_score is None for domain in panel.domains):
                raise ValueError("scoring.max_score 非空时每个认知域都必须给 max_score")
            domain_sum = sum(domain.max_score or 0 for domain in panel.domains)
            if domain_sum != scoring.max_score:
                raise ValueError(
                    f"各认知域 max_score 之和 {domain_sum} ≠ scoring.max_score {scoring.max_score}")
        stratified = scoring.stratified_cutoff
        if stratified is not None:
            by_item = next((item for item in panel.items
                            if item.item_key == stratified.by_item), None)
            if by_item is None or by_item.entry.choice is None:
                raise ValueError("stratified_cutoff.by_item 必须指向一个 choice 条目")
            if set(stratified.groups) != set(by_item.entry.choice.allowed):
                raise ValueError("stratified_cutoff.groups 的键必须与该条目 allowed 完全一致")

    def all_items(self) -> list[_Item | _ExaminerItem]:
        if self.sections is not None:
            return [item for section in self.sections for item in section.items]
        if self.examiner_panel is not None:
            return list(self.examiner_panel.items)
        return list(self.items or [])

    def expected_fields(self) -> dict[tuple[str, str], _ChoiceField | _IntegerDomain]:
        """锁定完整性合同：每个 (item_key, field_key) 必须有终值的值域。

        symptom_triplet 的 severity/frequency 不在这里——它们是条件必填，
        由 assert_lock_complete 按 present 终值裁定。
        """
        expected: dict[tuple[str, str], _ChoiceField | _IntegerDomain] = {}
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
        elif self.response_kind == "symptom_triplet":
            assert self.present_field is not None
            for item in self.all_items():
                expected[(item.item_key, FIELD_PRESENT)] = self.present_field
        elif self.response_kind == "examiner_scored":
            assert self.examiner_panel is not None
            for item in self.examiner_panel.items:
                expected[(item.item_key, FIELD_VALUE)] = item.entry.domain()
        else:
            raise AssertionError(f"未知的 response_kind {self.response_kind}")
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

    def value_domain(self, item_key: str,
                     field_key: str) -> _ChoiceField | _IntegerDomain | None:
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


def _slot_label(definition: QuestionnaireDefinition,
                item_key: str, field_key: str) -> str:
    """人读的作答位置(锁定拒绝与越域消息用):第 N 题 / 「节」的「要素」。

    施测者屏幕上不出现 (gds_11, value) 这类内部元组;拼不出人话时才退回内部键。
    """
    for item in definition.all_items():
        if item.item_key == item_key:
            if isinstance(item, _ExaminerItem) and definition.examiner_panel is not None:
                return f"「{definition.examiner_panel.domain_of(item).title}」第 {item.no} 题"
            return f"第 {item.no} 题"
    if item_key.startswith("section:") and field_key.startswith("element:"):
        section_id = item_key[len("section:"):]
        element_key = field_key[len("element:"):]
        section = next((s for s in (definition.sections or [])
                        if s.section_id == section_id), None)
        element = None
        if definition.element_field is not None:
            element = next((e for e in definition.element_field.elements
                            if e.element_key == element_key), None)
        if section is not None and element is not None:
            return f"「{section.title}」的「{element.label}」"
    return f"({item_key}, {field_key})"


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
    if not _domain_accepts(domain, value):
        raise QuestionnaireValidationError(
            "questionnaire_value_out_of_domain",
            f"{_slot_label(definition, item_key, field_key)}的{_domain_hint(domain)}")


def assert_lock_complete(
        definition: QuestionnaireDefinition,
        final_values: dict[tuple[str, str], str]) -> None:
    """锁定完整性：必填全有、值全在域内、symptom_triplet 无矛盾。

    problems 面向施测者屏幕(P1-3):写"第 N 题未作答",不写内部元组;
    错误码字段保持不变,程序仍按 code 分支。
    """
    problems: list[str] = []
    for (item_key, field_key), domain in definition.expected_fields().items():
        value = final_values.get((item_key, field_key))
        label = _slot_label(definition, item_key, field_key)
        if value is None:
            problems.append(
                f"{label}未评" if label.startswith("「") else f"{label}未作答")
        elif not _domain_accepts(domain, value):
            problems.append(
                f"{label}的记录值 {value!r} 不在允许"
                f"{'档位' if isinstance(domain, _ChoiceField) else '范围'}内")
    if definition.response_kind == "symptom_triplet":
        conditional = definition.conditional_fields()
        for item in definition.all_items():
            present = final_values.get((item.item_key, FIELD_PRESENT))
            severity = final_values.get((item.item_key, FIELD_SEVERITY))
            frequency = final_values.get((item.item_key, FIELD_FREQUENCY))
            if present == "有":
                if severity is None:
                    problems.append(f"第 {item.no} 题记为“有”但缺严重度")
                elif severity not in set(
                        conditional[(item.item_key, FIELD_SEVERITY)].allowed):
                    problems.append(f"第 {item.no} 题的严重度不在允许档位内")
                if frequency is None:
                    problems.append(f"第 {item.no} 题记为“有”但缺频率")
                elif frequency not in set(
                        conditional[(item.item_key, FIELD_FREQUENCY)].allowed):
                    problems.append(f"第 {item.no} 题的频率不在允许档位内")
            elif present == "无":
                if severity is not None or frequency is not None:
                    problems.append(
                        f"第 {item.no} 题记为“无”却带严重度/频率——先清除再锁定")
    # 域外键：终值集合里出现定义外的键 = 数据被绕过校验写入，锁定拒绝。
    known = set(definition.expected_fields()) | set(definition.conditional_fields())
    for key in final_values:
        if key not in known:
            problems.append(
                f"出现了定义之外的作答记录 ({key[0]}, {key[1]})，请联系管理员核查")
    if problems:
        raise QuestionnaireValidationError(
            "questionnaire_lock_incomplete",
            "还有条目未完成或存在矛盾，暂不能锁定", problems=problems)


def compute_scoring(
        definition: QuestionnaireDefinition,
        final_values: dict[tuple[str, str], str]) -> dict | None:
    """只执行源表写明的计分说明；未定义计分的表返回 None。"""
    scoring = definition.scoring
    if scoring is None:
        return None
    if scoring.kind == "examiner_sum":
        return _compute_examiner_sum(definition, scoring, final_values)
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


def examiner_domain_totals(
        definition: QuestionnaireDefinition,
        final_values: dict[tuple[str, str], str]) -> dict[str, int]:
    """各认知域小计（只按已录入的终值累加；缺项按 0 计，完整性由锁定合同另管）。"""
    panel = definition.examiner_panel
    assert panel is not None
    totals = {domain.domain_key: 0 for domain in panel.domains}
    for item in panel.items:
        value = final_values.get((item.item_key, FIELD_VALUE))
        if value is None:
            continue
        totals[item.domain_key] += item.entry.points(value)
    return totals


def _compute_examiner_sum(
        definition: QuestionnaireDefinition,
        scoring: _ExaminerSumScoring,
        final_values: dict[tuple[str, str], str]) -> dict:
    panel = definition.examiner_panel
    assert panel is not None
    for item in panel.items:
        if item.entry.scored and final_values.get((item.item_key, FIELD_VALUE)) is None:
            raise QuestionnaireValidationError(
                "questionnaire_scoring_incomplete",
                f"计分前提被破坏：条目 {item.item_key} 缺终值")
    domain_totals = examiner_domain_totals(definition, final_values)
    for domain in panel.domains:
        if domain.max_score is not None and domain_totals[domain.domain_key] > domain.max_score:
            raise QuestionnaireValidationError(
                "questionnaire_scoring_overflow",
                f"计分溢出：{domain.title} {domain_totals[domain.domain_key]} > {domain.max_score}")
    total = sum(domain_totals.values())
    if scoring.max_score is not None and total > scoring.max_score:
        raise QuestionnaireValidationError(
            "questionnaire_scoring_overflow",
            f"计分溢出：{total} > {scoring.max_score}")
    cutoff_met: bool | None = None
    computed_flag: str | None = None
    if scoring.cutoff is not None:
        cutoff_met = scoring.cutoff.met(total)
        computed_flag = scoring.cutoff.label if cutoff_met else None
    elif scoring.stratified_cutoff is not None:
        group = final_values.get((scoring.stratified_cutoff.by_item, FIELD_VALUE))
        if group is None:
            raise QuestionnaireValidationError(
                "questionnaire_scoring_incomplete",
                f"计分前提被破坏：分组条目 {scoring.stratified_cutoff.by_item} 缺终值")
        if group not in scoring.stratified_cutoff.groups:
            raise QuestionnaireValidationError(
                "questionnaire_scoring_incomplete",
                f"计分前提被破坏：分组值 {group!r} 不在分组界值表内")
        rule = scoring.stratified_cutoff.groups[group]
        if rule is None:
            computed_flag = scoring.stratified_cutoff.unjudged_label
        else:
            cutoff_met = rule.met(total)
            computed_flag = rule.label if cutoff_met else None
    return {
        "computed_total": float(total),
        "cutoff_met": cutoff_met,
        "computed_flag": computed_flag,
        "scoring_rule_id": scoring.scoring_rule_id,
    }
