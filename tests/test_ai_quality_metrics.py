"""Synthetic, identifier-free tests for the AI quality aggregator."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from app.ai_quality_metrics import (
    AttemptQualityEvidence,
    QualityDimensions,
    TurnQualityEvidence,
    aggregate_ai_quality as _aggregate_ai_quality,
    dashboard_payload_keys,
)
from app.ai_quality_service import _ai_binary


GENERATED_AT = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
TRUSTED_LOW_CARDINALITY_VALUES = {
    "content_group": {"common-object-naming"},
    "provider_id": {"qwen-cloud"},
    "device_profile": {"tablet-chrome-mic-v1"},
}


def aggregate_ai_quality(evidence, **kwargs):
    kwargs.setdefault(
        "trusted_low_cardinality_values",
        TRUSTED_LOW_CARDINALITY_VALUES,
    )
    return _aggregate_ai_quality(evidence, **kwargs)


def _dimensions(**overrides: object) -> QualityDimensions:
    values: dict[str, object] = {
        "data_classification": "research",
        "week_no": 2,
        "phase_type": "正式训练",
        "task_type": "单要素",
        "content_group": "common-object-naming",
        "provider_id": "qwen-cloud",
        "device_profile": "tablet-chrome-mic-v1",
        "protocol_version": "week2-protocol-v1",
        "asr_engine_version": "qwen-asr-v1",
        "judge_engine_version": "judge-v2",
    }
    values.update(overrides)
    return QualityDimensions(**values)  # type: ignore[arg-type]


def _turn(
    *,
    ai: bool,
    truth: bool,
    prompt_level: int,
    latency_ms: int,
    status: str = "completed",
    asr_reviewed: bool = True,
    asr_corrected: bool | None = False,
    pause_count: int = 0,
    takeover_count: int = 0,
    dimensions: QualityDimensions | None = None,
    operational_score: float | None = None,
) -> TurnQualityEvidence:
    return TurnQualityEvidence(
        dimensions=dimensions or _dimensions(),
        eligible=True,
        attempts=(AttemptQualityEvidence(
            prompt_level=prompt_level,
            processing_status=status,
            latency_ms=latency_ms,
        ),),
        audio_evidenced=True,
        ai_attempted=True,
        ai_judged=True,
        ai_predicted_correct=ai,
        asr_reviewed=asr_reviewed,
        asr_corrected=asr_corrected,
        human_truth_locked=True,
        human_truth_correct=truth,
        technical_pause_count=pause_count,
        researcher_takeover_count=takeover_count,
        operational_score=operational_score,
    )


def test_aggregate_computes_counts_rates_confusion_and_latency_percentiles():
    evidence = (
        _turn(ai=True, truth=True, prompt_level=0, latency_ms=100),
        _turn(
            ai=False,
            truth=False,
            prompt_level=1,
            latency_ms=200,
            asr_corrected=True,
            pause_count=1,
        ),
        _turn(
            ai=True,
            truth=False,
            prompt_level=2,
            latency_ms=300,
            pause_count=1,
            takeover_count=1,
        ),
        replace(_turn(
            ai=False,
            truth=True,
            prompt_level=3,
            latency_ms=400,
            asr_reviewed=False,
            asr_corrected=None,
        ), attempts=(
            AttemptQualityEvidence(3, "technical_failure", 400),
            AttemptQualityEvidence(3, "completed", 500),
        )),
    )

    dashboard = aggregate_ai_quality(
        evidence,
        generated_at=GENERATED_AT,
        group_by=("week", "task_type", "content", "provider", "device", "version"),
    )

    assert len(dashboard.rows) == 1
    row = dashboard.rows[0]
    operational = row.operational
    assert operational.eligible_turns == 4
    assert operational.ai_attempted_turns == 4
    assert operational.ai_judged_turns == 4
    assert operational.asr_reviewed_turns == 3
    assert operational.asr_corrected_turns == 1
    assert operational.total_attempts == 5
    assert (
        operational.prompt_level_0_count,
        operational.prompt_level_1_count,
        operational.prompt_level_2_count,
        operational.prompt_level_3_count,
    ) == (1, 1, 1, 2)
    assert operational.technical_failure_attempts == 1
    assert operational.technical_pause_count == 2
    assert operational.researcher_takeover_count == 1
    assert operational.latency_sample_count == 5
    assert operational.latency_p50_ms == 300
    assert operational.latency_p95_ms == 480

    truth = row.research_truth
    assert truth.reviewed_decisions == 4
    assert truth.true_positive == 1
    assert truth.true_negative == 1
    assert truth.false_positive == 1
    assert truth.false_negative == 1

    assert row.rates.asr_manual_correction_rate == pytest.approx(1 / 3)
    assert row.rates.prompt_escalation_rate == pytest.approx(4 / 5)
    assert row.rates.tell_answer_rate == pytest.approx(2 / 5)
    assert row.rates.technical_failure_rate == pytest.approx(1 / 5)
    assert row.rates.technical_pause_rate == pytest.approx(1 / 2)
    assert row.rates.researcher_takeover_rate == pytest.approx(1 / 4)
    assert row.rates.false_positive_rate == pytest.approx(1 / 2)
    assert row.rates.false_negative_rate == pytest.approx(1 / 2)


def test_payload_exactly_matches_strict_aggregate_only_contract():
    dashboard = aggregate_ai_quality(
        [_turn(ai=True, truth=True, prompt_level=0, latency_ms=120)],
        generated_at=GENERATED_AT,
    )
    payload = dashboard.to_dict()

    assert payload["schema_version"] == "ai-quality-dashboard.v1"
    assert payload["generated_at"] == "2026-07-22T10:00:00Z"
    assert payload["privacy"] == {
        "aggregation_only": True,
        "contains_patient_identifiers": False,
        "contains_audio": False,
        "contains_transcripts": False,
    }
    assert set(payload) == {"schema_version", "generated_at", "privacy", "rows"}
    row = payload["rows"][0]  # type: ignore[index]
    assert set(row) == {"dimensions", "operational", "research_truth"}
    assert set(row["dimensions"]) == set(dashboard_payload_keys()["dimensions"])
    assert set(row["operational"]) == set(dashboard_payload_keys()["operational"])
    assert set(row["research_truth"]) == set(
        dashboard_payload_keys()["research_truth"])

    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden_field in (
        '"patient_id":',
        '"subject_id":',
        '"session_id":',
        '"item_id":',
        '"raw_audio_id":',
        '"transcript":',
        '"asr_text":',
        '"confirmed_response_text":',
        '"coverage":',
        '"unknown_reason_counts":',
        '"rates":',
    ):
        assert forbidden_field not in serialized


def test_missing_dimensions_are_retained_as_null_group_and_classifications_never_merge():
    unknown_dimensions = QualityDimensions(
        data_classification=None,
        week_no=2,
        task_type="单要素",
    )
    simulation_dimensions = _dimensions(data_classification="simulation")
    dashboard = aggregate_ai_quality(
        [
            _turn(
                ai=True,
                truth=True,
                prompt_level=0,
                latency_ms=100,
                dimensions=unknown_dimensions,
            ),
            _turn(
                ai=True,
                truth=True,
                prompt_level=0,
                latency_ms=100,
                dimensions=simulation_dimensions,
            ),
        ],
        generated_at=GENERATED_AT,
        group_by=("week",),
    )

    assert len(dashboard.rows) == 2
    assert {row.dimensions.data_classification for row in dashboard.rows} == {
        None,
        "simulation",
    }
    unknown_row = next(
        row for row in dashboard.rows
        if row.dimensions.data_classification is None
    )
    assert unknown_row.dimensions.week_no == 2
    assert unknown_row.coverage.turns_with_unknown_dimensions == 1
    assert unknown_row.coverage.unknown_count("dimension_unknown") == 1


def test_missing_and_inconsistent_evidence_becomes_null_and_fixed_code_coverage():
    evidence = TurnQualityEvidence(
        dimensions=QualityDimensions(
            data_classification="research",
            week_no=0,
            task_type="单要素",
        ),
        eligible=None,
        attempts=None,
        audio_evidenced=None,
        ai_attempted=True,
        ai_judged=True,
        ai_predicted_correct=True,
        asr_reviewed=False,
        asr_corrected=True,
        human_truth_locked=False,
        human_truth_correct=True,
        technical_pause_count=None,
        researcher_takeover_count=-1,
        operational_score=1.0,
    )

    row = aggregate_ai_quality(
        [evidence],
        generated_at=GENERATED_AT,
        group_by=("week", "task_type", "provider", "device"),
    ).rows[0]

    operational = row.operational
    assert operational.eligible_turns is None
    assert operational.ai_attempted_turns is None
    assert operational.ai_judged_turns is None
    assert operational.asr_reviewed_turns is None
    assert operational.asr_corrected_turns is None
    assert operational.total_attempts is None
    assert operational.prompt_level_0_count is None
    assert operational.technical_failure_attempts is None
    assert operational.technical_pause_count is None
    assert operational.researcher_takeover_count is None
    assert operational.latency_sample_count is None
    assert operational.latency_p50_ms is None
    assert operational.latency_p95_ms is None
    assert row.research_truth.reviewed_decisions is None

    coverage = row.coverage
    assert coverage.source_turns == 1
    assert coverage.eligibility_known_turns == 0
    assert coverage.audio_evidence_known_turns == 0
    assert coverage.audio_evidenced_turns == 0
    assert coverage.attempts_observed == 0
    assert coverage.inconsistent_turns == 1
    for code in (
        "dimension_week_no_invalid",
        "dimension_unknown",
        "eligibility_unknown",
        "audio_evidence_unknown",
        "ai_attempt_without_audio_evidence",
        "ai_attempt_on_ineligible_turn",
        "ai_judgement_without_attempt",
        "human_truth_without_lock",
        "technical_pause_count_unknown",
        "researcher_takeover_count_invalid",
        "attempt_ledger_unknown",
    ):
        assert coverage.unknown_count(code) == 1


def test_partial_attempt_coverage_keeps_known_latency_but_not_false_prompt_totals():
    evidence = TurnQualityEvidence(
        dimensions=_dimensions(provider_id=None, device_profile=None),
        eligible=True,
        attempts=(
            AttemptQualityEvidence(None, "completed", None),
            AttemptQualityEvidence(2, "completed", 225),
        ),
        audio_evidenced=True,
        ai_attempted=True,
        ai_judged=True,
        ai_predicted_correct=None,
        asr_reviewed=True,
        asr_corrected=False,
        human_truth_locked=True,
        human_truth_correct=True,
        operational_score=1.0,
    )

    row = aggregate_ai_quality(
        [evidence], generated_at=GENERATED_AT).rows[0]
    assert row.operational.total_attempts == 2
    assert row.operational.prompt_level_0_count is None
    assert row.operational.prompt_level_1_count is None
    assert row.operational.prompt_level_2_count is None
    assert row.operational.prompt_level_3_count is None
    assert row.operational.technical_failure_attempts == 0
    assert row.operational.latency_sample_count == 1
    assert row.operational.latency_p50_ms == 225
    assert row.operational.latency_p95_ms == 225
    assert row.research_truth.reviewed_decisions == 0
    assert row.research_truth.true_positive == 0
    assert row.coverage.human_truth_known_turns == 1
    assert row.coverage.human_truth_locked_turns == 1
    assert row.coverage.binary_comparison_eligible_turns == 0
    assert row.coverage.unknown_count("ai_binary_prediction_unknown") == 1
    assert row.coverage.unknown_count("prompt_level_unknown") == 1
    assert row.coverage.unknown_count("latency_unknown") == 1


def test_ai_metrics_fail_closed_when_audio_evidence_is_absent():
    missing_audio = replace(
        _turn(ai=True, truth=True, prompt_level=0, latency_ms=100),
        audio_evidenced=False,
    )

    row = aggregate_ai_quality(
        [missing_audio], generated_at=GENERATED_AT).rows[0]

    assert row.operational.eligible_turns == 1
    assert row.operational.ai_attempted_turns is None
    assert row.operational.ai_judged_turns is None
    assert row.research_truth.reviewed_decisions is None
    assert row.coverage.audio_evidence_known_turns == 1
    assert row.coverage.audio_evidenced_turns == 0
    assert row.coverage.unknown_count("audio_evidence_missing") == 1
    assert row.coverage.unknown_count("ai_attempt_without_audio_evidence") == 1


def test_technical_failure_is_never_counted_as_an_incorrect_answer():
    failed = TurnQualityEvidence(
        dimensions=_dimensions(),
        eligible=True,
        attempts=(AttemptQualityEvidence(0, "technical_failure", 300),),
        audio_evidenced=True,
        ai_attempted=True,
        ai_judged=True,
        ai_predicted_correct=False,
        asr_reviewed=False,
        asr_corrected=None,
        human_truth_locked=True,
        human_truth_correct=True,
        operational_score=0.0,
    )

    row = aggregate_ai_quality([failed], generated_at=GENERATED_AT).rows[0]

    assert row.operational.technical_failure_attempts == 1
    assert row.operational.ai_judged_turns is None
    assert row.research_truth.reviewed_decisions is None
    assert row.research_truth.false_negative is None
    assert row.coverage.binary_comparison_eligible_turns == 0
    assert row.coverage.unknown_count(
        "ai_judgement_without_completed_attempt") == 1


def test_operational_score_never_substitutes_for_ai_or_locked_research_truth():
    no_locked_truth = TurnQualityEvidence(
        dimensions=_dimensions(),
        eligible=True,
        attempts=(AttemptQualityEvidence(0, "completed", 100),),
        audio_evidenced=True,
        ai_attempted=True,
        ai_judged=True,
        ai_predicted_correct=True,
        asr_reviewed=False,
        asr_corrected=None,
        human_truth_locked=False,
        human_truth_correct=None,
        operational_score=0.0,
    )
    high_operational_score = replace(no_locked_truth, operational_score=1.0)

    low = aggregate_ai_quality(
        [no_locked_truth], generated_at=GENERATED_AT).rows[0]
    high = aggregate_ai_quality(
        [high_operational_score], generated_at=GENERATED_AT).rows[0]

    assert low.research_truth == high.research_truth
    assert low.research_truth.reviewed_decisions == 0
    assert low.research_truth.true_positive == 0
    assert low.coverage.unknown_count("human_truth_unavailable") == 1

    # A locked label still requires an independently mapped binary AI class;
    # operational_score=1.0 is not silently converted to a positive prediction.
    missing_ai_class = replace(
        high_operational_score,
        ai_predicted_correct=None,
        human_truth_locked=True,
        human_truth_correct=True,
    )
    missing = aggregate_ai_quality(
        [missing_ai_class], generated_at=GENERATED_AT).rows[0]
    assert missing.research_truth.reviewed_decisions == 0
    assert missing.coverage.unknown_count("ai_binary_prediction_unknown") == 1


@pytest.mark.parametrize("nondecision", ["未识别", "沉默", "拒答"])
def test_nondecision_operational_outcomes_stay_outside_binary_matrix(
        nondecision):
    evidence = replace(
        _turn(ai=True, truth=True, prompt_level=0, latency_ms=100),
        ai_predicted_correct=_ai_binary(nondecision),
    )

    row = aggregate_ai_quality(
        [evidence], generated_at=GENERATED_AT).rows[0]

    assert row.research_truth.reviewed_decisions == 0
    assert row.research_truth.false_negative == 0
    assert row.coverage.human_truth_locked_turns == 1
    assert row.coverage.binary_comparison_eligible_turns == 0
    assert row.coverage.unknown_count("ai_binary_prediction_unknown") == 1


def test_privacy_limits_reject_fingerprints_and_high_cardinality_labels():
    fingerprinted = replace(
        _turn(ai=True, truth=True, prompt_level=0, latency_ms=100),
        dimensions=_dimensions(
            device_profile="device-hash-abcdef1234567890"),
    )
    with pytest.raises(ValueError, match="record identifier or fingerprint"):
        aggregate_ai_quality([fingerprinted], generated_at=GENERATED_AT)

    high_cardinality = [
        _turn(
            ai=True,
            truth=True,
            prompt_level=0,
            latency_ms=100,
            dimensions=_dimensions(content_group=f"content-family-{index}"),
        )
        for index in range(33)
    ]
    with pytest.raises(ValueError, match="low-cardinality privacy limit"):
        _aggregate_ai_quality(
            high_cardinality,
            generated_at=GENERATED_AT,
            trusted_low_cardinality_values={
                **TRUSTED_LOW_CARDINALITY_VALUES,
                "content_group": {
                    f"content-family-{index}" for index in range(33)
                },
            },
        )


def test_nonempty_low_cardinality_dimensions_require_explicit_trust():
    evidence = _turn(ai=True, truth=True, prompt_level=0, latency_ms=100)
    with pytest.raises(ValueError, match="trusted aggregate allowlist"):
        _aggregate_ai_quality([evidence], generated_at=GENERATED_AT)

    singleton_free_text = replace(
        evidence,
        dimensions=_dimensions(device_profile="病房3床12号"),
    )
    with pytest.raises(ValueError, match="trusted aggregate allowlist"):
        _aggregate_ai_quality(
            [singleton_free_text],
            generated_at=GENERATED_AT,
            trusted_low_cardinality_values=TRUSTED_LOW_CARDINALITY_VALUES,
        )


def test_research_truth_metrics_are_all_known_or_all_null():
    from app.ai_quality_metrics import ResearchTruthMetrics

    with pytest.raises(ValueError, match="all known or all null"):
        ResearchTruthMetrics(None, 1, None, None, None)
    with pytest.raises(ValueError, match="must sum"):
        ResearchTruthMetrics(1, 1, 1, 0, 0)


def test_invalid_contract_inputs_fail_closed():
    with pytest.raises(ValueError, match="timezone-aware"):
        aggregate_ai_quality(
            [], generated_at=datetime(2026, 7, 22, 10, 0))

    with pytest.raises(ValueError, match="unsupported quality dimension"):
        aggregate_ai_quality(
            [], generated_at=GENERATED_AT, group_by=("patient_id",))

    with pytest.raises(TypeError, match="TurnQualityEvidence"):
        aggregate_ai_quality(
            [object()],  # type: ignore[list-item]
            generated_at=GENERATED_AT,
        )
