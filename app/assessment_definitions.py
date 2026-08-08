"""Fail-closed registry for versioned formal assessment definitions.

Production starts with no registered bundle.  A registry entry contains an
immutable, content-addressed snapshot plus definition-owned validation and
scoring callbacks.  This keeps the platform generic: a future licensed
instrument is admitted only by its own frozen adapter, never by coercing its
responses into a built-in free-form score field.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
import threading
from types import MappingProxyType
from typing import Callable, Iterator, Mapping, NoReturn

from .assessment_contract import (
    AssessmentCategoryKey,
    AssessmentDefinitionBundle,
    AssessmentDefinitionSpec,
    AssessmentItemSpec,
)


class AssessmentDefinitionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> NoReturn:
    raise AssessmentDefinitionError(code, message)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_for(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True)
class NormalizedAssessmentResponse:
    """Canonical definition-approved response persisted by the service."""

    payload: dict[str, object]


@dataclass(frozen=True)
class AssessmentScore:
    """Server-derived result and completeness evidence from one frozen scorer."""

    score: float
    result: dict[str, object]
    answered_item_count: int
    missing_item_count: int
    stopped_early: bool = False
    stopping_reason_code: str | None = None


ResponseValidator = Callable[[str, dict[str, object]], NormalizedAssessmentResponse]
ScoreFunction = Callable[[Mapping[str, dict[str, object]]], AssessmentScore]


@dataclass(frozen=True)
class RegisteredAssessmentDefinition:
    snapshot: AssessmentDefinitionSpec
    validate_response: ResponseValidator
    score_responses: ScoreFunction


@dataclass(frozen=True)
class RegisteredAssessmentBundle:
    snapshot: AssessmentDefinitionBundle
    definitions: Mapping[AssessmentCategoryKey, RegisteredAssessmentDefinition]


def _definition_projection(snapshot: AssessmentDefinitionSpec) -> dict[str, object]:
    return {
        "schema": "assessment-definition.v1",
        "definition_id": snapshot.definition_id,
        "category_key": snapshot.category_key,
        "instrument_id": snapshot.instrument_id,
        "instrument_version": snapshot.instrument_version,
        "item_set_digest": snapshot.item_set_digest,
        "administration_protocol_digest": snapshot.administration_protocol_digest,
        "response_schema_digest": snapshot.response_schema_digest,
        "result_schema_digest": snapshot.result_schema_digest,
        "missingness_rule_digest": snapshot.missingness_rule_digest,
        "stopping_rule_digest": snapshot.stopping_rule_digest,
        "scoring_algorithm_id": snapshot.scoring_algorithm_id,
        "scoring_algorithm_version": snapshot.scoring_algorithm_version,
        "scoring_algorithm_digest": snapshot.scoring_algorithm_digest,
        "score_min": snapshot.score_min,
        "score_max": snapshot.score_max,
        "score_direction": snapshot.score_direction,
        "score_rounding_rule": snapshot.score_rounding_rule,
        "automatic_scoring_permitted": snapshot.automatic_scoring_permitted,
        "item_response_storage_permitted": snapshot.item_response_storage_permitted,
        "result_storage_permitted": snapshot.result_storage_permitted,
        "result_export_permitted": snapshot.result_export_permitted,
    }


def definition_digest(snapshot: AssessmentDefinitionSpec) -> str:
    return digest_for(_definition_projection(snapshot))


def bundle_digest(
    bundle_id: str,
    definitions: tuple[AssessmentDefinitionSpec, AssessmentDefinitionSpec],
    *,
    formal_research_approved: bool,
) -> str:
    ordered = sorted(definitions, key=lambda row: row.category_key)
    return digest_for({
        "schema": "assessment-definition-bundle.v1",
        "bundle_id": bundle_id,
        "definitions": [
            {
                "category_key": row.category_key,
                "definition_id": row.definition_id,
                "definition_digest": row.definition_digest,
            }
            for row in ordered
        ],
        "formal_research_approved": formal_research_approved,
    })


def validate_registered_bundle(bundle: RegisteredAssessmentBundle) -> None:
    snapshots = tuple(bundle.snapshot.definitions)
    snapshot_categories = {row.category_key for row in snapshots}
    if snapshot_categories != set(bundle.definitions):
        _fail(
            "assessment_bundle_adapter_mismatch",
            "definition adapters do not match the exact bundle categories",
        )
    for snapshot in snapshots:
        registered = bundle.definitions[snapshot.category_key]
        if registered.snapshot != snapshot:
            _fail(
                "assessment_bundle_adapter_mismatch",
                "definition adapter snapshot differs from the bundle snapshot",
            )
        item_keys = [row.item_key for row in snapshot.items]
        if snapshot.item_set_digest != digest_for({
            "schema": "assessment-item-set.v1",
            "item_keys": sorted(item_keys),
        }):
            _fail(
                "assessment_item_set_digest_mismatch",
                f"definition {snapshot.definition_id} item-set digest is invalid",
            )
        if snapshot.definition_digest != definition_digest(snapshot):
            _fail(
                "assessment_definition_digest_mismatch",
                f"definition {snapshot.definition_id} digest is invalid",
            )
        if not (
            snapshot.automatic_scoring_permitted
            and snapshot.item_response_storage_permitted
            and snapshot.result_storage_permitted
        ):
            _fail(
                "assessment_definition_runtime_permission_missing",
                "registered platform definition lacks scoring/response/result permission",
            )
    expected_bundle_digest = bundle_digest(
        bundle.snapshot.bundle_id,
        (snapshots[0], snapshots[1]),
        formal_research_approved=bundle.snapshot.formal_research_approved,
    )
    if bundle.snapshot.bundle_digest != expected_bundle_digest:
        _fail(
            "assessment_bundle_digest_mismatch",
            "assessment definition bundle digest is invalid",
        )


def build_synthetic_registered_definition(
    *,
    definition_id: str,
    category_key: AssessmentCategoryKey,
    item_keys: tuple[str, ...],
    instrument_id: str,
    instrument_version: str = "synthetic-v1",
) -> RegisteredAssessmentDefinition:
    """Build a numeric synthetic adapter; installation remains pytest-only."""
    items = tuple(AssessmentItemSpec(item_key=key) for key in item_keys)
    item_set_digest = digest_for({
        "schema": "assessment-item-set.v1", "item_keys": sorted(item_keys)})
    administration_digest = digest_for({
        "schema": "synthetic-administration.v1", "definition_id": definition_id})
    response_digest = digest_for({
        "schema": "synthetic-numeric-response.v1", "minimum": 0, "maximum": 2})
    result_digest = digest_for({
        "schema": "synthetic-score-result.v1", "field": "total_score"})
    missingness_digest = digest_for({
        "schema": "synthetic-missingness.v1", "all_items_required": True})
    stopping_digest = digest_for({
        "schema": "synthetic-stopping.v1", "early_stop": False})
    algorithm_id = "synthetic_numeric_item_sum"
    algorithm_version = "v1"
    algorithm_digest = digest_for({
        "schema": "synthetic-scoring.v1",
        "algorithm_id": algorithm_id,
        "algorithm_version": algorithm_version,
        "item_keys": sorted(item_keys),
    })
    provisional = AssessmentDefinitionSpec(
        definition_id=definition_id,
        category_key=category_key,
        instrument_id=instrument_id,
        instrument_version=instrument_version,
        definition_digest="sha256:" + "0" * 64,
        item_set_digest=item_set_digest,
        administration_protocol_digest=administration_digest,
        response_schema_digest=response_digest,
        result_schema_digest=result_digest,
        missingness_rule_digest=missingness_digest,
        stopping_rule_digest=stopping_digest,
        scoring_algorithm_id=algorithm_id,
        scoring_algorithm_version=algorithm_version,
        scoring_algorithm_digest=algorithm_digest,
        score_min=0,
        score_max=float(2 * len(item_keys)),
        score_direction="higher_is_better",
        score_rounding_rule="integer_exact",
        automatic_scoring_permitted=True,
        item_response_storage_permitted=True,
        result_storage_permitted=True,
        result_export_permitted=False,
        items=items,
    )
    snapshot = provisional.model_copy(update={
        "definition_digest": definition_digest(provisional)})

    allowed = set(item_keys)

    def validate_response(
        item_key: str, payload: dict[str, object]
    ) -> NormalizedAssessmentResponse:
        if item_key not in allowed:
            _fail("assessment_item_unknown", "item key is not in the frozen definition")
        artifact = payload.get("authorized_artifact_digest")
        has_value = "value" in payload
        if not has_value:
            _fail("assessment_response_value_required", "synthetic response requires value")
        value = payload.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail("assessment_response_invalid", "synthetic response must be numeric")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or numeric > 2:
            _fail("assessment_response_invalid", "synthetic response is outside 0..2")
        normalized: dict[str, object] = {"value": numeric}
        if artifact is not None:
            normalized["authorized_artifact_digest"] = artifact
        return NormalizedAssessmentResponse(normalized)

    def score_responses(
        responses: Mapping[str, dict[str, object]],
    ) -> AssessmentScore:
        missing = sorted(allowed - set(responses))
        if missing:
            _fail(
                "assessment_responses_incomplete",
                "all frozen synthetic items require a latest response",
            )
        total = sum(float(responses[key]["value"]) for key in sorted(allowed))
        return AssessmentScore(
            score=total,
            result={"total_score": total},
            answered_item_count=len(allowed),
            missing_item_count=0,
        )

    return RegisteredAssessmentDefinition(snapshot, validate_response, score_responses)


def build_synthetic_bundle(
    *,
    bundle_id: str = "synthetic-two-outcomes-v1",
) -> RegisteredAssessmentBundle:
    first = build_synthetic_registered_definition(
        definition_id="synthetic-naming-v1",
        category_key="untrained_standardized_naming",
        item_keys=("naming_01", "naming_02"),
        instrument_id="synthetic_naming",
    )
    second = build_synthetic_registered_definition(
        definition_id="synthetic-functional-v1",
        category_key="functional_communication",
        item_keys=("functional_01", "functional_02"),
        instrument_id="synthetic_functional",
    )
    definitions = (first.snapshot, second.snapshot)
    snapshot = AssessmentDefinitionBundle(
        bundle_id=bundle_id,
        bundle_digest=bundle_digest(
            bundle_id, definitions, formal_research_approved=False),
        definitions=definitions,
        formal_research_approved=False,
    )
    return RegisteredAssessmentBundle(
        snapshot=snapshot,
        definitions=MappingProxyType({
            first.snapshot.category_key: first,
            second.snapshot.category_key: second,
        }),
    )


_REGISTRY_LOCK = threading.RLock()
_BUNDLES: dict[str, RegisteredAssessmentBundle] = {}
_ACTIVE_BUNDLE_ID: str | None = None


def registered_bundles() -> tuple[AssessmentDefinitionBundle, ...]:
    with _REGISTRY_LOCK:
        return tuple(
            _BUNDLES[key].snapshot.model_copy(deep=True)
            for key in sorted(_BUNDLES)
        )


def current_bundle(bundle_id: str | None = None) -> RegisteredAssessmentBundle:
    with _REGISTRY_LOCK:
        if not _BUNDLES:
            _fail(
                "assessment_definitions_not_ready",
                "formal assessment definitions are not frozen",
            )
        if bundle_id is None:
            if _ACTIVE_BUNDLE_ID is None:
                _fail(
                    "assessment_bundle_selection_required",
                    "an active assessment definition bundle is not selected",
                )
            bundle = _BUNDLES.get(_ACTIVE_BUNDLE_ID)
            if bundle is None:
                _fail(
                    "assessment_bundle_selection_invalid",
                    "active assessment definition bundle is not registered",
                )
        else:
            bundle = _BUNDLES.get(bundle_id)
            if bundle is None:
                _fail(
                    "assessment_bundle_not_found",
                    "requested assessment definition bundle is not registered",
                )
        validate_registered_bundle(bundle)
        return bundle


def install_production_bundles(
    bundles: tuple[RegisteredAssessmentBundle, ...],
    *,
    active_bundle_id: str,
) -> None:
    """Permanently install loader-verified production bundles (startup path).

    与 pytest 安装器不同:接受包内声明的 formal_research_approved(该事实由
    PI 制品供给且被 bundle_digest 覆盖),且为持久安装。每个 bundle 在此再次
    整体校验;归档版本一并安装,在途事件按冻结 bundle_id 解析(rollover 语义)。
    仅允许对空注册表安装——生产进程启动恰好一次,拒绝运行中静默换表。
    """
    if not bundles:
        _fail(
            "assessment_production_registry_empty",
            "production install requires at least one bundle",
        )
    by_id: dict[str, RegisteredAssessmentBundle] = {}
    for selected in bundles:
        validate_registered_bundle(selected)
        if selected.snapshot.bundle_id in by_id:
            _fail(
                "assessment_bundle_duplicate",
                "assessment definition bundle ids must be unique",
            )
        by_id[selected.snapshot.bundle_id] = selected
    if active_bundle_id not in by_id:
        _fail(
            "assessment_bundle_selection_invalid",
            "active assessment definition bundle is not registered",
        )
    global _ACTIVE_BUNDLE_ID
    with _REGISTRY_LOCK:
        if _BUNDLES:
            _fail(
                "assessment_registry_already_installed",
                "assessment definition registry is already installed",
            )
        _BUNDLES.update(by_id)
        _ACTIVE_BUNDLE_ID = active_bundle_id


@contextmanager
def install_synthetic_bundle_for_testing(
    bundle: RegisteredAssessmentBundle | None = None,
) -> Iterator[RegisteredAssessmentBundle]:
    """Temporarily install one non-formal bundle in an actual pytest process."""
    selected = bundle or build_synthetic_bundle()
    with install_synthetic_bundles_for_testing(
        (selected,), active_bundle_id=selected.snapshot.bundle_id,
    ):
        yield selected


@contextmanager
def install_synthetic_bundles_for_testing(
    bundles: tuple[RegisteredAssessmentBundle, ...],
    *,
    active_bundle_id: str,
) -> Iterator[tuple[RegisteredAssessmentBundle, ...]]:
    """Install one active bundle plus immutable archived versions for tests.

    New events bind the active version.  Existing events resolve their frozen
    ``bundle_id`` against any archived version, so a safe rollout never has to
    choose between accepting new work and stranding an in-flight assessment.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        _fail(
            "assessment_test_registry_forbidden",
            "synthetic assessment definitions may only be installed by pytest",
        )
    if not bundles:
        _fail(
            "assessment_test_registry_empty",
            "synthetic assessment registry requires at least one bundle",
        )
    by_id: dict[str, RegisteredAssessmentBundle] = {}
    for selected in bundles:
        if selected.snapshot.formal_research_approved:
            _fail(
                "assessment_test_bundle_must_be_nonformal",
                "a synthetic test bundle cannot claim formal research approval",
            )
        validate_registered_bundle(selected)
        if selected.snapshot.bundle_id in by_id:
            _fail(
                "assessment_bundle_duplicate",
                "assessment definition bundle ids must be unique",
            )
        by_id[selected.snapshot.bundle_id] = selected
    if active_bundle_id not in by_id:
        _fail(
            "assessment_bundle_selection_invalid",
            "active assessment definition bundle is not registered",
        )
    global _ACTIVE_BUNDLE_ID
    with _REGISTRY_LOCK:
        previous = dict(_BUNDLES)
        previous_active = _ACTIVE_BUNDLE_ID
        _BUNDLES.clear()
        _BUNDLES.update(by_id)
        _ACTIVE_BUNDLE_ID = active_bundle_id
    try:
        yield bundles
    finally:
        with _REGISTRY_LOCK:
            _BUNDLES.clear()
            _BUNDLES.update(previous)
            _ACTIVE_BUNDLE_ID = previous_active
