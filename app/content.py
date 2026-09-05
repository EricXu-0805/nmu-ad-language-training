"""训练内容加载与校验（M5）。

把结构化题库/脚本从 JSON 载入并校验一致性——把「双人校对四者一致」自动化：
目标词 ↔ 成功话术 ↔ 告知话术 ↔ 线索 对不上就报出来（斧子+树 那类复制粘贴勘误即由此发现）。
严格 JSON 解码后再由 Pydantic 闭合模型校验；禁止类型强转和附加字段。
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel, ConfigDict, Field, ValidationError, field_validator,
    model_validator,
)

from .image_id_contract import require_training_image_id


@dataclass(frozen=True)
class ItemBank:
    version_id: str
    single_element: list[dict]
    double_element: list[dict]
    multi_element: list[dict] = field(default_factory=list)
    errata_fixed: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def supported_training_weeks(self) -> tuple[int, ...]:
        """当前结构化内容真正覆盖的训练周；不得由 UI/运行时自行外推。"""
        raw = self.meta.get("supported_training_weeks", [])
        return tuple(int(w) for w in raw if isinstance(w, int) and 2 <= w <= 8)

    @property
    def qc_status(self) -> str:
        return str(self.meta.get("qc_status") or "draft")


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ITEM_BANK_SELF_DIGEST_FIELDS = frozenset({
    "definition_digest",
    "item_bank_definition_digest",
})
_AUTOPILOT_SELF_DIGEST_FIELDS = frozenset({
    "definition_digest",
    "protocol_definition_digest",
    "autopilot_protocol_definition_digest",
})


class _StrictJSONError(ValueError):
    """A frozen content definition violated the strict JSON data model."""


class FrozenContentUnavailable(ValueError):
    """A packaged frozen definition could not be safely loaded.

    This deliberately remains a ``ValueError`` subtype for non-HTTP validation
    callers while giving the API one narrow exception type to map to 503.
    """


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict:
    """Build an object while rejecting duplicate keys at every nesting level."""
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError(f"JSON 对象包含重复键 {key!r}")
        result[key] = value
    return result


def _reject_non_finite_constant(token: str) -> None:
    # Python's decoder accepts NaN/Infinity although RFC 8259 JSON does not.
    raise _StrictJSONError(f"JSON 包含非有限数 {token!r}")


def _strict_finite_float(token: str) -> float:
    # ``parse_constant`` catches literal NaN/Infinity, while a syntactically
    # ordinary exponent such as 1e400 overflows to infinity in binary64 and
    # therefore needs its own finite check.
    value = float(token)
    if not math.isfinite(value):
        raise _StrictJSONError(f"JSON 包含非有限数 {token!r}")
    return value


def _load_strict_json_object(path: str | Path, *, label: str) -> dict:
    """Load one frozen JSON definition using a fail-closed shared contract."""
    source = Path(path)
    try:
        data = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_non_finite_constant,
            parse_float=_strict_finite_float,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON 语法不合法：{exc.msg}") from exc
    except _StrictJSONError as exc:
        raise ValueError(f"{label} JSON 不合法：{exc}") from exc
    except RecursionError as exc:
        # CPython's JSON decoder raises RecursionError (not JSONDecodeError)
        # for adversarially deep arrays/objects.  Normalize only that known
        # parser failure here so callers keep a narrow, controlled boundary.
        raise ValueError(f"{label} JSON 嵌套过深") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} JSON 根必须是对象")
    return data


def _blank_string_paths(value: object, path: tuple[str, ...] = ()):
    """Yield pure-whitespace string locations without normalizing the input."""
    pending = [(value, path)]
    while pending:
        current, current_path = pending.pop()
        if isinstance(current, str):
            if not current.strip():
                yield current_path
            continue
        if isinstance(current, dict):
            children = []
            for key, child in current.items():
                key_label = str(key)
                if isinstance(key, str) and not key.strip():
                    yield (*current_path, "<blank-key>")
                children.append((child, (*current_path, key_label)))
            pending.extend(reversed(children))
            continue
        if isinstance(current, (list, tuple)):
            pending.extend(
                (child, (*current_path, str(index)))
                for index, child in reversed(tuple(enumerate(current)))
            )


class _FrozenContentSchema(BaseModel):
    """Strict, closed schema shared by packaged clinical content definitions.

    Validation is deliberately non-coercing and does not serialize the model back
    over the source dictionary.  The original JSON object therefore remains the
    definition-digest input byte-for-byte in value semantics while malformed
    shapes fail before any runtime helper can interpret truthiness.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_pure_whitespace_strings(cls, value: object) -> object:
        blank_path = next(_blank_string_paths(value), None)
        if blank_path is not None:
            location = ".".join(blank_path) or "root"
            raise ValueError(f"pure-whitespace strings are forbidden at {location}")
        # Validation is observational.  In particular, do not strip strings:
        # their exact values remain part of the frozen definition digest.
        return value


class _AssetDeliveryManifestSchema(_FrozenContentSchema):
    version_id: str = Field(min_length=1)
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _SourceUnstructuredPositionSchema(_FrozenContentSchema):
    source_position_key: str = Field(min_length=1)
    response_role: str = Field(min_length=1)
    source_paragraphs: list[int]
    status: Literal["awaiting_content_decision", "awaiting_pi_rubric"]

    @field_validator("source_paragraphs")
    @classmethod
    def _paragraph_span_is_ordered(cls, value: list[int]) -> list[int]:
        if len(value) != 2 or value[0] < 0 or value[1] < value[0]:
            raise ValueError("must be one ordered, non-negative two-index span")
        return value


class _ErratumSchema(_FrozenContentSchema):
    item: str = Field(min_length=1)
    field: str = Field(min_length=1)
    source_value: str = Field(min_length=1)
    corrected_to: str = Field(min_length=1)
    note: str = Field(min_length=1)


class _CueVariantSchema(_FrozenContentSchema):
    text: str = Field(min_length=1)
    source_paragraph_index: int = Field(ge=0)


class _Cue1VariantsSchema(_FrozenContentSchema):
    unknown: _CueVariantSchema
    close: _CueVariantSchema
    silence: _CueVariantSchema


class _Cue1Schema(_FrozenContentSchema):
    cue_type: str = Field(min_length=1)
    text: str = Field(min_length=1)
    variants: _Cue1VariantsSchema


class _Cue2Schema(_FrozenContentSchema):
    cue_type: str = Field(min_length=1)
    text: str = Field(min_length=1)


class _SingleCuesSchema(_FrozenContentSchema):
    cue_1: _Cue1Schema = Field(alias="1")
    cue_2: _Cue2Schema = Field(alias="2")


class _RubricCuesSchema(_FrozenContentSchema):
    cue_1: str = Field(alias="1", min_length=1)
    cue_2: str = Field(alias="2", min_length=1)


class _OperationalRubricSchema(_FrozenContentSchema):
    rubric_version: str = Field(min_length=1)
    decision_policy: Literal[
        "any_acceptable_expression", "all_required_concepts",
        "all_concept_groups", "hybrid"
    ]
    acceptable_expressions: list[str] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    required_concept_groups: list[list[str]] | None = None
    cues: _RubricCuesSchema
    tell_answer: str = Field(min_length=1)

    @model_validator(mode="after")
    def _has_operational_truth(self):
        group_values = tuple(
            value for group in (self.required_concept_groups or [])
            for value in group)
        values = (*self.acceptable_expressions, *self.required_concepts,
                  *group_values)
        if not any(value.strip() for value in values):
            raise ValueError("must contain at least one non-blank truth expression")
        if any(not value.strip() for value in values):
            raise ValueError("truth expressions must not be blank")
        if self.decision_policy == "all_concept_groups":
            if not self.required_concept_groups \
                    or any(not group for group in self.required_concept_groups):
                raise ValueError(
                    "all_concept_groups requires non-empty required_concept_groups")
        elif self.required_concept_groups is not None:
            raise ValueError(
                "required_concept_groups is only valid with all_concept_groups")
        return self


class _ImageItemSchema(_FrozenContentSchema):
    image_id: str = Field(min_length=1)

    @field_validator("image_id")
    @classmethod
    def _uses_canonical_image_id_contract(cls, value: str) -> str:
        return require_training_image_id(value)


class _SingleItemSchema(_ImageItemSchema):
    item_id: str = Field(min_length=1)
    task_type: Literal["单要素"]
    target_word: str = Field(min_length=1)
    acceptable_expressions: list[str]
    related_but_inaccurate: list[str]
    initial_prompt: str = Field(min_length=1)
    success_line: str = Field(min_length=1)
    cues: _SingleCuesSchema
    tell_answer: str = Field(min_length=1)
    item_set_type: str = Field(min_length=1)
    difficulty_level: str | int | None
    raw_source_ref: str = Field(min_length=1)

    @field_validator("acceptable_expressions", "related_but_inaccurate")
    @classmethod
    def _term_lists_have_no_blank_values(cls, value: list[str]) -> list[str]:
        if any(not term.strip() for term in value):
            raise ValueError("term list must not contain blank values")
        return value


class _DoubleItemSchema(_ImageItemSchema):
    item_id: str = Field(min_length=1)
    task_type: Literal["双要素"]
    pair_title: str = Field(min_length=1)
    left_word: str = Field(min_length=1)
    right_word: str = Field(min_length=1)
    left_function_cue: str = Field(min_length=1)
    right_function_cue: str = Field(min_length=1)
    relation_cue: str = Field(min_length=1)
    item_set_type: str = Field(min_length=1)
    difficulty_level: str | int | None
    raw_source_ref: str = Field(min_length=1)
    operational_rubrics: dict[str, _OperationalRubricSchema] | None = None


class _KeyElementSchema(_FrozenContentSchema):
    key: str | None = None
    id: str | None = None
    label: str | None = None
    target: str | None = None

    @model_validator(mode="after")
    def _has_identity_and_label(self):
        if not any(value and value.strip() for value in (self.key, self.id)):
            raise ValueError("must contain a non-blank key or id")
        if not any(value and value.strip() for value in (self.label, self.target)):
            raise ValueError("must contain a non-blank label or target")
        for value in (self.key, self.id, self.label, self.target):
            if value is not None and not value.strip():
                raise ValueError("key-element strings must not be blank")
        return self


class _MultiItemSchema(_ImageItemSchema):
    item_id: str = Field(min_length=1)
    task_type: Literal["多要素"]
    scene_title: str = Field(min_length=1)
    initial_prompt: str = Field(min_length=1)
    item_set_type: str = Field(min_length=1)
    difficulty_level: str | int | None
    raw_source_ref: str = Field(min_length=1)
    key_elements: list[str | _KeyElementSchema] = Field(min_length=1)
    operational_rubrics: dict[str, _OperationalRubricSchema] | None = None

    @field_validator("key_elements")
    @classmethod
    def _string_key_elements_are_non_blank(
        cls, value: list[str | _KeyElementSchema]
    ) -> list[str | _KeyElementSchema]:
        if any(isinstance(element, str) and not element.strip() for element in value):
            raise ValueError("string key elements must not be blank")
        return value


class _ItemBankSchema(_FrozenContentSchema):
    item_bank_version_id: str = Field(min_length=1)
    content_schema_version: str = Field(min_length=1)
    training_week_no: int = Field(ge=2, le=8)
    supported_training_weeks: list[int] = Field(min_length=1)
    qc_status: Literal["draft", "reviewed", "frozen"]
    source: str = Field(min_length=1)
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_revision: str = Field(min_length=1)
    patient_asset_delivery_manifest: _AssetDeliveryManifestSchema
    source_protocol_position_count: int = Field(ge=0)
    source_unstructured_positions: list[_SourceUnstructuredPositionSchema]
    source_unstructured_blockers: list[str]
    note: str = Field(min_length=1)
    errata_fixed: list[_ErratumSchema]
    single_element: list[_SingleItemSchema]
    double_element: list[_DoubleItemSchema]
    multi_element: list[_MultiItemSchema] = Field(default_factory=list)
    cue1_variant_source_locator_scheme: str = Field(min_length=1)
    definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    item_bank_definition_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("supported_training_weeks")
    @classmethod
    def _weeks_are_unique_and_bounded(cls, value: list[int]) -> list[int]:
        if any(week < 2 or week > 8 for week in value) or len(set(value)) != len(value):
            raise ValueError("must contain unique training weeks in 2..8")
        return value

    @field_validator("source_unstructured_blockers")
    @classmethod
    def _blockers_are_non_blank(cls, value: list[str]) -> list[str]:
        if any(not blocker.strip() for blocker in value):
            raise ValueError("blockers must not contain blank strings")
        return value


class _ProfileSlotSchema(_FrozenContentSchema):
    source: str = Field(min_length=1)
    fallback: str = Field(min_length=1)


class _ZodiacSlotSchema(_FrozenContentSchema):
    extract: Literal["zodiac_closed_list"]
    inject_hotword: bool
    fallback_line: str = Field(min_length=1)


class _OpenPhraseSlotSchema(_FrozenContentSchema):
    extract: Literal["open_phrase"]
    fallback_line: str = Field(min_length=1)


class _Week1SlotsSchema(_FrozenContentSchema):
    appellation: _ProfileSlotSchema = Field(alias="称呼")
    zodiac: _ZodiacSlotSchema = Field(alias="老人所说的属相")
    interest: _OpenPhraseSlotSchema = Field(alias="老人所说的兴趣")
    activity: _OpenPhraseSlotSchema = Field(alias="老人所说的活动")
    place: _OpenPhraseSlotSchema = Field(alias="老人所说的地点")
    object_name: _OpenPhraseSlotSchema = Field(alias="老人所说的物品")


class _Week1QuestionSchema(_FrozenContentSchema):
    slot_field: str = Field(min_length=1)
    ask: str = Field(min_length=1)
    success: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, min_length=1)


class _Week1SectionSchema(_FrozenContentSchema):
    key: str = Field(min_length=1)
    speaker: Literal["研究者", "机器人"]
    note: str | None = Field(default=None, min_length=1)
    questions: list[_Week1QuestionSchema] | None = None
    line: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _has_section_content(self):
        if self.questions is not None and not self.questions:
            raise ValueError("questions must be absent or non-empty")
        if self.speaker == "机器人" and not self.questions and not self.line:
            raise ValueError("robot section must contain questions or line")
        if not self.note and not self.questions and not self.line:
            raise ValueError("section must contain note, questions, or line")
        return self


class _Week1ScriptSchema(_FrozenContentSchema):
    script_version_id: str = Field(min_length=1)
    phase_type: Literal["关系建立"]
    event_line: Literal["关系建立环节"]
    source: str = Field(min_length=1)
    robot_name_config_key: str = Field(min_length=1)
    zodiac_closed_list: list[str]
    slots: _Week1SlotsSchema
    silence_seconds: int
    generic_fallback_line: str = Field(min_length=1)
    sections: list[_Week1SectionSchema] = Field(min_length=1)

    @field_validator("zodiac_closed_list")
    @classmethod
    def _zodiac_is_the_closed_twelve(cls, value: list[str]) -> list[str]:
        expected = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
        if value != expected:
            raise ValueError("must be the canonical ordered twelve-item zodiac list")
        return value

    @field_validator("sections")
    @classmethod
    def _section_keys_are_unique(
        cls, value: list[_Week1SectionSchema]
    ) -> list[_Week1SectionSchema]:
        keys = [section.key for section in value]
        if len(set(keys)) != len(keys):
            raise ValueError("section keys must be globally unique")
        return value

    @field_validator("sections")
    @classmethod
    def _completion_contract_structure(
        cls, value: list[_Week1SectionSchema]
    ) -> list[_Week1SectionSchema]:
        # 完成合同的结构前提在装载时就钉死:末节必须是机器人道别台词,
        # 自我介绍节(直接身份红线的载体)必须存在。否则真实受试者会在
        # 准入放行后被完成门禁永久卡死(rapport_script_invalid)。
        last = value[-1]
        if last.speaker != "机器人" or not (last.line or "").strip():
            raise ValueError(
                "last section must be the robot farewell line "
                "(week-1 completion contract anchors it)")
        if not any(section.key == "自我介绍" for section in value):
            raise ValueError(
                "自我介绍 section is required "
                "(direct-identifier red line anchors it)")
        return value


class _Cue1SuccessSchema(_FrozenContentSchema):
    unknown: str = Field(min_length=1)
    close: str = Field(min_length=1)
    silence: str = Field(min_length=1)


class _NamingProtocolSchema(_FrozenContentSchema):
    success_after_cue1: _Cue1SuccessSchema
    success_after_cue2: str = Field(min_length=1)


class _DoubleProtocolSchema(_FrozenContentSchema):
    namefix_left: str = Field(min_length=1)
    namefix_right: str = Field(min_length=1)


class _InteractionPackageRefSchema(_FrozenContentSchema):
    file: str = Field(pattern=r"^[A-Za-z0-9._\-]+\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _AutopilotProtocolSchema(_FrozenContentSchema):
    protocol_schema_version: Literal["autopilot-protocol.v1"]
    protocol_version_id: str = Field(min_length=1)
    qc_status: Literal["draft", "reviewed", "frozen"]
    draft_revision: str = Field(min_length=1)
    supported_training_weeks: list[int] = Field(min_length=1)
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(min_length=1)
    silence_seconds: int
    naming: _NamingProtocolSchema
    double: _DoubleProtocolSchema
    interaction_packages: dict[str, _InteractionPackageRefSchema] | None = None
    notes: list[str]
    definition_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_definition_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    autopilot_protocol_definition_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("supported_training_weeks")
    @classmethod
    def _weeks_are_unique_and_bounded(cls, value: list[int]) -> list[int]:
        if any(week < 2 or week > 8 for week in value) or len(set(value)) != len(value):
            raise ValueError("must contain unique training weeks in 2..8")
        return value

    @field_validator("notes")
    @classmethod
    def _notes_are_non_blank(cls, value: list[str]) -> list[str]:
        if any(not note.strip() for note in value):
            raise ValueError("notes must not contain blank strings")
        return value

    @field_validator("interaction_packages")
    @classmethod
    def _interaction_keys_are_weeks(
        cls, value: dict[str, _InteractionPackageRefSchema] | None,
    ) -> dict[str, _InteractionPackageRefSchema] | None:
        if value is not None and any(
                key not in {"2", "3", "4", "5", "6", "7", "8"} for key in value):
            raise ValueError("interaction_packages keys must be '2'..'8'")
        return value


# 交互数据包内话术的封闭来源集：verbatim=源 docx 逐字句；bank_field=同题冻结
# 题库字段；protocol_namefix=协议纠正模板+题库目标词展开。三者之外没有话术
# 来源——引擎与 TTS 白名单都据此解析，数据包自身永远不携带新写的临床措辞。
_INTERACTION_BANK_FIELDS = ("left_function_cue", "right_function_cue", "relation_cue")


class _InteractionSpeechSchema(_FrozenContentSchema):
    source: Literal["verbatim", "bank_field", "protocol_namefix"]
    text: str | None = Field(default=None, min_length=1)
    source_paragraph_index: int | None = Field(default=None, ge=0)
    field: Literal[
        "left_function_cue", "right_function_cue", "relation_cue"] | None = None
    side: Literal["left", "right"] | None = None

    @model_validator(mode="after")
    def _shape_matches_source(self) -> "_InteractionSpeechSchema":
        shapes = {
            "verbatim": (self.text is not None
                         and self.source_paragraph_index is not None
                         and self.field is None and self.side is None),
            "bank_field": (self.field is not None and self.text is None
                           and self.source_paragraph_index is None
                           and self.side is None),
            "protocol_namefix": (self.side is not None and self.text is None
                                 and self.source_paragraph_index is None
                                 and self.field is None),
        }
        if not shapes[self.source]:
            raise ValueError(f"speech fields do not match source={self.source}")
        return self


class _InteractionQuestionSchema(_FrozenContentSchema):
    source: Literal["verbatim"]
    text: str = Field(min_length=1)
    source_paragraph_index: int = Field(ge=0)


class _InteractionBranchSchema(_FrozenContentSchema):
    kind: Literal["advance_silent", "speak_then_advance", "speak_then_rerecord"]
    speech: _InteractionSpeechSchema | None = None

    @model_validator(mode="after")
    def _speech_presence_matches_kind(self) -> "_InteractionBranchSchema":
        if (self.speech is None) != (self.kind == "advance_silent"):
            raise ValueError("speech must be present iff the branch speaks")
        return self


class _InteractionAfterRerecordSchema(_FrozenContentSchema):
    full: _InteractionBranchSchema
    otherwise: _InteractionBranchSchema

    @model_validator(mode="after")
    def _second_recording_is_terminal(self) -> "_InteractionAfterRerecordSchema":
        if "speak_then_rerecord" in (self.full.kind, self.otherwise.kind):
            raise ValueError("after_rerecord branches must be terminal")
        return self


class _InteractionTurnSchema(_FrozenContentSchema):
    turn_seq: int = Field(ge=1)
    response_role: str = Field(min_length=1)
    max_recordings: Literal[1, 2]
    question: _InteractionQuestionSchema
    branches: dict[str, _InteractionBranchSchema]
    after_rerecord: _InteractionAfterRerecordSchema | None = None

    @model_validator(mode="after")
    def _branches_are_closed_and_consistent(self) -> "_InteractionTurnSchema":
        keys = set(self.branches)
        if not {"full", "wrong", "silence"} <= keys or not keys <= {
                "full", "partial", "wrong", "silence"}:
            raise ValueError(
                "branches must be full/wrong/silence plus optional partial")
        has_rerecord = any(
            branch.kind == "speak_then_rerecord"
            for branch in self.branches.values())
        if has_rerecord != (self.after_rerecord is not None):
            raise ValueError(
                "after_rerecord must exist iff a branch rerecords")
        if self.max_recordings != (2 if has_rerecord else 1):
            raise ValueError("max_recordings must match the branch structure")
        return self


class _InteractionItemSchema(_FrozenContentSchema):
    item_id: str = Field(min_length=1)
    task_type: Literal["双要素", "多要素"]
    turns: list[_InteractionTurnSchema] = Field(min_length=1)

    @field_validator("turns")
    @classmethod
    def _turn_seqs_are_dense(
        cls, value: list[_InteractionTurnSchema],
    ) -> list[_InteractionTurnSchema]:
        if [turn.turn_seq for turn in value] != list(range(1, len(value) + 1)):
            raise ValueError("turn_seq must be dense from 1")
        return value


class _FrozenOutSourceLineSchema(_FrozenContentSchema):
    item_id: str = Field(min_length=1)
    turn_seq: int = Field(ge=1)
    source_paragraph_index: int = Field(ge=0)
    reason: str = Field(min_length=1)


class _AutopilotInteractionSchema(_FrozenContentSchema):
    interaction_schema_version: Literal["autopilot-interaction.v1"]
    package_version_id: str = Field(min_length=1)
    week_no: int
    qc_status: Literal["draft", "reviewed", "frozen"]
    draft_revision: str = Field(min_length=1)
    source_file: str = Field(min_length=1)
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_normalized_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_item_bank_version_id: str = Field(min_length=1)
    parent_item_bank_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    silence_seconds: int
    items: list[_InteractionItemSchema] = Field(min_length=1)
    structure_freezes: list[str] = Field(min_length=1)
    frozen_out_source_lines: list[_FrozenOutSourceLineSchema]
    notes: list[str]

    @field_validator("week_no")
    @classmethod
    def _week_is_bounded(cls, value: int) -> int:
        if not 2 <= value <= 8:
            raise ValueError("week_no must be in 2..8")
        return value


def _validate_frozen_schema(data: dict, schema: type[BaseModel], *, label: str) -> None:
    """Validate without coercing or replacing the definition used for hashing."""
    try:
        schema.model_validate(data)
    except ValidationError as exc:
        failures = []
        for error in exc.errors(include_url=False, include_input=False)[:8]:
            location = ".".join(str(part) for part in error["loc"]) or "root"
            failures.append(f"{location}:{error['type']}")
        raise ValueError(f"{label} JSON 结构不合法：{'；'.join(failures)}") from exc


def _validate_declared_definition_digests(
    definition: dict,
    *,
    digest_fields: frozenset[str],
    expected_digest: str,
    label: str,
) -> None:
    """Require every present self-digest alias to match runtime identity."""
    for field_name in digest_fields:
        if field_name in definition and definition[field_name] != expected_digest:
            raise ValueError(f"{label} {field_name} 与运行时定义摘要不一致")


def _canonical_definition_sha256(
    definition: dict,
    *,
    excluded_top_level_fields: frozenset[str],
) -> str:
    """Hash one complete JSON definition using a stable canonical encoding.

    Only explicitly self-referential *top-level* digest fields are omitted.  Source
    artifact digests and nested rubric/approval digests remain part of the frozen
    definition, so changing any operational content or provenance changes the
    resulting identity.
    """
    if not isinstance(definition, dict):
        raise TypeError("definition 必须是字典")
    payload = {
        key: value
        for key, value in definition.items()
        if key not in excluded_top_level_fields
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def item_bank_definition(bank: ItemBank) -> dict:
    """Return the complete serializable definition represented by ``ItemBank``."""
    definition = deepcopy(bank.meta)
    definition["item_bank_version_id"] = bank.version_id
    definition["single_element"] = deepcopy(bank.single_element)
    definition["double_element"] = deepcopy(bank.double_element)
    definition["multi_element"] = deepcopy(bank.multi_element)
    definition["errata_fixed"] = deepcopy(bank.errata_fixed)
    return definition


def item_bank_definition_digest(bank: ItemBank) -> str:
    """Stable SHA-256 identity for the full item-bank definition."""
    return _canonical_definition_sha256(
        item_bank_definition(bank),
        excluded_top_level_fields=_ITEM_BANK_SELF_DIGEST_FIELDS,
    )


def autopilot_protocol_definition_digest(protocol: dict) -> str:
    """Stable SHA-256 identity for the full automatic-interaction protocol."""
    return _canonical_definition_sha256(
        protocol,
        excluded_top_level_fields=_AUTOPILOT_SELF_DIGEST_FIELDS,
    )


def load_item_bank(path: str | Path) -> ItemBank:
    try:
        data = _load_strict_json_object(path, label="题库")
        _validate_frozen_schema(data, _ItemBankSchema, label="题库")
        vid = data["item_bank_version_id"]
        bank = ItemBank(
            version_id=vid,
            single_element=data.get("single_element", []),
            double_element=data.get("double_element", []),
            multi_element=data.get("multi_element", []),
            errata_fixed=data.get("errata_fixed", []),
            meta={k: v for k, v in data.items()
                  if k not in (
                      "single_element", "double_element", "multi_element",
                      "errata_fixed",
                  )},
        )
        _validate_declared_definition_digests(
            data,
            digest_fields=_ITEM_BANK_SELF_DIGEST_FIELDS,
            expected_digest=item_bank_definition_digest(bank),
            label="题库",
        )
        return bank
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(str(exc)) from exc


class TrainingWeekContentUnavailable(FrozenContentUnavailable):
    """The requested training week has no registered structured bank yet."""

    def __init__(self, week_no: int, message: str):
        super().__init__(message)
        self.week_no = week_no


# 第 1 周关系建立不消耗训练题库；其场次/安排的题库版本绑定锚定平台默认题库
# （周 2 数据包），仅作为内容一致性锚点，不参与关系建立呈现。
RAPPORT_ANCHOR_WEEK = 2

ITEM_BANK_INDEX_FILE = "item_bank_index.json"


class _ItemBankIndexEntrySchema(_FrozenContentSchema):
    week_no: int = Field(ge=2, le=8)
    file: str = Field(min_length=1)

    @field_validator("file")
    @classmethod
    def _plain_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("index file entries must be plain content file names")
        return value


class _ItemBankIndexSchema(_FrozenContentSchema):
    schema_version: Literal["item-bank-index.v1"]
    note: str | None = None
    banks: list[_ItemBankIndexEntrySchema] = Field(min_length=1)

    @field_validator("banks")
    @classmethod
    def _weeks_are_unique(
        cls, value: list[_ItemBankIndexEntrySchema]
    ) -> list[_ItemBankIndexEntrySchema]:
        weeks = [entry.week_no for entry in value]
        if len(set(weeks)) != len(weeks):
            raise ValueError("index weeks must be unique")
        return value


def load_item_bank_index(content_dir: str | Path | None = None) -> dict[int, str]:
    """Return the frozen week→bank-file map; fail-closed on any defect."""
    base = Path(content_dir) if content_dir is not None else CONTENT_DIR
    try:
        data = _load_strict_json_object(
            base / ITEM_BANK_INDEX_FILE, label="题库索引")
        index = _ItemBankIndexSchema.model_validate(data)
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(f"题库索引不可用：{exc}") from exc
    return {entry.week_no: entry.file for entry in index.banks}


def load_item_bank_for_week(
    week_no: int, content_dir: str | Path | None = None
) -> ItemBank:
    """Load the structured bank registered for one training week.

    每个训练周的「冻结训练计划」载体就是该周的题库数据包文件本身：内容团队
    交付某周材料并在索引登记后，该周即可被解析；未登记的周 fail-closed。
    """
    base = Path(content_dir) if content_dir is not None else CONTENT_DIR
    week_files = load_item_bank_index(base)
    file_name = week_files.get(week_no)
    if file_name is None:
        raise TrainingWeekContentUnavailable(
            week_no, f"第{week_no}周材料尚未结构化并校对：题库索引未登记该周")
    bank = load_item_bank(base / file_name)
    declared_week = bank.meta.get("training_week_no")
    if declared_week != week_no or week_no not in bank.supported_training_weeks:
        raise FrozenContentUnavailable(
            f"题库索引第{week_no}周条目与数据包声明周次不一致"
            f"（training_week_no={declared_week}，"
            f"supported={list(bank.supported_training_weeks)}）")
    return bank


class _Week1ReplyEntrySchema(_FrozenContentSchema):
    id: str = Field(min_length=1, max_length=16, pattern=r"^[a-z][a-z0-9]*$")
    text: str = Field(min_length=1, max_length=60)
    when: str = Field(min_length=1)
    invites_more: bool
    group: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _no_slot_may_carry_what_the_elder_said(cls, value: str) -> str:
        if "【" in value or "】" in value:
            raise ValueError("回应句不得带槽位：实例化后含老人自述内容，进不了云 TTS 白名单")
        return value


class _Week1ReplyGroupSchema(_FrozenContentSchema):
    key: str = Field(min_length=1)
    hint: str = Field(min_length=1)


class _Week1ReplyPositionSchema(_FrozenContentSchema):
    section_key: str = Field(min_length=1)
    question_idx: int = Field(ge=0)
    # 这一问最多聊几轮(缺省用全局 RAPPORT_MAX_ROUNDS);属相一问定 1——答一轮就换问、
    # 不追问,免得现编句问出出生年份(2026-09-05 Eric 拍板)。
    max_rounds: int | None = Field(default=None, ge=1)


class _Week1ReplyExclusionSchema(_Week1ReplyPositionSchema):
    why: str = Field(min_length=1)


class _Week1ReplyBankSchema(_FrozenContentSchema):
    bank_version_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    qc_status: str = Field(min_length=1)
    signed_by: str | None
    note: str = Field(min_length=1)
    applies_to: list[_Week1ReplyPositionSchema] = Field(min_length=1)
    excluded: list[_Week1ReplyExclusionSchema]
    fallback: str = Field(min_length=1)
    groups: list[_Week1ReplyGroupSchema] = Field(min_length=1)
    replies: list[_Week1ReplyEntrySchema] = Field(min_length=1)

    @field_validator("replies")
    @classmethod
    def _ids_are_unique(cls, value):
        ids = [row.id for row in value]
        if len(ids) != len(set(ids)):
            raise ValueError("回应句 id 重复")
        return value


def load_week1_reply_bank(path: str | Path) -> dict:
    """第1周开放式回应库。每一句都由人事先定稿，运行时只能从这个闭集里选。"""
    try:
        data = _load_strict_json_object(path, label="第一周回应库")
        _validate_frozen_schema(data, _Week1ReplyBankSchema, label="第一周回应库")
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(str(exc)) from exc
    known = {row["key"] for row in data["groups"]}
    unknown = {row["group"] for row in data["replies"]} - known
    if unknown:
        raise FrozenContentUnavailable(f"回应句引用了未登记的分组：{sorted(unknown)}")
    return data


def load_week1_script(path: str | Path) -> dict:
    try:
        data = _load_strict_json_object(path, label="第一周脚本")
        _validate_frozen_schema(data, _Week1ScriptSchema, label="第一周脚本")
        return data
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(str(exc)) from exc


def load_autopilot_protocol(path: str | Path) -> dict:
    try:
        data = _load_strict_json_object(path, label="自动驾驶协议")
        _validate_frozen_schema(data, _AutopilotProtocolSchema, label="自动驾驶协议")
        _validate_declared_definition_digests(
            data,
            digest_fields=_AUTOPILOT_SELF_DIGEST_FIELDS,
            expected_digest=autopilot_protocol_definition_digest(data),
            label="自动驾驶协议",
        )
        return data
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(str(exc)) from exc


def validate_autopilot_protocol(p: dict) -> list[str]:
    """协议话术完整性闸:缺任何一条固定反馈,自动驾驶就会在该分支哑掉(留空不兜底)。"""
    issues: list[str] = []
    if not isinstance(p, dict):
        return ["自动驾驶协议必须是对象"]
    if p.get("protocol_schema_version") != "autopilot-protocol.v1":
        issues.append("protocol_schema_version 不是 autopilot-protocol.v1")
    version_id = p.get("protocol_version_id")
    if not isinstance(version_id, str) or not version_id.strip():
        issues.append("缺合法 protocol_version_id")
    if p.get("qc_status") not in {"draft", "reviewed", "frozen"}:
        issues.append("qc_status 必须是 draft/reviewed/frozen")
    draft_revision = p.get("draft_revision")
    if not isinstance(draft_revision, str) or not draft_revision.strip():
        issues.append("缺可追溯 draft_revision")
    weeks = p.get("supported_training_weeks")
    if (not isinstance(weeks, list) or not weeks
            or any(
                not isinstance(week, int) or isinstance(week, bool)
                or not 2 <= week <= 8 for week in weeks)
            or len(set(weeks)) != len(weeks)):
        issues.append("supported_training_weeks 必须是 2..8 的非重复整数列表")
    for key, label in (
        ("source_document_sha256", "源 Word 文件摘要"),
        ("source_normalized_text_sha256", "源 Word 规范化文本摘要"),
    ):
        value = p.get(key)
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            issues.append(f"缺合法 {label}（{key}）")
    if p.get("silence_seconds") != 10:
        issues.append("沉默阈值应为 10 秒(与源文档口径一致;改动须课题组拍板)")
    raw_naming = p.get("naming")
    if not isinstance(raw_naming, dict):
        issues.append("naming 必须是对象")
        naming: dict = {}
    else:
        naming = raw_naming
    raw_cue1 = naming.get("success_after_cue1")
    if not isinstance(raw_cue1, dict):
        issues.append("naming.success_after_cue1 必须是对象")
        cue1: dict = {}
    else:
        cue1 = raw_cue1
    for k in ("unknown", "close", "silence"):
        if not cue1.get(k):
            issues.append(f"缺 naming.success_after_cue1.{k}")
    if not naming.get("success_after_cue2"):
        issues.append("缺 naming.success_after_cue2")
    raw_double = p.get("double")
    if not isinstance(raw_double, dict):
        issues.append("double 必须是对象")
        double: dict = {}
    else:
        double = raw_double
    for k in ("namefix_left", "namefix_right"):
        if not double.get(k):
            issues.append(f"缺 double.{k}")
    for key, tpl in (("close", cue1.get("close")), ("silence", cue1.get("silence")),
                     ("cue2", naming.get("success_after_cue2"))):
        if tpl and "【物品名】" not in tpl:
            issues.append(f"success_after_cue1.{key} 模板缺【物品名】槽位")
    packages = p.get("interaction_packages")
    if not isinstance(packages, dict):
        issues.append("缺 interaction_packages（逐周交互数据包登记）")
    else:
        declared_weeks = set(packages)
        supported_weeks = {
            str(week) for week in weeks
            if isinstance(week, int) and not isinstance(week, bool)
        } if isinstance(weeks, list) else set()
        if declared_weeks != supported_weeks:
            issues.append(
                "interaction_packages 登记周次必须与 supported_training_weeks 一致")
        for week_key in sorted(declared_weeks):
            entry = packages.get(week_key)
            entry = entry if isinstance(entry, dict) else {}
            file_name = entry.get("file")
            if (not isinstance(file_name, str)
                    or Path(file_name).name != file_name
                    or not file_name.endswith(".json")):
                issues.append(f"interaction_packages.{week_key} 缺合法数据包文件名")
            sha = entry.get("sha256")
            if not isinstance(sha, str) or _HEX64_RE.fullmatch(sha) is None:
                issues.append(f"interaction_packages.{week_key} 缺合法字节摘要")
    return issues


def load_autopilot_interaction_package(
    week_no: int,
    content_dir: str | Path | None = None,
    protocol: dict | None = None,
) -> dict:
    """按协议声明的字节摘要装载一个训练周的自动交互数据包。

    数据包身份 = 文件字节 sha256，钉在协议 interaction_packages 里（协议
    digest 因而链式覆盖全部话术）。未登记的周 / 摘要不一致一律 fail-closed。
    """
    base = Path(content_dir) if content_dir is not None else CONTENT_DIR
    try:
        if protocol is None:
            protocol = load_autopilot_protocol(base / "autopilot_protocol_v1.json")
        packages = protocol.get("interaction_packages")
        declared = packages.get(str(week_no)) if isinstance(packages, dict) else None
        if not isinstance(declared, dict):
            raise FrozenContentUnavailable(
                f"第{week_no}周自动交互数据包未在协议登记")
        file_name = declared.get("file")
        if not isinstance(file_name, str) or Path(file_name).name != file_name:
            raise FrozenContentUnavailable(
                f"第{week_no}周自动交互数据包文件名不合法")
        path = base / file_name
        raw_bytes = path.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != declared.get("sha256"):
            raise FrozenContentUnavailable(
                f"第{week_no}周自动交互数据包字节摘要与协议钉不一致")
        data = _load_strict_json_object(path, label="自动交互数据包")
        _validate_frozen_schema(
            data, _AutopilotInteractionSchema, label="自动交互数据包")
        if data["week_no"] != week_no:
            raise FrozenContentUnavailable(
                f"自动交互数据包声明周次 {data['week_no']} 与请求周次 {week_no} 不一致")
        return data
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(f"自动交互数据包不可用：{exc}") from exc


def interaction_speech_text(
    speech: dict, bank_item: dict, protocol: dict,
) -> str | None:
    """把一条 speech 引用解析成实际话术文本；解析不出返回 None（fail-closed）。"""
    if not isinstance(speech, dict):
        return None
    source = speech.get("source")
    if source == "verbatim":
        text = speech.get("text")
        return text if isinstance(text, str) and text.strip() else None
    if source == "bank_field":
        field_name = speech.get("field")
        if field_name not in _INTERACTION_BANK_FIELDS:
            return None
        value = bank_item.get(field_name) if isinstance(bank_item, dict) else None
        return value if isinstance(value, str) and value.strip() else None
    if source == "protocol_namefix":
        side = speech.get("side")
        if side not in ("left", "right"):
            return None
        template = (protocol.get("double") or {}).get(f"namefix_{side}")
        word = (bank_item.get(f"{side}_word")
                if isinstance(bank_item, dict) else None)
        if (not isinstance(template, str) or "【物品名】" not in template
                or not isinstance(word, str) or not word.strip()):
            return None
        return template.replace("【物品名】", word)
    return None


def _iter_interaction_speeches(package: dict):
    """Yield (item_id, turn_seq, where, speech_dict) across one package."""
    for item in package.get("items") or []:
        item_id = item.get("item_id")
        for turn in item.get("turns") or []:
            turn_seq = turn.get("turn_seq")
            branches = turn.get("branches")
            for key, branch in (branches or {}).items():
                speech = (branch or {}).get("speech") if isinstance(branch, dict) else None
                if speech is not None:
                    yield item_id, turn_seq, f"branches.{key}", speech
            after = turn.get("after_rerecord")
            if isinstance(after, dict):
                for key, branch in after.items():
                    speech = (branch or {}).get("speech") if isinstance(branch, dict) else None
                    if speech is not None:
                        yield item_id, turn_seq, f"after_rerecord.{key}", speech


# 多概念判定策略：只有这些策略下引擎才可能产出 partial 类别，
# partial 分支的存在性必须与题库冻结 rubric 严格对齐。
_MULTI_CONCEPT_POLICIES = frozenset({
    "all_required_concepts", "all_concept_groups", "hybrid",
})


def validate_autopilot_interaction_package(
    package: dict, bank: ItemBank, protocol: dict,
) -> list[str]:
    """交互数据包语义闸：结构由 schema 保证，这里校验与题库/协议的绑定。

    缺任何一条＝该周自动带练在对应分支哑掉或说错题的话，fail-closed 不兜底。
    """
    issues: list[str] = []
    week_no = package.get("week_no")
    if bank.meta.get("training_week_no") != week_no:
        issues.append("数据包周次与题库声明周次不一致")
    if package.get("parent_item_bank_version_id") != bank.version_id:
        issues.append("parent_item_bank_version_id 与当前题库不一致")
    if (package.get("parent_item_bank_definition_digest")
            != item_bank_definition_digest(bank)):
        issues.append("parent_item_bank_definition_digest 与当前题库定义摘要不一致")
    for key in ("source_document_sha256", "source_normalized_text_sha256"):
        if package.get(key) != bank.meta.get(key):
            issues.append(f"{key} 与题库记录的源文摘要不一致")
    if package.get("silence_seconds") != protocol.get("silence_seconds"):
        issues.append("silence_seconds 与协议不一致")
    bank_items = {
        it.get("item_id"): (it, task_type)
        for rows, task_type in (
            (bank.double_element, "双要素"), (bank.multi_element, "多要素"))
        for it in rows
    }
    package_items = {it.get("item_id"): it for it in package.get("items") or []}
    if set(package_items) != set(bank_items):
        issues.append("数据包条目集合必须恰好等于题库双要素+多要素条目集合")
        return issues
    for item_id, package_item in package_items.items():
        bank_item, task_type = bank_items[item_id]
        if package_item.get("task_type") != task_type:
            issues.append(f"{item_id} task_type 与题库不一致")
            continue
        turns = package_item.get("turns") or []
        if task_type == "双要素":
            expected_roles = ["左命名", "左作用", "右命名", "右作用", "关系识别"]
        else:
            expected_roles = []
            for index, raw in enumerate(bank_item.get("key_elements") or [],
                                        start=1):
                if isinstance(raw, str):
                    expected_roles.append(raw)
                else:
                    expected_roles.append(
                        str(raw.get("label") or raw.get("key") or f"key_{index}"))
        if [turn.get("response_role") for turn in turns] != expected_roles:
            issues.append(f"{item_id} 环节角色序列与题库计划位置不一致")
            continue
        for turn in turns:
            role = turn.get("response_role")
            where = f"{item_id}#{turn.get('turn_seq')}"
            branches = turn.get("branches") or {}
            if task_type == "多要素":
                rubric = (bank_item.get("operational_rubrics") or {}).get(role)
                policy = (rubric or {}).get("decision_policy")
                if (policy in _MULTI_CONCEPT_POLICIES) != ("partial" in branches):
                    issues.append(
                        f"{where} partial 分支存在性与 rubric 策略 {policy} 不符")
            elif "partial" in branches:
                issues.append(f"{where} 双要素环节不得有 partial 分支")
    for item_id, turn_seq, where, speech in _iter_interaction_speeches(package):
        bank_item, _ = bank_items.get(item_id, ({}, None))
        if interaction_speech_text(speech, bank_item, protocol) is None:
            issues.append(f"{item_id}#{turn_seq} {where} 话术引用解析不出文本")
    return issues


_DOUBLE_OPEN_OPERATIONAL_ROLES = ("左作用", "右作用", "关系识别")
# all_concept_groups：源文完成条件形如「同时抓取到“甲/乙”与“丙”（或丁）」——
# 每组内任取其一、组间求与。拍平成 all_required_concepts 会把标准正确回答
# （说了甲和丙）判失败，2026-08-19 保真度审计实测踩到（wk6 院子/wk8 家里等）。
_RUBRIC_POLICIES = {
    "any_acceptable_expression",
    "all_required_concepts",
    "all_concept_groups",
    "hybrid",
}


def _planned_open_roles(item: dict, task_type: str) -> tuple[str, ...]:
    if task_type == "双要素":
        return _DOUBLE_OPEN_OPERATIONAL_ROLES
    if task_type != "多要素":
        return ()
    roles: list[str] = []
    for index, raw in enumerate(item.get("key_elements") or [], start=1):
        if isinstance(raw, str):
            roles.append(raw)
        elif isinstance(raw, dict):
            roles.append(str(raw.get("label") or raw.get("target")
                             or raw.get("key") or raw.get("id") or f"key_{index}"))
    return tuple(roles)


def unsupported_operational_rubrics(bank: ItemBank) -> tuple[str, ...]:
    """列出不能安全自动判定的开放回答环节。"""
    unsupported: list[str] = []
    typed_items = [
        *((item, "双要素") for item in bank.double_element),
        *((item, "多要素") for item in bank.multi_element),
    ]
    for item, task_type in typed_items:
        item_id = str(item.get("item_id") or "?")
        rubrics = item.get("operational_rubrics")
        for role in _planned_open_roles(item, task_type):
            rubric = rubrics.get(role) if isinstance(rubrics, dict) else None
            expressions = rubric.get("acceptable_expressions") if isinstance(rubric, dict) else None
            concepts = rubric.get("required_concepts") if isinstance(rubric, dict) else None
            groups = rubric.get("required_concept_groups") if isinstance(rubric, dict) else None
            policy = rubric.get("decision_policy") if isinstance(rubric, dict) else None
            cues = rubric.get("cues") if isinstance(rubric, dict) else None
            if not (
                isinstance(rubric, dict)
                and isinstance(rubric.get("rubric_version"), str)
                and rubric["rubric_version"].strip()
                and policy in _RUBRIC_POLICIES
                and (
                    isinstance(expressions, list) and any(
                        isinstance(value, str) and value.strip() for value in expressions)
                    or isinstance(concepts, list) and any(
                        isinstance(value, str) and value.strip() for value in concepts)
                    or isinstance(groups, list) and groups and all(
                        isinstance(group, list) and group and all(
                            isinstance(value, str) and value.strip()
                            for value in group)
                        for group in groups)
                )
                and isinstance(cues, dict)
                and all(
                    isinstance(cues.get(level), str) and cues[level].strip()
                    for level in ("1", "2")
                )
                and isinstance(rubric.get("tell_answer"), str)
                and rubric["tell_answer"].strip()
            ):
                unsupported.append(f"{item_id}:{role}")
    return tuple(unsupported)


def operational_rubric_for(
        bank: ItemBank, item_id: str, response_role: str) -> dict | None:
    """只返回已通过完整结构门禁的版本化开放回答 rubric。"""
    typed_items = (
        *((item, "双要素") for item in bank.double_element),
        *((item, "多要素") for item in bank.multi_element),
    )
    for item, task_type in typed_items:
        if item.get("item_id") != item_id:
            continue
        # A syntactically valid rubric under an invented key must never create a
        # new executable response role.  Runtime positions come only from the
        # frozen plan, so lookup is restricted to those exact open-answer roles.
        if response_role not in _planned_open_roles(item, task_type):
            return None
        position = f"{item_id}:{response_role}"
        if position in unsupported_operational_rubrics(bank):
            return None
        rubrics = item.get("operational_rubrics")
        rubric = rubrics.get(response_role) if isinstance(rubrics, dict) else None
        return dict(rubric) if isinstance(rubric, dict) else None
    return None


def validate_item_bank(bank: ItemBank) -> dict[str, list[str]]:
    """两档校验，冻结前的自动校对闸：
      errors   = 缺陷/勘误（目标词↔告知话术↔标题 对不上、重复 id）——冻结前必须清零；
      warnings = 待补全（缺线索/成功/告知话术文本，如缺引号导致抽不出的线索）——内容组填。
    返回 {"errors": [...], "warnings": [...]}。
    """
    errors: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for path in _blank_string_paths(item_bank_definition(bank)):
        location = ".".join(path) or "root"
        errors.append(f"题库含纯空白字符串：{location}")

    if bank.qc_status not in {"draft", "reviewed", "frozen"}:
        errors.append(f"题库 qc_status 非法：{bank.qc_status!r}")
    if not bank.supported_training_weeks:
        errors.append("题库缺 supported_training_weeks，运行时不得猜测覆盖周次")
    if bank.qc_status != "frozen":
        warnings.append(f"题库尚未冻结（qc_status={bank.qc_status}），仅可用于开发/演示")

    # A research-frozen bank must retain independently verifiable source
    # provenance.  Drafts may still be assembled incrementally, but their
    # missing provenance is made visible rather than silently accepted.
    for key, label in (
        ("source_document_sha256", "源 Word 文件摘要"),
        ("source_normalized_text_sha256", "源 Word 规范化文本摘要"),
    ):
        value = bank.meta.get(key)
        valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        if not valid:
            target = errors if bank.qc_status == "frozen" else warnings
            target.append(f"题库缺合法 {label}（{key}）")
    if not isinstance(bank.meta.get("draft_revision"), str) \
            or not str(bank.meta.get("draft_revision")).strip():
        target = errors if bank.qc_status == "frozen" else warnings
        target.append("题库缺可追溯 draft_revision")

    source_position_count = bank.meta.get("source_protocol_position_count")
    source_unstructured = bank.meta.get("source_unstructured_positions")
    if (not isinstance(source_position_count, int)
            or isinstance(source_position_count, bool)
            or source_position_count < 0):
        errors.append("题库缺合法 source_protocol_position_count")
    if not isinstance(source_unstructured, list):
        errors.append("题库缺 source_unstructured_positions 列表")
        source_unstructured = []
    unstructured_keys: set[str] = set()
    for row in source_unstructured:
        key = row.get("source_position_key") if isinstance(row, dict) else None
        role = row.get("response_role") if isinstance(row, dict) else None
        paragraphs = row.get("source_paragraphs") if isinstance(row, dict) else None
        status = row.get("status") if isinstance(row, dict) else None
        valid_paragraphs = (
            isinstance(paragraphs, list)
            and len(paragraphs) == 2
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in paragraphs
            )
            and paragraphs[0] <= paragraphs[1]
        )
        if (not isinstance(key, str) or not key.strip()
                or not isinstance(role, str) or not role.strip()
                or not valid_paragraphs
                or status not in {
                    "awaiting_content_decision", "awaiting_pi_rubric",
                }):
            errors.append("source_unstructured_positions 含非法题位记录")
            continue
        if key in unstructured_keys:
            errors.append(f"source_unstructured_positions 重复：{key}")
        unstructured_keys.add(key)
    structured_position_count = (
        len(bank.single_element)
        + 5 * len(bank.double_element)
        + sum(len(item.get("key_elements") or []) for item in bank.multi_element)
    )
    if (isinstance(source_position_count, int)
            and not isinstance(source_position_count, bool)
            and source_position_count
            != structured_position_count + len(source_unstructured)):
        errors.append(
            "source_protocol_position_count 与已结构化/未结构化题位总数不一致"
        )
    for key in sorted(unstructured_keys):
        warnings.append(f"{key}: 源协议题位尚未结构化，禁止自动执行")

    for it in bank.single_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        tw = it.get("target_word")
        if not tw:
            errors.append(f"{iid}: 缺 target_word")
            continue
        tell = it.get("tell_answer")
        if tell and tw not in tell:
            # 四者一致核心：告知话术点到别的词 = 勘误（斧子+树/螺丝刀→衬衫 那类）
            errors.append(f"{iid}: 告知话术未含目标词“{tw}”（勘误）：{tell[:24]}…")
        elif not tell:
            warnings.append(f"{iid}: 缺 tell_answer")
        if not it.get("success_line"):
            warnings.append(f"{iid}: 缺 success_line")
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        if not it.get("acceptable_expressions"):
            warnings.append(f"{iid}: acceptable_expressions 尚未由内容组确认")
        if not it.get("difficulty_level"):
            warnings.append(f"{iid}: difficulty_level 尚未标注")
        related = it.get("related_but_inaccurate") or []
        malformed = [
            str(value) for value in related
            if any(marker in str(value) for marker in ("“", "”"))
        ]
        if malformed:
            errors.append(
                f"{iid}: related_but_inaccurate 含解析引号残片：{malformed!r}")
        cues = it.get("cues", {})
        for lv in ("1", "2"):
            if not (cues.get(lv) or {}).get("text"):
                warnings.append(f"{iid}: 缺第{lv}级线索文本")
        cue1 = cues.get("1") if isinstance(cues, dict) else None
        variants = cue1.get("variants") if isinstance(cue1, dict) else None
        if not isinstance(variants, dict):
            warnings.append(f"{iid}: 缺第1级 unknown/close/silence 源路径话术")
        else:
            for branch in ("unknown", "close", "silence"):
                variant = variants.get(branch)
                text = variant.get("text") if isinstance(variant, dict) else None
                source_index = (
                    variant.get("source_paragraph_index")
                    if isinstance(variant, dict) else None
                )
                if not isinstance(text, str) or not text.strip():
                    warnings.append(f"{iid}: 缺第1级 {branch} 路径话术")
                if (not isinstance(source_index, int)
                        or isinstance(source_index, bool) or source_index < 0):
                    warnings.append(f"{iid}: 第1级 {branch} 缺合法源段落定位")
            unknown = variants.get("unknown")
            unknown_text = (
                unknown.get("text") if isinstance(unknown, dict) else None
            )
            if isinstance(unknown_text, str) and cue1.get("text") != unknown_text:
                errors.append(f"{iid}: legacy cue1.text 与 unknown 源路径不一致")

    for it in bank.double_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        title = it.get("pair_title", "")
        parts = [p for p in title.split("+") if p]
        for side in ("left_word", "right_word"):
            w = it.get(side)
            if not w:
                warnings.append(f"{iid}: 缺 {side}")
            elif w not in parts:
                errors.append(f"{iid}: {side}“{w}”不在标题“{title}”中（勘误）")
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        for cue in ("left_function_cue", "right_function_cue", "relation_cue"):
            if not it.get(cue):
                warnings.append(f"{iid}: 缺 {cue}")
        rubrics = it.get("operational_rubrics")
        if rubrics is not None and not isinstance(rubrics, dict):
            errors.append(f"{iid}: operational_rubrics 必须是按回答角色索引的对象")
        elif isinstance(rubrics, dict):
            planned_roles = set(_planned_open_roles(it, "双要素"))
            for role in rubrics:
                if not isinstance(role, str) or role not in planned_roles:
                    errors.append(
                        f"{iid}: operational_rubrics 含非冻结计划回答角色：{role!r}")

    for it in bank.multi_element:
        iid = it.get("item_id", "?")
        if iid in seen_ids:
            errors.append(f"{iid}: item_id 重复")
        seen_ids.add(iid)
        if not it.get("image_id"):
            warnings.append(f"{iid}: 缺 image_id")
        if not it.get("key_elements"):
            errors.append(f"{iid}: 多要素题缺 key_elements")
        rubrics = it.get("operational_rubrics")
        if rubrics is not None and not isinstance(rubrics, dict):
            errors.append(f"{iid}: operational_rubrics 必须是按回答角色索引的对象")
        elif isinstance(rubrics, dict):
            planned_roles = set(_planned_open_roles(it, "多要素"))
            for role in rubrics:
                if not isinstance(role, str) or role not in planned_roles:
                    errors.append(
                        f"{iid}: operational_rubrics 含非冻结计划回答角色：{role!r}")

    for position in unsupported_operational_rubrics(bank):
        warnings.append(
            f"{position}: 缺版本化 operational_rubric，禁止以‘有回答’自动推进"
        )

    if not bank.multi_element:
        warnings.append("当前结构化题库未包含多要素任务，尚未达到蓝图 M0 的完整题型闭环")

    return {"errors": errors, "warnings": warnings}


def content_readiness(bank: ItemBank) -> dict[str, object]:
    """面向 API/UI 的明确能力声明，避免把“能加载”误写成“可入组”。"""
    validation = validate_item_bank(bank)
    unsupported_rubrics = unsupported_operational_rubrics(bank)
    source_unstructured = bank.meta.get("source_unstructured_positions")
    unstructured_count = (
        len(source_unstructured) if isinstance(source_unstructured, list) else 0
    )
    return {
        "qc_status": bank.qc_status,
        "supported_training_weeks": list(bank.supported_training_weeks),
        "operational_autopilot_ready": (
            not unsupported_rubrics and unstructured_count == 0
        ),
        "unsupported_operational_rubrics": list(unsupported_rubrics),
        "source_protocol_position_count": bank.meta.get(
            "source_protocol_position_count"),
        "source_unstructured_position_count": unstructured_count,
        "source_unstructured_positions": deepcopy(source_unstructured)
        if isinstance(source_unstructured, list) else [],
        "ready_for_research": (
            bank.qc_status == "frozen"
            and not validation["errors"]
            and not validation["warnings"]
            and bool(bank.multi_element)
            and not unsupported_rubrics
            and unstructured_count == 0
        ),
        "errors": validation["errors"],
        "warnings": validation["warnings"],
    }


def validate_week1_script(script: dict) -> list[str]:
    issues: list[str] = []
    if not isinstance(script, dict):
        return ["第一周脚本必须是对象"]
    zodiac = script.get("zodiac_closed_list")
    if not isinstance(zodiac, list):
        issues.append("zodiac_closed_list 必须是数组")
    elif len(zodiac) != 12:
        issues.append(f"属相封闭词表应为 12 个，实为 {len(zodiac)}")
    elif any(not isinstance(value, str) or not value.strip() for value in zodiac):
        issues.append("zodiac_closed_list 必须只含非空字符串")
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections:
        issues.append("sections 必须是非空数组")
    else:
        section_keys: list[str] = []
        for section in sections:
            key = section.get("key") if isinstance(section, dict) else None
            if not isinstance(key, str) or not key.strip():
                issues.append("sections 含缺失合法 key 的节")
                continue
            section_keys.append(key)
        if len(section_keys) != len(set(section_keys)):
            issues.append("sections.key 必须全局唯一")
    if script.get("silence_seconds") != 10:
        issues.append("沉默触发阈值应为 10 秒（与第2–8周口径一致）")
    return issues


# 老人端前端硬编码的固定话术（PatientStage 问句 / RapportStage 兜底 / 试听句）。
# 改前端这些字符串时必须同步这里，否则云 TTS 拒合成、自动落回本地引擎。
FIXED_TTS_LINES: tuple[str, ...] = (
    "您好",
    "请看这张图片，这是什么？",
    "它是做什么用的呢？",
    "它们之间有什么关系呢？",
    "我们一起聊聊天，好吗？",
)


_SLOT_RE = re.compile(r"【[^】]*】")
_ZODIAC_SLOT = "【老人所说的属相】"


def _expand_slots(line: str, zodiac: list[str]):
    """槽位模板处理：属相是 12 生肖闭集 → 展开成具体句进白名单；
    开放槽位（兴趣/活动/称呼…）实例化后含老人自述内容=患者数据 →
    模板与实例一律不进白名单（云端拒合成，由本地引擎朗读）。"""
    if "【" not in line:
        yield line
        return
    if _SLOT_RE.findall(line) == [_ZODIAC_SLOT]:
        for z in zodiac:
            yield line.replace(_ZODIAC_SLOT, z)


def tts_allowlist(bank: ItemBank, week1_script: dict | None = None,
                  autopilot_protocol: dict | None = None,
                  interaction_package: dict | None = None,
                  week1_reply_bank: dict | None = None) -> frozenset[str]:
    """云 TTS 允许合成的全部文本（闭集）。

    红线：发往云端的文本只能来自题库/脚本/固定 UI 话术，永不携带患者字段。
    不在此集合的文本，云引擎一律拒合成（fail-closed，落回本地引擎/系统语音）。
    自动驾驶协议的【物品名】模板用题库词表（闭集）展开成具体句。
    """
    lines: set[str] = set(FIXED_TTS_LINES)
    for it in bank.single_element:
        for t in (it.get("initial_prompt"), it.get("success_line"), it.get("tell_answer")):
            if t:
                lines.add(t)
        for cue in (it.get("cues") or {}).values():
            t = (cue or {}).get("text")
            if t:
                lines.add(t)
            variants = (cue or {}).get("variants")
            if isinstance(variants, dict):
                for variant in variants.values():
                    variant_text = (
                        variant.get("text") if isinstance(variant, dict) else None
                    )
                    if variant_text:
                        lines.add(variant_text)
    for it in bank.double_element:
        for k in ("left_function_cue", "right_function_cue", "relation_cue"):
            t = it.get(k)
            if t:
                lines.add(t)
    if week1_script:
        zodiac = list(week1_script.get("zodiac_closed_list") or [])
        t = week1_script.get("generic_fallback_line")
        if t:
            lines.add(t)
        for sec in week1_script.get("sections") or []:
            if sec.get("speaker") != "机器人":
                continue
            if sec.get("line"):
                lines.update(_expand_slots(sec["line"], zodiac))
            for q in sec.get("questions") or []:
                for t in (q.get("ask"), q.get("success")):
                    if t:
                        lines.update(_expand_slots(t, zodiac))
        for slot in (week1_script.get("slots") or {}).values():
            t = (slot or {}).get("fallback_line")
            if t:
                lines.add(t)
    if week1_reply_bank:
        t = week1_reply_bank.get("fallback")
        if t:
            lines.add(t)
        for row in week1_reply_bank.get("replies") or []:
            t = (row or {}).get("text")
            if t:
                lines.add(t)
    if autopilot_protocol:
        words = [it.get("target_word") for it in bank.single_element]
        words += [it.get(k) for it in bank.double_element for k in ("left_word", "right_word")]
        words = [w for w in words if w]
        naming = autopilot_protocol.get("naming") or {}
        cue1 = naming.get("success_after_cue1") or {}
        templates = [cue1.get("unknown"), cue1.get("close"), cue1.get("silence"),
                     naming.get("success_after_cue2"),
                     (autopilot_protocol.get("double") or {}).get("namefix_left"),
                     (autopilot_protocol.get("double") or {}).get("namefix_right")]
        for tpl in templates:
            if not tpl:
                continue
            if "【物品名】" in tpl:
                lines.update(tpl.replace("【物品名】", w) for w in words)
            else:
                lines.add(tpl)
    if interaction_package:
        by_id = {it.get("item_id"): it
                 for rows in (bank.double_element, bank.multi_element)
                 for it in rows}
        for item in interaction_package.get("items") or []:
            for turn in item.get("turns") or []:
                t = (turn.get("question") or {}).get("text")
                if t:
                    lines.add(t)
        for item_id, _turn_seq, _where, speech in _iter_interaction_speeches(
                interaction_package):
            t = interaction_speech_text(
                speech, by_id.get(item_id) or {}, autopilot_protocol or {})
            if t:
                lines.add(t)
    return frozenset(s.strip() for s in lines if s and s.strip())


# 内容目录默认位置
CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
