"""Production loader for formal assessment definition bundles (收据 150 S1).

Bundles are pure-data JSON packages under ``content/assessment_definitions/``.
Every digest the runtime trusts is recomputed here from the exact bytes this
loader interprets — a claimed digest can never stand in for verified content.
Copyrighted item words stay on the server disk for the training-isolation
check only; the wire contract continues to carry opaque item keys and digests.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .assessment_contract import (
    AssessmentDefinitionBundle,
    AssessmentDefinitionSpec,
    AssessmentItemSpec,
)
from .assessment_definitions import (
    AssessmentScore,
    NormalizedAssessmentResponse,
    RegisteredAssessmentBundle,
    RegisteredAssessmentDefinition,
    bundle_digest,
    definition_digest,
    digest_for,
    validate_registered_bundle,
)
from .content import FrozenContentUnavailable

ASSESSMENT_BUNDLE_DIR = "assessment_definitions"
ASSESSMENT_BUNDLE_INDEX_FILE = "bundle_index.json"
BUNDLE_PACKAGE_SCHEMA_VERSION = "assessment-definition-bundle-package.v1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ResponseSchema(_FrozenModel):
    kind: Literal["bounded_numeric"]
    minimum: float = Field(allow_inf_nan=False)
    maximum: float = Field(allow_inf_nan=False)
    allowed_values: tuple[float, ...] | None = None

    @model_validator(mode="after")
    def _coherent(self):
        if self.minimum >= self.maximum:
            raise ValueError("response minimum must be lower than maximum")
        if self.allowed_values is not None:
            if not self.allowed_values:
                raise ValueError("allowed_values must be absent or non-empty")
            if any(v < self.minimum or v > self.maximum
                   for v in self.allowed_values):
                raise ValueError("allowed_values must stay inside the bounds")
            if len(set(self.allowed_values)) != len(self.allowed_values):
                raise ValueError("allowed_values must be unique")
        return self


class _MissingnessRule(_FrozenModel):
    kind: Literal["all_items_required"]


class _StoppingRule(_FrozenModel):
    kind: Literal["none"]


class _ScoringSpec(_FrozenModel):
    algorithm_id: str = Field(min_length=1, max_length=200)
    algorithm_version: str = Field(min_length=1, max_length=128)
    kind: Literal["item_sum"]
    rounding: Literal["integer_exact", "half_up"]


class _PermissionSpec(_FrozenModel):
    automatic_scoring_permitted: bool
    item_response_storage_permitted: bool
    result_storage_permitted: bool
    result_export_permitted: bool


class _ItemSpec(_FrozenModel):
    item_key: str = Field(min_length=1, max_length=200)
    # 量表一(命名测验)逐题词:只用于服务器侧「与训练词表不相交」检查,
    # 永不进入 wire 契约或患者端;功能沟通类条目无词可留空。
    word: str | None = Field(default=None, min_length=1, max_length=64)


class _DefinitionPackage(_FrozenModel):
    definition_id: str = Field(min_length=1, max_length=200)
    category_key: Literal[
        "untrained_standardized_naming", "functional_communication"]
    instrument_id: str = Field(min_length=1, max_length=200)
    instrument_version: str = Field(min_length=1, max_length=128)
    administration_protocol: dict
    response_schema: _ResponseSchema
    missingness_rule: _MissingnessRule
    stopping_rule: _StoppingRule
    scoring: _ScoringSpec
    score_min: float = Field(allow_inf_nan=False)
    score_max: float = Field(allow_inf_nan=False)
    score_direction: Literal["higher_is_better", "lower_is_better"]
    permissions: _PermissionSpec
    items: tuple[_ItemSpec, ...] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def _unique_items(cls, value: tuple[_ItemSpec, ...]):
        keys = [item.item_key for item in value]
        if len(set(keys)) != len(keys):
            raise ValueError("item keys must be unique")
        return value

    @field_validator("administration_protocol")
    @classmethod
    def _protocol_not_empty(cls, value: dict):
        if not value:
            raise ValueError("administration_protocol must not be empty")
        return value


class _BundlePackage(_FrozenModel):
    schema_version: Literal["assessment-definition-bundle-package.v1"]
    bundle_id: str = Field(min_length=1, max_length=200)
    formal_research_approved: bool
    definitions: tuple[_DefinitionPackage, ...] = Field(
        min_length=2, max_length=2)
    # 可选:PI 交接件里声明的期望摘要;声明了就必须与重算值一致(跨手递交防篡改)。
    expected_digests: dict[str, str] | None = None

    @field_validator("definitions")
    @classmethod
    def _exact_categories(cls, value: tuple[_DefinitionPackage, ...]):
        keys = {row.category_key for row in value}
        if keys != {"untrained_standardized_naming", "functional_communication"}:
            raise ValueError("bundle must contain the two frozen categories")
        return value


class _BundleIndexEntry(_FrozenModel):
    bundle_id: str = Field(min_length=1, max_length=200)
    file: str = Field(min_length=1)

    @field_validator("file")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value.startswith("."):
            raise ValueError("bundle files must be plain file names")
        return value


class _BundleIndex(_FrozenModel):
    schema_version: Literal["assessment-bundle-index.v1"]
    note: str | None = None
    active_bundle_id: str = Field(min_length=1, max_length=200)
    bundles: tuple[_BundleIndexEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent(self):
        ids = [entry.bundle_id for entry in self.bundles]
        if len(set(ids)) != len(ids):
            raise ValueError("bundle ids must be unique")
        if self.active_bundle_id not in ids:
            raise ValueError("active_bundle_id must be listed in bundles")
        return self


def _canonical_digest_payload(definition: _DefinitionPackage) -> dict[str, str]:
    """Recompute every rule/artifact digest from the exact interpreted bytes."""
    item_keys = sorted(item.item_key for item in definition.items)
    return {
        "item_set_digest": digest_for({
            "schema": "assessment-item-set.v1", "item_keys": item_keys}),
        "administration_protocol_digest": digest_for({
            "schema": "assessment-administration.v1",
            "payload": definition.administration_protocol}),
        "response_schema_digest": digest_for({
            "schema": "assessment-response-schema.v1",
            "payload": definition.response_schema.model_dump()}),
        "result_schema_digest": digest_for({
            "schema": "assessment-result-schema.v1",
            "kind": definition.scoring.kind,
            "field": "total_score"}),
        "missingness_rule_digest": digest_for({
            "schema": "assessment-missingness.v1",
            "payload": definition.missingness_rule.model_dump()}),
        "stopping_rule_digest": digest_for({
            "schema": "assessment-stopping.v1",
            "payload": definition.stopping_rule.model_dump()}),
        "scoring_algorithm_digest": digest_for({
            "schema": "assessment-scoring.v1",
            "algorithm_id": definition.scoring.algorithm_id,
            "algorithm_version": definition.scoring.algorithm_version,
            "payload": definition.scoring.model_dump(),
            "item_keys": item_keys}),
    }


def _compile_definition(
        definition: _DefinitionPackage) -> RegisteredAssessmentDefinition:
    digests = _canonical_digest_payload(definition)
    provisional = AssessmentDefinitionSpec(
        definition_id=definition.definition_id,
        category_key=definition.category_key,
        instrument_id=definition.instrument_id,
        instrument_version=definition.instrument_version,
        definition_digest="sha256:" + "0" * 64,
        item_set_digest=digests["item_set_digest"],
        administration_protocol_digest=digests[
            "administration_protocol_digest"],
        response_schema_digest=digests["response_schema_digest"],
        result_schema_digest=digests["result_schema_digest"],
        missingness_rule_digest=digests["missingness_rule_digest"],
        stopping_rule_digest=digests["stopping_rule_digest"],
        scoring_algorithm_id=definition.scoring.algorithm_id,
        scoring_algorithm_version=definition.scoring.algorithm_version,
        scoring_algorithm_digest=digests["scoring_algorithm_digest"],
        score_min=definition.score_min,
        score_max=definition.score_max,
        score_direction=definition.score_direction,
        score_rounding_rule=definition.scoring.rounding,
        automatic_scoring_permitted=(
            definition.permissions.automatic_scoring_permitted),
        item_response_storage_permitted=(
            definition.permissions.item_response_storage_permitted),
        result_storage_permitted=(
            definition.permissions.result_storage_permitted),
        result_export_permitted=(
            definition.permissions.result_export_permitted),
        items=tuple(AssessmentItemSpec(item_key=item.item_key)
                    for item in definition.items),
    )
    snapshot = provisional.model_copy(update={
        "definition_digest": definition_digest(provisional)})

    allowed = {item.item_key for item in definition.items}
    schema = definition.response_schema
    allowed_values = (set(schema.allowed_values)
                      if schema.allowed_values is not None else None)
    rounding = definition.scoring.rounding
    score_min = definition.score_min
    score_max = definition.score_max

    def validate_response(
            item_key: str, payload: dict[str, object],
    ) -> NormalizedAssessmentResponse:
        if item_key not in allowed:
            raise _definition_error(
                "assessment_item_unknown",
                "item key is not in the frozen definition")
        if "value" not in payload:
            raise _definition_error(
                "assessment_response_value_required",
                "declarative response requires value")
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _definition_error(
                "assessment_response_invalid",
                "declarative response must be numeric")
        numeric = float(value)
        if (numeric != numeric or numeric in (float("inf"), float("-inf"))
                or numeric < schema.minimum or numeric > schema.maximum
                or (allowed_values is not None
                    and numeric not in allowed_values)):
            raise _definition_error(
                "assessment_response_invalid",
                "declarative response is outside the frozen value set")
        normalized: dict[str, object] = {"value": numeric}
        artifact = payload.get("authorized_artifact_digest")
        if artifact is not None:
            normalized["authorized_artifact_digest"] = artifact
        return NormalizedAssessmentResponse(normalized)

    def score_responses(responses) -> AssessmentScore:
        missing = sorted(allowed - set(responses))
        if missing:
            raise _definition_error(
                "assessment_responses_incomplete",
                "all frozen items require a latest response")
        total = sum(float(responses[key]["value"]) for key in sorted(allowed))
        if rounding == "half_up":
            total = float(int(total + 0.5) if total >= 0 else -int(-total + 0.5))
        if total < score_min or total > score_max:
            raise _definition_error(
                "assessment_scoring_invalid",
                "declarative total is outside the frozen score range")
        return AssessmentScore(
            score=total,
            result={"total_score": total},
            answered_item_count=len(allowed),
            missing_item_count=0,
        )

    return RegisteredAssessmentDefinition(
        snapshot, validate_response, score_responses)


def _definition_error(code: str, message: str):
    from .assessment_definitions import AssessmentDefinitionError

    return AssessmentDefinitionError(code, message)


def compile_bundle_package(data: dict) -> RegisteredAssessmentBundle:
    """Compile one package into a registry bundle; every digest recomputed."""
    package = _BundlePackage.model_validate(data)
    compiled = tuple(
        _compile_definition(row)
        for row in sorted(package.definitions, key=lambda r: r.category_key)
    )
    snapshots = tuple(row.snapshot for row in compiled)
    snapshot = AssessmentDefinitionBundle(
        bundle_id=package.bundle_id,
        bundle_digest=bundle_digest(
            package.bundle_id,
            (snapshots[0], snapshots[1]),
            formal_research_approved=package.formal_research_approved,
        ),
        definitions=snapshots,
        formal_research_approved=package.formal_research_approved,
    )
    bundle = RegisteredAssessmentBundle(
        snapshot=snapshot,
        definitions={row.snapshot.category_key: row for row in compiled},
    )
    validate_registered_bundle(bundle)
    if package.expected_digests:
        computed = {
            "bundle_digest": snapshot.bundle_digest,
            **{
                f"{row.snapshot.category_key}.definition_digest":
                    row.snapshot.definition_digest
                for row in compiled
            },
        }
        for field, expected in package.expected_digests.items():
            if computed.get(field) != expected:
                raise FrozenContentUnavailable(
                    f"量表定义包声明摘要与重算值不一致:{field}")
    return bundle


def naming_words(data: dict) -> frozenset[str]:
    """量表一逐题词表(仅服务器侧隔离检查用);包结构非法时 fail-closed。"""
    package = _BundlePackage.model_validate(data)
    words: set[str] = set()
    for definition in package.definitions:
        if definition.category_key != "untrained_standardized_naming":
            continue
        for item in definition.items:
            if item.word:
                words.add(item.word.strip())
    return frozenset(words)


def assert_training_isolation(
    raw_packages: tuple[dict, ...], content_dir: str | Path,
) -> None:
    """量表一(未训练词命名测验)词表必须与全部训练周词表不相交(采集单 #11)。

    「未训练词」是该结局的效度前提:训练过的词混进测验=结局污染。训练索引
    坏档时同样拒绝安装量表包(fail-closed,不在盲区里放行)。
    """
    from . import content as content_module

    week_files = content_module.load_item_bank_index(content_dir)
    training_words: set[str] = set()
    for week in sorted(week_files):
        bank = content_module.load_item_bank_for_week(week, content_dir)
        for item in (*bank.single_element, *bank.double_element):
            for word in (item.get("target_word"), item.get("left_word"),
                         item.get("right_word")):
                if word:
                    training_words.add(str(word).strip())
            for word in item.get("acceptable_expressions") or []:
                if word:
                    training_words.add(str(word).strip())
    for data in raw_packages:
        overlap = naming_words(data) & training_words
        if overlap:
            raise FrozenContentUnavailable(
                f"量表一词表与训练词表相交({len(overlap)} 词),违反未训练词前提")


def load_bundle_packages(
    content_dir: str | Path,
) -> tuple[tuple[RegisteredAssessmentBundle, ...], str, tuple[dict, ...]]:
    """Load every indexed bundle package; returns (bundles, active_id, raw)."""
    base = Path(content_dir) / ASSESSMENT_BUNDLE_DIR
    index_path = base / ASSESSMENT_BUNDLE_INDEX_FILE
    try:
        index = _BundleIndex.model_validate(
            json.loads(index_path.read_text(encoding="utf-8")))
        bundles: list[RegisteredAssessmentBundle] = []
        raw_packages: list[dict] = []
        for entry in index.bundles:
            data = json.loads(
                (base / entry.file).read_text(encoding="utf-8"))
            bundle = compile_bundle_package(data)
            if bundle.snapshot.bundle_id != entry.bundle_id:
                raise ValueError(
                    f"索引条目 {entry.bundle_id} 与包内 bundle_id 不一致")
            bundles.append(bundle)
            raw_packages.append(data)
    except FrozenContentUnavailable:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise FrozenContentUnavailable(f"量表定义包不可用:{exc}") from exc
    return tuple(bundles), index.active_bundle_id, tuple(raw_packages)
