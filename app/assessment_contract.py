"""Versioned, text-minimising contracts for formal outcome assessments.

This module contains no database or HTTP dependency.  It deliberately exposes
only protocol keys, immutable definition bindings, revisions and server-derived
results.  Instrument names, copyrighted item text and client-supplied totals do
not belong in the runtime contract.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)


ASSESSMENT_SCHEMA_VERSION = "formal-assessment.v1"
MAX_ASSESSMENT_JSON_BYTES = 64 * 1024
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
OPAQUE_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"

AssessmentTimepoint = Literal["pretest", "posttest", "followup"]
AssessmentCategoryKey = Literal[
    "untrained_standardized_naming",
    "functional_communication",
]
AssessmentEventStatus = Literal[
    "due", "in_progress", "awaiting_closeout", "closed", "cancelled"]
AssessmentInstanceStatus = Literal[
    "due", "in_progress", "completed", "approved_deferred"]
AssessmentDeferralReason = Literal[
    "participant_unavailable",
    "clinical_or_safety",
    "technical_failure",
    "authorized_reschedule",
]
AssessmentCancellationReason = Literal[
    "schedule_changed",
    "participant_unavailable",
    "protocol_correction",
    "duplicate_event",
]


def _assessment_utc_timestamp_json(value: datetime) -> str:
    """Serialize assessment timestamps as explicit UTC without migrating SQLite.

    Existing assessment rows use the platform's historical convention that a
    timezone-naive database value is UTC.  Keep that storage/comparison model
    intact, but make the HTTP contract unambiguous for strict clients.  Any
    future timezone-aware value is normalized to the same canonical ``Z`` form.
    """
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else (
        value.astimezone(timezone.utc))
    return aware.isoformat().replace("+00:00", "Z")


AssessmentUtcTimestamp = Annotated[
    datetime,
    PlainSerializer(
        _assessment_utc_timestamp_json,
        return_type=str,
        when_used="json",
    ),
    WithJsonSchema(
        {"type": "string", "format": "date-time", "pattern": "Z$"},
        mode="serialization",
    ),
]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssessmentItemSpec(StrictContract):
    """One opaque item key; content and response semantics stay in the registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_key: str = Field(min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)


class AssessmentDefinitionSpec(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_id: str = Field(
        min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    category_key: AssessmentCategoryKey
    instrument_id: str = Field(
        min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    instrument_version: str = Field(
        min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    definition_digest: str = Field(pattern=DIGEST_PATTERN)
    item_set_digest: str = Field(pattern=DIGEST_PATTERN)
    administration_protocol_digest: str = Field(pattern=DIGEST_PATTERN)
    response_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    missingness_rule_digest: str = Field(pattern=DIGEST_PATTERN)
    stopping_rule_digest: str = Field(pattern=DIGEST_PATTERN)
    scoring_algorithm_id: str = Field(
        min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    scoring_algorithm_version: str = Field(
        min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN)
    scoring_algorithm_digest: str = Field(pattern=DIGEST_PATTERN)
    score_min: float = Field(allow_inf_nan=False)
    score_max: float = Field(allow_inf_nan=False)
    score_direction: Literal["higher_is_better", "lower_is_better"]
    score_rounding_rule: str = Field(min_length=1, max_length=128)
    automatic_scoring_permitted: bool
    item_response_storage_permitted: bool
    result_storage_permitted: bool
    result_export_permitted: bool
    items: tuple[AssessmentItemSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_score_range(self):
        if self.score_min >= self.score_max:
            raise ValueError("score_min must be lower than score_max")
        return self

    @field_validator("items")
    @classmethod
    def unique_item_keys(cls, value: tuple[AssessmentItemSpec, ...]):
        keys = [item.item_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("assessment definition item keys must be unique")
        return value


class AssessmentDefinitionBundle(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(
        min_length=1, max_length=200, pattern=IDENTIFIER_PATTERN)
    bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    definitions: tuple[AssessmentDefinitionSpec, ...] = Field(min_length=2, max_length=2)
    # Synthetic fixtures exercise the platform contract without becoming a
    # formal outcome merely because every technical field is present.
    formal_research_approved: bool = False

    @field_validator("definitions")
    @classmethod
    def exact_frozen_categories(
        cls, value: tuple[AssessmentDefinitionSpec, ...]
    ) -> tuple[AssessmentDefinitionSpec, ...]:
        categories = [row.category_key for row in value]
        if set(categories) != {
            "untrained_standardized_naming",
            "functional_communication",
        } or len(set(categories)) != 2:
            raise ValueError("definition bundle must contain the two exact categories")
        return value


class AssessmentResponseValue(StrictContract):
    """Definition-specific JSON response; totals remain server-only."""

    value: Any = None
    # This is a server-issued, context-bound receipt digest, not the raw
    # recording's byte checksum.  The receipt must bind patient/event/instance/
    # item/revision and may be consumed by at most one response.
    authorized_artifact_digest: str | None = Field(
        default=None, pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def finite_json_and_some_evidence(self):
        import json

        if "value" not in self.model_fields_set and self.authorized_artifact_digest is None:
            raise ValueError("response value or authorized artifact digest is required")
        if "value" in self.model_fields_set:
            try:
                encoded = json.dumps(
                    self.value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("assessment response must be finite JSON") from exc
            if len(encoded) > MAX_ASSESSMENT_JSON_BYTES:
                raise ValueError("assessment response exceeds 64 KiB")
        return self


class CreateAssessmentEventIn(StrictContract):
    timepoint: AssessmentTimepoint
    scheduled_date: date
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=OPAQUE_KEY_PATTERN)


class StartAssessmentEventIn(StrictContract):
    expected_event_revision: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=OPAQUE_KEY_PATTERN)


class CancelAssessmentEventIn(StartAssessmentEventIn):
    reason_code: AssessmentCancellationReason


class SubmitAssessmentResponseIn(StrictContract):
    response: AssessmentResponseValue
    expected_event_revision: int = Field(ge=1)
    expected_instance_revision: int = Field(ge=1)
    expected_item_revision: int = Field(ge=0)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=OPAQUE_KEY_PATTERN)


class CompleteAssessmentInstanceIn(StrictContract):
    expected_event_revision: int = Field(ge=1)
    expected_instance_revision: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=OPAQUE_KEY_PATTERN)


class ApproveAssessmentDeferralIn(CompleteAssessmentInstanceIn):
    reason_code: AssessmentDeferralReason
    deferred_until: date


class CloseAssessmentEventIn(StrictContract):
    expected_event_revision: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8, max_length=200, pattern=OPAQUE_KEY_PATTERN)
    report_status: Literal[
        "no_additional_observation", "observation_recorded"]
    fatigue_observed: bool = False
    distress_or_discomfort_observed: bool = False
    participant_declined_to_continue: bool = False
    staff_assistance_occurred: bool = False
    environment_interruption_occurred: bool = False
    device_or_network_interruption_occurred: bool = False
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def closeout_is_consistent(self):
        flags = (
            self.fatigue_observed,
            self.distress_or_discomfort_observed,
            self.participant_declined_to_continue,
            self.staff_assistance_occurred,
            self.environment_interruption_occurred,
            self.device_or_network_interruption_occurred,
        )
        normalized_note = self.note.strip() if self.note is not None else None
        if self.note is not None and not normalized_note:
            raise ValueError("closeout note cannot be blank")
        self.note = normalized_note
        if self.report_status == "no_additional_observation":
            if any(flags) or self.note is not None:
                raise ValueError("no_additional_observation must be empty")
        elif not any(flags) and self.note is None:
            raise ValueError("observation_recorded requires a flag or note")
        return self


class AssessmentScoringEvidenceOut(StrictContract):
    evidence_id: str
    instance_id: str
    event_id: str
    patient_id: str
    category_key: AssessmentCategoryKey
    definition_digest: str
    item_response_set_digest: str
    scoring_algorithm_id: str
    scoring_algorithm_version: str
    scoring_algorithm_digest: str
    score: float = Field(allow_inf_nan=False)
    result: dict[str, Any]
    result_digest: str
    answered_item_count: int = Field(ge=0)
    missing_item_count: int = Field(ge=0)
    stopped_early: bool
    stopping_reason_code: str | None
    formal_outcome_eligible: bool
    scored_at: AssessmentUtcTimestamp


class AssessmentDeferralOut(StrictContract):
    deferral_id: str
    instance_id: str
    event_id: str
    patient_id: str
    category_key: AssessmentCategoryKey
    definition_digest: str
    reason_code: AssessmentDeferralReason
    deferred_until: date
    approved_by: str
    approved_role: Literal["admin", "local_m0"]
    approved_at: AssessmentUtcTimestamp


class AssessmentEventCloseoutOut(StrictContract):
    closeout_id: str
    event_id: str
    patient_id: str
    event_revision: int
    report_status: Literal[
        "no_additional_observation", "observation_recorded"]
    fatigue_observed: bool
    distress_or_discomfort_observed: bool
    participant_declined_to_continue: bool
    staff_assistance_occurred: bool
    environment_interruption_occurred: bool
    device_or_network_interruption_occurred: bool
    note: str | None
    closed_by: str
    closed_at: AssessmentUtcTimestamp
    switch_allowed: Literal[True] = True


class AssessmentEventCancellationOut(StrictContract):
    reason_code: AssessmentCancellationReason
    cancelled_by: str
    cancelled_at: AssessmentUtcTimestamp
    switch_allowed: Literal[True] = True


class AssessmentInstanceOut(StrictContract):
    instance_id: str
    event_id: str
    patient_id: str
    category_key: AssessmentCategoryKey
    definition_bundle_id: str
    definition_bundle_digest: str
    definition_id: str
    instrument_id: str
    instrument_version: str
    definition_digest: str
    item_set_digest: str
    administration_protocol_digest: str
    response_schema_digest: str
    result_schema_digest: str
    missingness_rule_digest: str
    stopping_rule_digest: str
    scoring_algorithm_id: str
    scoring_algorithm_version: str
    scoring_algorithm_digest: str
    score_min: float = Field(allow_inf_nan=False)
    score_max: float = Field(allow_inf_nan=False)
    score_direction: Literal["higher_is_better", "lower_is_better"]
    score_rounding_rule: str
    automatic_scoring_permitted: bool
    item_response_storage_permitted: bool
    result_storage_permitted: bool
    result_export_permitted: bool
    status: AssessmentInstanceStatus
    revision: int = Field(ge=1)
    item_response_count: int = Field(ge=0)
    required_item_count: int = Field(ge=1)
    data_classification: Literal["research", "simulation"]
    formal_outcome_eligible: bool
    scoring_evidence: AssessmentScoringEvidenceOut | None
    deferral: AssessmentDeferralOut | None
    created_at: AssessmentUtcTimestamp
    completed_at: AssessmentUtcTimestamp | None
    updated_at: AssessmentUtcTimestamp


class AssessmentEventOut(StrictContract):
    schema_version: Literal["formal-assessment.v1"] = ASSESSMENT_SCHEMA_VERSION
    event_id: str
    patient_id: str
    assigned_assessor_id: str
    timepoint: AssessmentTimepoint
    scheduled_date: date
    status: AssessmentEventStatus
    revision: int = Field(ge=1)
    is_simulation: bool
    data_classification: Literal["research", "simulation"]
    formal_outcome_eligible: bool
    definition_bundle_id: str
    definition_bundle_digest: str
    instances: list[AssessmentInstanceOut] = Field(min_length=2, max_length=2)
    closeout: AssessmentEventCloseoutOut | None
    cancellation: AssessmentEventCancellationOut | None
    created_at: AssessmentUtcTimestamp
    updated_at: AssessmentUtcTimestamp

    @field_validator("instances")
    @classmethod
    def exact_instance_categories(
        cls, value: list[AssessmentInstanceOut]
    ) -> list[AssessmentInstanceOut]:
        if {row.category_key for row in value} != {
            "untrained_standardized_naming",
            "functional_communication",
        }:
            raise ValueError("event must expose the two exact assessment instances")
        return value


class AssessmentTodayOut(StrictContract):
    as_of_date: date
    events: list[AssessmentEventOut]


JsonObject = dict[str, Any]
