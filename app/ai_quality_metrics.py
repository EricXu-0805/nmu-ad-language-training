"""Deidentified, pure aggregation for AI quality monitoring.

This module deliberately has no database, HTTP, or model dependency.  The
integration layer must first project the existing Attempt/Interaction/Audio,
ASR, AI judgement, locked human-truth, provider, device-profile, and version
ledgers onto :class:`TurnQualityEvidence`.

The projection is one expected protocol turn per row and contains no patient,
subject, session, item, audio, transcript, or response identifier.  In
particular:

* ``content_group`` is a low-cardinality content family, never an item id;
* ``device_profile`` is a model/capability class, never a device hash;
* ``human_truth_correct`` is usable only when ``human_truth_locked`` is true
  and the value came from a frozen, task-specific research-truth mapping;
* ``operational_score`` is accepted only to make that boundary testable.  It
  is never used as human truth or to fill a missing binary AI prediction.

The strict dashboard payload intentionally contains only aggregate fields.
Metric coverage and fixed-code unknown/inconsistency counts remain available
on each :class:`QualityMetricRow` through ``coverage`` without entering the
privacy-constrained JSON payload.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import math
import re
from typing import Iterable, Literal, Mapping, Sequence


SCHEMA_VERSION = "ai-quality-dashboard.v1"
MAX_ROWS = 500
MAX_DIMENSION_LENGTH = 128
MAX_LOW_CARDINALITY_VALUES = 32

DimensionName = Literal[
    "data_classification",
    "week_no",
    "phase_type",
    "task_type",
    "content_group",
    "provider_id",
    "device_profile",
    "protocol_version",
    "asr_engine_version",
    "judge_engine_version",
]

DIMENSION_NAMES: tuple[DimensionName, ...] = (
    "data_classification",
    "week_no",
    "phase_type",
    "task_type",
    "content_group",
    "provider_id",
    "device_profile",
    "protocol_version",
    "asr_engine_version",
    "judge_engine_version",
)

DEFAULT_GROUP_BY: tuple[DimensionName, ...] = DIMENSION_NAMES

_GROUP_ALIASES: Mapping[str, tuple[DimensionName, ...]] = {
    "week": ("week_no",),
    "content": ("content_group",),
    "provider": ("provider_id",),
    "device": ("device_profile",),
    "version": (
        "protocol_version",
        "asr_engine_version",
        "judge_engine_version",
    ),
}
_LOW_CARDINALITY_FIELDS = (
    "content_group",
    "provider_id",
    "device_profile",
)
_FINGERPRINT_RE = re.compile(
    r"(?:^[a-f\d]{24,64}$)|"
    r"(?:^[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12}$)|"
    r"(?:(?:item|session|patient|subject|participant|device)"
    r"[-_ ]?(?:hash|digest|fingerprint))",
    re.IGNORECASE,
)
_KNOWN_PROCESSING_STATUSES = frozenset({
    "received",
    "asr_completed",
    "completed",
    "technical_failure",
})
_INCONSISTENCY_CODES = frozenset({
    "dimension_data_classification_invalid",
    "dimension_week_no_invalid",
    "dimension_phase_type_invalid",
    "dimension_task_type_invalid",
    "dimension_content_group_invalid",
    "dimension_provider_id_invalid",
    "dimension_device_profile_invalid",
    "dimension_protocol_version_invalid",
    "dimension_asr_engine_version_invalid",
    "dimension_judge_engine_version_invalid",
    "ai_attempt_on_ineligible_turn",
    "ai_attempt_without_audio_evidence",
    "ai_judgement_without_attempt",
    "ai_judgement_without_completed_attempt",
    "ai_prediction_without_judgement",
    "asr_correction_without_review",
    "asr_review_on_ineligible_turn",
    "locked_human_truth_label_unknown",
    "human_truth_without_lock",
    "prompt_level_invalid",
    "processing_status_invalid",
    "latency_invalid",
    "technical_pause_count_invalid",
    "researcher_takeover_count_invalid",
    "operational_score_invalid",
})


@dataclass(frozen=True)
class QualityDimensions:
    """Low-cardinality, deidentified dimensions allowed in the dashboard."""

    data_classification: str | None = None
    week_no: int | None = None
    phase_type: str | None = None
    task_type: str | None = None
    content_group: str | None = None
    provider_id: str | None = None
    device_profile: str | None = None
    protocol_version: str | None = None
    asr_engine_version: str | None = None
    judge_engine_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AttemptQualityEvidence:
    """One current-ledger attempt's non-text operational evidence.

    ``prompt_level`` is intentionally the frozen AttemptEvent 0..3 contract
    (0=no prompt, 3=tell answer).  It does not claim to model a future generic
    task contract whose hint ladder may have more levels.
    """

    prompt_level: int | None
    processing_status: str | None
    latency_ms: int | float | None = None


@dataclass(frozen=True)
class TurnQualityEvidence:
    """One expected protocol turn projected onto deidentified quality facts.

    ``ai_predicted_correct`` must be an independently mapped binary AI class.
    Ambiguous AI classes (for example partial/related answers) stay ``None``.
    ``human_truth_correct`` must stay ``None`` until a reviewer has locked the
    task-specific research truth.  Neither field may be inferred from
    ``operational_score``.
    """

    dimensions: QualityDimensions
    eligible: bool | None
    attempts: tuple[AttemptQualityEvidence, ...] | None
    audio_evidenced: bool | None = None
    ai_attempted: bool | None = None
    ai_judged: bool | None = None
    ai_predicted_correct: bool | None = None
    asr_reviewed: bool | None = None
    asr_corrected: bool | None = None
    human_truth_locked: bool | None = None
    human_truth_correct: bool | None = None
    technical_pause_count: int | None = 0
    researcher_takeover_count: int | None = 0
    # Operational only.  Deliberately unused by every research-truth metric.
    operational_score: float | None = None


@dataclass(frozen=True)
class OperationalMetrics:
    eligible_turns: int | None
    ai_attempted_turns: int | None
    ai_judged_turns: int | None
    asr_reviewed_turns: int | None
    asr_corrected_turns: int | None
    total_attempts: int | None
    prompt_level_0_count: int | None
    prompt_level_1_count: int | None
    prompt_level_2_count: int | None
    prompt_level_3_count: int | None
    technical_failure_attempts: int | None
    technical_pause_count: int | None
    researcher_takeover_count: int | None
    latency_sample_count: int | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchTruthMetrics:
    reviewed_decisions: int | None
    true_positive: int | None
    true_negative: int | None
    false_positive: int | None
    false_negative: int | None

    def __post_init__(self) -> None:
        values = (
            self.reviewed_decisions,
            self.true_positive,
            self.true_negative,
            self.false_positive,
            self.false_negative,
        )
        if all(value is None for value in values):
            return
        if any(value is None for value in values):
            raise ValueError("research truth metrics must be all known or all null")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in values):
            raise ValueError("research truth metrics must be nonnegative integers")
        reviewed, *cells = values
        if sum(cells) != reviewed:
            raise ValueError("confusion matrix must sum to reviewed_decisions")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class QualityRates:
    """Derived rates; intentionally outside the strict dashboard v1 JSON."""

    asr_manual_correction_rate: float | None
    prompt_escalation_rate: float | None
    tell_answer_rate: float | None
    technical_failure_rate: float | None
    technical_pause_rate: float | None
    researcher_takeover_rate: float | None
    false_positive_rate: float | None
    false_negative_rate: float | None


@dataclass(frozen=True)
class QualityCoverage:
    """Deidentified source coverage and fixed-code unknown diagnostics."""

    source_turns: int
    turns_with_unknown_dimensions: int
    eligibility_known_turns: int
    audio_evidence_known_turns: int
    audio_evidenced_turns: int
    ai_attempt_status_known_turns: int
    ai_judgement_status_known_turns: int
    asr_review_status_known_turns: int
    human_truth_locked_turns: int
    human_truth_known_turns: int
    binary_comparison_eligible_turns: int
    attempts_observed: int
    prompt_level_known_attempts: int
    processing_status_known_attempts: int
    latency_known_attempts: int
    turns_with_unknown_evidence: int
    inconsistent_turns: int
    unknown_reason_counts: tuple[tuple[str, int], ...]

    def unknown_count(self, code: str) -> int:
        return dict(self.unknown_reason_counts).get(code, 0)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["unknown_reason_counts"] = dict(self.unknown_reason_counts)
        return payload


@dataclass(frozen=True)
class QualityMetricRow:
    dimensions: QualityDimensions
    operational: OperationalMetrics
    research_truth: ResearchTruthMetrics
    coverage: QualityCoverage
    rates: QualityRates

    def to_dict(self) -> dict[str, object]:
        """Return exactly the privacy-constrained dashboard row contract."""
        return {
            "dimensions": self.dimensions.to_dict(),
            "operational": self.operational.to_dict(),
            "research_truth": self.research_truth.to_dict(),
        }


@dataclass(frozen=True)
class QualityDashboard:
    generated_at: datetime
    rows: tuple[QualityMetricRow, ...]
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        generated_at = self.generated_at
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        rendered = generated_at.astimezone(timezone.utc).isoformat()
        if rendered.endswith("+00:00"):
            rendered = rendered[:-6] + "Z"
        return {
            "schema_version": self.schema_version,
            "generated_at": rendered,
            "privacy": {
                "aggregation_only": True,
                "contains_patient_identifiers": False,
                "contains_audio": False,
                "contains_transcripts": False,
            },
            "rows": [row.to_dict() for row in self.rows],
        }


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _rates(
    operational: OperationalMetrics,
    truth: ResearchTruthMetrics,
) -> QualityRates:
    prompt_values = (
        operational.prompt_level_0_count,
        operational.prompt_level_1_count,
        operational.prompt_level_2_count,
        operational.prompt_level_3_count,
    )
    if any(value is None for value in prompt_values):
        prompt_escalations = tell_answers = None
    else:
        prompt_escalations = sum(value or 0 for value in prompt_values[1:])
        tell_answers = operational.prompt_level_3_count

    negative_truth = None
    if truth.false_positive is not None and truth.true_negative is not None:
        negative_truth = truth.false_positive + truth.true_negative
    positive_truth = None
    if truth.false_negative is not None and truth.true_positive is not None:
        positive_truth = truth.false_negative + truth.true_positive

    return QualityRates(
        asr_manual_correction_rate=_ratio(
            operational.asr_corrected_turns,
            operational.asr_reviewed_turns,
        ),
        prompt_escalation_rate=_ratio(
            prompt_escalations,
            operational.total_attempts,
        ),
        tell_answer_rate=_ratio(
            tell_answers,
            operational.total_attempts,
        ),
        technical_failure_rate=_ratio(
            operational.technical_failure_attempts,
            operational.total_attempts,
        ),
        technical_pause_rate=_ratio(
            operational.technical_pause_count,
            operational.eligible_turns,
        ),
        researcher_takeover_rate=_ratio(
            operational.researcher_takeover_count,
            operational.eligible_turns,
        ),
        false_positive_rate=_ratio(truth.false_positive, negative_truth),
        false_negative_rate=_ratio(truth.false_negative, positive_truth),
    )


def _canonical_group_by(group_by: Sequence[str]) -> tuple[DimensionName, ...]:
    expanded: list[DimensionName] = []
    for raw_name in group_by:
        names = _GROUP_ALIASES.get(raw_name)
        if names is None:
            if raw_name not in DIMENSION_NAMES:
                raise ValueError(f"unsupported quality dimension: {raw_name}")
            names = (raw_name,)  # type: ignore[assignment]
        for name in names:
            if name not in expanded:
                expanded.append(name)
    # Never merge research, simulation, and unknown-classification evidence.
    if "data_classification" not in expanded:
        expanded.insert(0, "data_classification")
    return tuple(expanded)


def _safe_dimension_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_DIMENSION_LENGTH:
        raise ValueError(f"{field_name} exceeds safe aggregate dimension length")
    if any(ord(character) < 32 or ord(character) == 127
           for character in normalized):
        raise ValueError(f"{field_name} contains a control character")
    if _FINGERPRINT_RE.search(normalized):
        raise ValueError(
            f"{field_name} looks like a record identifier or fingerprint")
    return normalized


def _normalize_dimensions(
    dimensions: QualityDimensions,
) -> tuple[QualityDimensions, tuple[str, ...]]:
    reasons: list[str] = []
    classification = dimensions.data_classification
    if classification not in {"research", "simulation", None}:
        classification = None
        reasons.append("dimension_data_classification_invalid")

    week_no = dimensions.week_no
    if (isinstance(week_no, bool)
            or (week_no is not None
                and (not isinstance(week_no, int) or week_no < 1))):
        week_no = None
        reasons.append("dimension_week_no_invalid")

    values: dict[str, object] = {
        "data_classification": classification,
        "week_no": week_no,
    }
    for field_name in DIMENSION_NAMES[2:]:
        raw_value = getattr(dimensions, field_name)
        normalized = _safe_dimension_string(raw_value, field_name)
        if raw_value is not None and normalized is None:
            reasons.append(f"dimension_{field_name}_invalid")
        values[field_name] = normalized
    return QualityDimensions(**values), tuple(reasons)


def _percentile(values: Sequence[float], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return int(math.floor(ordered[0] + 0.5))
    position = (len(ordered) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    interpolated = lower + ((upper - lower) * (position - lower_index))
    return int(math.floor(interpolated + 0.5))


@dataclass
class _Accumulator:
    source_turns: int = 0
    unknown_dimension_turns: int = 0
    eligibility_known_turns: int = 0
    eligible_turns: int = 0
    eligibility_complete: bool = True
    audio_evidence_known_turns: int = 0
    audio_evidenced_turns: int = 0
    ai_attempt_status_known_turns: int = 0
    ai_attempted_turns: int = 0
    ai_attempt_complete: bool = True
    ai_judgement_status_known_turns: int = 0
    ai_judged_turns: int = 0
    ai_judgement_complete: bool = True
    asr_review_status_known_turns: int = 0
    asr_reviewed_turns: int = 0
    asr_corrected_turns: int = 0
    asr_review_complete: bool = True
    human_truth_locked_turns: int = 0
    human_truth_known_turns: int = 0
    binary_comparison_eligible_turns: int = 0
    reviewed_decisions: int = 0
    true_positive: int = 0
    true_negative: int = 0
    false_positive: int = 0
    false_negative: int = 0
    attempts_observed: int = 0
    attempts_complete: bool = True
    prompt_level_known_attempts: int = 0
    prompt_counts_complete: bool = True
    prompt_counts: list[int] | None = None
    processing_status_known_attempts: int = 0
    technical_status_complete: bool = True
    technical_failures: int = 0
    latency_known_attempts: int = 0
    latencies_ms: list[float] | None = None
    pause_count: int = 0
    pause_complete: bool = True
    takeover_count: int = 0
    takeover_complete: bool = True
    unknown_turns: int = 0
    inconsistent_turns: int = 0
    unknown_reasons: Counter[str] | None = None

    def __post_init__(self) -> None:
        self.prompt_counts = [0, 0, 0, 0]
        self.latencies_ms = []
        self.unknown_reasons = Counter()

    def reason(self, code: str) -> None:
        assert self.unknown_reasons is not None
        self.unknown_reasons[code] += 1


def _strict_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _nonnegative_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _consume_attempts(
    accumulator: _Accumulator,
    attempts: tuple[AttemptQualityEvidence, ...] | None,
) -> None:
    if attempts is None:
        accumulator.attempts_complete = False
        accumulator.prompt_counts_complete = False
        accumulator.technical_status_complete = False
        accumulator.reason("attempt_ledger_unknown")
        return

    accumulator.attempts_observed += len(attempts)
    for attempt in attempts:
        prompt_level = attempt.prompt_level
        if (isinstance(prompt_level, bool) or not isinstance(prompt_level, int)
                or prompt_level not in {0, 1, 2, 3}):
            accumulator.prompt_counts_complete = False
            accumulator.reason(
                "prompt_level_unknown" if prompt_level is None
                else "prompt_level_invalid")
        else:
            accumulator.prompt_level_known_attempts += 1
            assert accumulator.prompt_counts is not None
            accumulator.prompt_counts[prompt_level] += 1

        status = attempt.processing_status
        if status not in _KNOWN_PROCESSING_STATUSES:
            accumulator.technical_status_complete = False
            accumulator.reason(
                "processing_status_unknown" if status is None
                else "processing_status_invalid")
        else:
            accumulator.processing_status_known_attempts += 1
            if status == "technical_failure":
                accumulator.technical_failures += 1

        latency = attempt.latency_ms
        if latency is None:
            accumulator.reason("latency_unknown")
        elif (isinstance(latency, bool) or not isinstance(latency, (int, float))
              or not math.isfinite(latency) or latency < 0):
            accumulator.reason("latency_invalid")
        else:
            accumulator.latency_known_attempts += 1
            assert accumulator.latencies_ms is not None
            accumulator.latencies_ms.append(float(latency))


def _consume_turn(
    accumulator: _Accumulator,
    evidence: TurnQualityEvidence,
    dimensions: QualityDimensions,
    dimension_reasons: tuple[str, ...],
) -> None:
    accumulator.source_turns += 1
    reasons_before = Counter(accumulator.unknown_reasons or {})
    if any(getattr(dimensions, name) is None for name in DIMENSION_NAMES):
        accumulator.unknown_dimension_turns += 1
        accumulator.reason("dimension_unknown")
    for reason in dimension_reasons:
        accumulator.reason(reason)

    eligible = _strict_bool(evidence.eligible)
    if eligible is None:
        accumulator.eligibility_complete = False
        accumulator.ai_attempt_complete = False
        accumulator.ai_judgement_complete = False
        accumulator.asr_review_complete = False
        accumulator.reason("eligibility_unknown")
    else:
        accumulator.eligibility_known_turns += 1
        if eligible:
            accumulator.eligible_turns += 1

    audio_evidenced = _strict_bool(evidence.audio_evidenced)
    if audio_evidenced is None:
        accumulator.reason("audio_evidence_unknown")
    else:
        accumulator.audio_evidence_known_turns += 1
        if audio_evidenced:
            accumulator.audio_evidenced_turns += 1
        else:
            accumulator.reason("audio_evidence_missing")

    ai_attempted = _strict_bool(evidence.ai_attempted)
    if eligible is True:
        if ai_attempted is None:
            accumulator.ai_attempt_complete = False
            accumulator.ai_judgement_complete = False
            accumulator.reason("ai_attempt_status_unknown")
        else:
            accumulator.ai_attempt_status_known_turns += 1
            if ai_attempted:
                accumulator.ai_attempted_turns += 1
    elif ai_attempted is True:
        accumulator.ai_attempt_complete = False
        accumulator.ai_judgement_complete = False
        accumulator.reason("ai_attempt_on_ineligible_turn")
    if ai_attempted is True and audio_evidenced is not True:
        accumulator.ai_attempt_complete = False
        accumulator.ai_judgement_complete = False
        accumulator.reason("ai_attempt_without_audio_evidence")

    ai_judged = _strict_bool(evidence.ai_judged)
    if eligible is True and ai_attempted is True:
        if ai_judged is None:
            accumulator.ai_judgement_complete = False
            accumulator.reason("ai_judgement_status_unknown")
        else:
            accumulator.ai_judgement_status_known_turns += 1
            if ai_judged:
                accumulator.ai_judged_turns += 1
    elif ai_judged is True:
        accumulator.ai_judgement_complete = False
        accumulator.reason("ai_judgement_without_attempt")
    has_completed_attempt = (
        evidence.attempts is not None
        and any(attempt.processing_status == "completed"
                for attempt in evidence.attempts)
    )
    if ai_judged is True and not has_completed_attempt:
        accumulator.ai_judgement_complete = False
        accumulator.reason("ai_judgement_without_completed_attempt")

    ai_prediction = _strict_bool(evidence.ai_predicted_correct)
    if ai_judged is True and ai_prediction is None:
        accumulator.reason("ai_binary_prediction_unknown")
    elif ai_judged is not True and ai_prediction is not None:
        accumulator.reason("ai_prediction_without_judgement")

    asr_reviewed = _strict_bool(evidence.asr_reviewed)
    asr_corrected = _strict_bool(evidence.asr_corrected)
    if eligible is True:
        if asr_reviewed is None:
            accumulator.asr_review_complete = False
            accumulator.reason("asr_review_status_unknown")
        else:
            accumulator.asr_review_status_known_turns += 1
            if asr_reviewed:
                if asr_corrected is None:
                    accumulator.asr_review_complete = False
                    accumulator.reason("asr_correction_status_unknown")
                else:
                    accumulator.asr_reviewed_turns += 1
                    if asr_corrected:
                        accumulator.asr_corrected_turns += 1
            elif asr_corrected is True:
                accumulator.asr_review_complete = False
                accumulator.reason("asr_correction_without_review")
    elif asr_reviewed is True or asr_corrected is True:
        accumulator.asr_review_complete = False
        accumulator.reason("asr_review_on_ineligible_turn")

    truth_locked = _strict_bool(evidence.human_truth_locked)
    human_truth = _strict_bool(evidence.human_truth_correct)
    if truth_locked is True:
        accumulator.human_truth_locked_turns += 1
        if human_truth is not None:
            accumulator.human_truth_known_turns += 1
        else:
            accumulator.reason("locked_human_truth_label_unknown")
    elif human_truth is not None:
        accumulator.reason("human_truth_without_lock")
    else:
        accumulator.reason("human_truth_unavailable")

    if (eligible is True and ai_attempted is True and ai_judged is True
            and has_completed_attempt
            and ai_prediction is not None
            and truth_locked is True and human_truth is not None):
        accumulator.reviewed_decisions += 1
        accumulator.binary_comparison_eligible_turns += 1
        if ai_prediction and human_truth:
            accumulator.true_positive += 1
        elif not ai_prediction and not human_truth:
            accumulator.true_negative += 1
        elif ai_prediction and not human_truth:
            accumulator.false_positive += 1
        else:
            accumulator.false_negative += 1

    pause_count = _nonnegative_count(evidence.technical_pause_count)
    if pause_count is None:
        accumulator.pause_complete = False
        accumulator.reason(
            "technical_pause_count_unknown"
            if evidence.technical_pause_count is None
            else "technical_pause_count_invalid")
    else:
        accumulator.pause_count += pause_count

    takeover_count = _nonnegative_count(evidence.researcher_takeover_count)
    if takeover_count is None:
        accumulator.takeover_complete = False
        accumulator.reason(
            "researcher_takeover_count_unknown"
            if evidence.researcher_takeover_count is None
            else "researcher_takeover_count_invalid")
    else:
        accumulator.takeover_count += takeover_count

    score = evidence.operational_score
    if score is not None and (isinstance(score, bool)
                              or not isinstance(score, (int, float))
                              or not math.isfinite(score)):
        accumulator.reason("operational_score_invalid")

    _consume_attempts(accumulator, evidence.attempts)
    reasons_after = Counter(accumulator.unknown_reasons or {})
    new_codes = {
        code for code, count in reasons_after.items()
        if count > reasons_before.get(code, 0)
    }
    if new_codes:
        accumulator.unknown_turns += 1
    if new_codes & _INCONSISTENCY_CODES:
        accumulator.inconsistent_turns += 1


def _finalize(
    dimensions: QualityDimensions,
    accumulator: _Accumulator,
) -> QualityMetricRow:
    attempts_known = accumulator.attempts_complete
    prompt_counts = accumulator.prompt_counts or [0, 0, 0, 0]
    latencies = accumulator.latencies_ms or []
    operational = OperationalMetrics(
        eligible_turns=(accumulator.eligible_turns
                        if accumulator.eligibility_complete else None),
        ai_attempted_turns=(accumulator.ai_attempted_turns
                            if accumulator.ai_attempt_complete else None),
        ai_judged_turns=(accumulator.ai_judged_turns
                         if accumulator.ai_judgement_complete else None),
        asr_reviewed_turns=(accumulator.asr_reviewed_turns
                            if accumulator.asr_review_complete else None),
        asr_corrected_turns=(accumulator.asr_corrected_turns
                             if accumulator.asr_review_complete else None),
        total_attempts=(accumulator.attempts_observed
                        if attempts_known else None),
        prompt_level_0_count=(prompt_counts[0]
                              if attempts_known
                              and accumulator.prompt_counts_complete else None),
        prompt_level_1_count=(prompt_counts[1]
                              if attempts_known
                              and accumulator.prompt_counts_complete else None),
        prompt_level_2_count=(prompt_counts[2]
                              if attempts_known
                              and accumulator.prompt_counts_complete else None),
        prompt_level_3_count=(prompt_counts[3]
                              if attempts_known
                              and accumulator.prompt_counts_complete else None),
        technical_failure_attempts=(accumulator.technical_failures
                                    if attempts_known
                                    and accumulator.technical_status_complete
                                    else None),
        technical_pause_count=(accumulator.pause_count
                               if accumulator.pause_complete else None),
        researcher_takeover_count=(accumulator.takeover_count
                                   if accumulator.takeover_complete else None),
        latency_sample_count=(accumulator.latency_known_attempts
                              if attempts_known else None),
        latency_p50_ms=(_percentile(latencies, 0.50)
                        if attempts_known else None),
        latency_p95_ms=(_percentile(latencies, 0.95)
                        if attempts_known else None),
    )
    if accumulator.ai_judgement_complete:
        truth = ResearchTruthMetrics(
            reviewed_decisions=accumulator.reviewed_decisions,
            true_positive=accumulator.true_positive,
            true_negative=accumulator.true_negative,
            false_positive=accumulator.false_positive,
            false_negative=accumulator.false_negative,
        )
    else:
        truth = ResearchTruthMetrics(None, None, None, None, None)

    coverage = QualityCoverage(
        source_turns=accumulator.source_turns,
        turns_with_unknown_dimensions=accumulator.unknown_dimension_turns,
        eligibility_known_turns=accumulator.eligibility_known_turns,
        audio_evidence_known_turns=accumulator.audio_evidence_known_turns,
        audio_evidenced_turns=accumulator.audio_evidenced_turns,
        ai_attempt_status_known_turns=accumulator.ai_attempt_status_known_turns,
        ai_judgement_status_known_turns=(
            accumulator.ai_judgement_status_known_turns),
        asr_review_status_known_turns=accumulator.asr_review_status_known_turns,
        human_truth_locked_turns=accumulator.human_truth_locked_turns,
        human_truth_known_turns=accumulator.human_truth_known_turns,
        binary_comparison_eligible_turns=(
            accumulator.binary_comparison_eligible_turns),
        attempts_observed=accumulator.attempts_observed,
        prompt_level_known_attempts=accumulator.prompt_level_known_attempts,
        processing_status_known_attempts=(
            accumulator.processing_status_known_attempts),
        latency_known_attempts=accumulator.latency_known_attempts,
        turns_with_unknown_evidence=accumulator.unknown_turns,
        inconsistent_turns=accumulator.inconsistent_turns,
        unknown_reason_counts=tuple(sorted(
            (accumulator.unknown_reasons or {}).items())),
    )
    return QualityMetricRow(
        dimensions=dimensions,
        operational=operational,
        research_truth=truth,
        coverage=coverage,
        rates=_rates(operational, truth),
    )


def _row_sort_key(row: QualityMetricRow) -> tuple[str, ...]:
    return tuple(
        "" if getattr(row.dimensions, name) is None
        else str(getattr(row.dimensions, name))
        for name in DIMENSION_NAMES
    )


def aggregate_ai_quality(
    evidence: Iterable[TurnQualityEvidence],
    *,
    generated_at: datetime,
    group_by: Sequence[str] = DEFAULT_GROUP_BY,
    trusted_low_cardinality_values: Mapping[str, Iterable[str]] | None = None,
) -> QualityDashboard:
    """Aggregate deidentified turn evidence into dashboard-v1 metric rows.

    Missing grouping values form a real ``None`` group and are never dropped.
    Research and simulation are always separated even when callers omit
    ``data_classification`` from ``group_by``.
    """
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    canonical_group_by = _canonical_group_by(group_by)

    trusted: dict[str, set[str]] = {
        field_name: set() for field_name in _LOW_CARDINALITY_FIELDS
    }
    if trusted_low_cardinality_values is not None:
        unknown_fields = (
            set(trusted_low_cardinality_values) - set(_LOW_CARDINALITY_FIELDS)
        )
        if unknown_fields:
            raise ValueError(
                "unsupported trusted low-cardinality fields: "
                + ", ".join(sorted(unknown_fields)))
        for field_name in _LOW_CARDINALITY_FIELDS:
            raw_values = trusted_low_cardinality_values.get(field_name, ())
            if isinstance(raw_values, str):
                raise ValueError(
                    f"trusted {field_name} values must be a collection")
            for raw_value in raw_values:
                normalized = _safe_dimension_string(raw_value, field_name)
                if normalized is None:
                    raise ValueError(
                        f"trusted {field_name} contains an invalid value")
                trusted[field_name].add(normalized)
            if len(trusted[field_name]) > MAX_LOW_CARDINALITY_VALUES:
                raise ValueError(
                    f"trusted {field_name} exceeds the low-cardinality privacy limit")

    grouped: dict[tuple[object, ...], list[
        tuple[TurnQualityEvidence, QualityDimensions, tuple[str, ...]]
    ]] = defaultdict(list)
    cardinality: dict[str, set[str]] = {
        name: set() for name in _LOW_CARDINALITY_FIELDS
    }
    for row in evidence:
        if not isinstance(row, TurnQualityEvidence):
            raise TypeError("evidence rows must be TurnQualityEvidence")
        dimensions, dimension_reasons = _normalize_dimensions(row.dimensions)
        for field_name in _LOW_CARDINALITY_FIELDS:
            value = getattr(dimensions, field_name)
            if value is not None:
                if value not in trusted[field_name]:
                    raise ValueError(
                        f"{field_name} is not in the trusted aggregate allowlist")
                cardinality[field_name].add(value)
        key = tuple(getattr(dimensions, name) for name in canonical_group_by)
        grouped[key].append((row, dimensions, dimension_reasons))

    for field_name, values in cardinality.items():
        if len(values) > MAX_LOW_CARDINALITY_VALUES:
            raise ValueError(
                f"{field_name} exceeds the low-cardinality privacy limit")
    if len(grouped) > MAX_ROWS:
        raise ValueError("quality aggregation exceeds the 500-row privacy limit")

    result_rows: list[QualityMetricRow] = []
    for key, source_rows in grouped.items():
        projected_values: dict[str, object] = {
            name: None for name in DIMENSION_NAMES
        }
        for name, value in zip(canonical_group_by, key, strict=True):
            projected_values[name] = value
        projected_dimensions = QualityDimensions(**projected_values)
        accumulator = _Accumulator()
        for row, dimensions, dimension_reasons in source_rows:
            _consume_turn(accumulator, row, dimensions, dimension_reasons)
        result_rows.append(_finalize(projected_dimensions, accumulator))

    result_rows.sort(key=_row_sort_key)
    return QualityDashboard(
        generated_at=generated_at,
        rows=tuple(result_rows),
    )


def dashboard_payload_keys() -> dict[str, tuple[str, ...]]:
    """Expose the exact v1 projection fields for an HTTP integration layer."""
    return {
        "dimensions": tuple(field.name for field in fields(QualityDimensions)),
        "operational": tuple(field.name for field in fields(OperationalMetrics)),
        "research_truth": tuple(
            field.name for field in fields(ResearchTruthMetrics)),
    }
