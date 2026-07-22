"""Unit contract for text-free session summaries and closeout payloads."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json

import pytest

from app.session_closeout import (
    CLOSEOUT_FLAG_FIELDS,
    CLOSEOUT_SCHEMA_VERSION,
    CloseoutValidationError,
    build_session_outcome_summary,
    closeout_operation_hash,
    closeout_request_hash,
    normalize_closeout_payload,
)
from app.models import SessionCloseoutReport


@dataclass(frozen=True)
class TurnEvidence:
    item_id: str
    turn_seq: int
    matched: bool
    audio_evidenced: bool
    # Deliberately present to prove the summary projection never reads it.
    confirmed_response_text: str = ""


@dataclass(frozen=True)
class AttemptEvidence:
    item_id: str
    turn_seq: int
    attempt_seq: int
    raw_audio_id: str
    prompt_level: int
    processing_status: str
    operational_needs_review: bool | None
    # Deliberately outside the protocol: neither value may enter the digest.
    asr_text: str = ""
    judge_reason: str = ""


@dataclass(frozen=True)
class InteractionEvidence:
    event_seq: int
    event_type: str
    item_id: str | None = None
    turn_seq: int | None = None
    attempt_seq: int | None = None
    # Arbitrary event payloads are likewise excluded.
    payload_json: str = "{}"


GENERATED_AT = datetime(2026, 7, 19, 10, 30, 0)


def _evidence():
    turns = [
        TurnEvidence("A", 1, True, True, "苹果"),
        TurnEvidence("B", 1, True, True, "杯子"),
        TurnEvidence("B", 2, True, False, "用杯子喝水"),
    ]
    attempts = [
        AttemptEvidence("A", 1, 1, "audio-a", 0, "completed", False,
                        "苹果", "匹配目标词"),
        AttemptEvidence("B", 1, 1, "audio-b-1", 1, "received", None,
                        "杯纸", ""),
        AttemptEvidence("B", 1, 2, "audio-b-2", 2, "completed", True,
                        "杯子", "二次尝试"),
        AttemptEvidence("B", 2, 1, "audio-b-3", 3,
                        "technical_failure", True, "", "网络中断"),
    ]
    interactions = [
        InteractionEvidence(1, "attempt_received", "A", 1, 1,
                            '{"response":"苹果"}'),
        InteractionEvidence(2, "technical_pause", "B", 1, 1,
                            '{"note":"需要协助"}'),
        InteractionEvidence(3, "technical_pause", "B", 2, 1),
        InteractionEvidence(4, "researcher_takeover", "B", 2, 1),
    ]
    return turns, attempts, interactions


def _build(turns, attempts, interactions):
    return build_session_outcome_summary(
        session_id="S-CLOSEOUT-1",
        schema_version="outcome-v1",
        generator_version="generator-v1",
        item_bank_version_id="bank-v1",
        is_simulation=False,
        data_classification="research",
        turn_evidence=turns,
        attempts=attempts,
        interactions=interactions,
        generated_at=GENERATED_AT,
    )


def test_summary_is_deterministic_text_free_and_counts_all_evidence():
    turns, attempts, interactions = _evidence()
    first = _build(turns, attempts, interactions)

    changed_text_turns = [
        replace(row, confirmed_response_text="完全不同的回答")
        for row in reversed(turns)
    ]
    changed_text_attempts = [
        replace(row, asr_text="敏感回答文本", judge_reason="长判断理由")
        for row in reversed(attempts)
    ]
    changed_text_interactions = [
        replace(row, payload_json='{"response":"另一段文本"}')
        for row in reversed(interactions)
    ]
    second = _build(
        changed_text_turns, changed_text_attempts, changed_text_interactions)

    assert first == second
    assert first.expected_turns == 3
    assert first.matched_turns == 3
    assert first.completed_attempt_turns == 2
    assert first.audio_evidenced_turns == 2
    assert first.total_attempts == 4
    assert first.completed_attempts == 2
    assert first.needs_review_attempts == 2
    assert first.technical_failure_attempts == 1
    assert (
        first.prompt_level_0_count,
        first.prompt_level_1_count,
        first.prompt_level_2_count,
        first.prompt_level_3_count,
    ) == (1, 1, 1, 1)
    assert first.technical_pause_count == 2
    assert first.researcher_takeover_count == 1
    assert len(first.source_digest) == 64
    assert set(first.source_digest) <= set("0123456789abcdef")

    serialized = json.dumps(first.to_dict(), ensure_ascii=False, default=str)
    for forbidden in (
        "苹果", "杯子", "敏感回答文本", "长判断理由",
        "需要协助", "response", "judge_reason", "asr_text",
    ):
        assert forbidden not in serialized


def test_summary_digest_changes_only_for_canonical_non_text_evidence():
    turns, attempts, interactions = _evidence()
    baseline = _build(turns, attempts, interactions)
    changed_audio = list(attempts)
    changed_audio[0] = replace(changed_audio[0], raw_audio_id="audio-a-v2")

    assert _build(turns, changed_audio, interactions).source_digest != baseline.source_digest


@pytest.mark.parametrize(
    "mutation, message",
    [
        ({"status": "unknown"}, "unsupported"),
        ({"status": "no_additional_observation", "fatigue_observed": True},
         "cannot contain"),
        ({"status": "no_additional_observation", "note": "有记录"},
         "cannot contain"),
        ({"status": "observation_recorded"}, "requires"),
        ({"status": "observation_recorded", "fatigue_observed": 1},
         "boolean"),
        ({"status": "observation_recorded", "note": "x" * 2001},
         "2000"),
        ({"status": "observation_recorded", "note": "记录", "extra": True},
         "unknown"),
    ],
)
def test_closeout_validation_fails_closed(mutation, message):
    with pytest.raises(CloseoutValidationError, match=message):
        normalize_closeout_payload(mutation)


def test_closeout_normalization_and_request_hash_are_canonical():
    schema_column = SessionCloseoutReport.__table__.c.schema_version
    assert CLOSEOUT_SCHEMA_VERSION == "session-closeout.v1"
    assert schema_column.nullable is False
    assert schema_column.default is None

    minimal = normalize_closeout_payload({
        "status": "  no_additional_observation  ",
        "note": "  ",
    })
    assert minimal.status == "no_additional_observation"
    assert minimal.note is None
    assert all(minimal.to_dict()[field] is False for field in CLOSEOUT_FLAG_FIELDS)

    explicit = {
        "status": "no_additional_observation",
        "note": None,
        **{field: False for field in CLOSEOUT_FLAG_FIELDS},
    }
    assert closeout_request_hash(minimal) == closeout_request_hash(explicit)

    # NFC normalisation makes canonically equivalent notes idempotent.
    decomposed = {
        "status": "observation_recorded",
        "note": "  Cafe\u0301  ",
    }
    composed = {
        "status": "observation_recorded",
        "note": "Café",
    }
    request_hash = closeout_request_hash(decomposed)
    assert request_hash == closeout_request_hash(composed)
    assert len(request_hash) == 64
    assert set(request_hash) <= set("0123456789abcdef")

    changed = {
        "status": "observation_recorded",
        "note": "Café",
        "fatigue_observed": True,
    }
    assert closeout_request_hash(changed) != request_hash


def test_closeout_operation_hash_includes_expected_revision():
    payload = {
        "status": "observation_recorded",
        "fatigue_observed": True,
    }

    assert closeout_operation_hash(payload, expected_revision=0) \
        != closeout_operation_hash(payload, expected_revision=1)
