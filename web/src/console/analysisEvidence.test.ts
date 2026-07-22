import assert from "node:assert/strict";
import test from "node:test";
import type { AttemptEvent, AudioAsset, InteractionEvent, ItemEvent, Session, TurnEvent } from "../types.ts";
import { buildEvidenceTimeline, compareOperationalToResearch, interactionSummary } from "./analysisEvidence.ts";

const session: Session = {
  session_id: "S1",
  patient_id: "P1",
  is_simulation: false,
  data_classification: "research",
  week_no: 2,
  phase_type: "正式训练",
  event_line: "正式训练",
  item_bank_version_id: "bank-1",
};

const item: ItemEvent = {
  id: 3,
  session_id: "S1",
  item_id: "SE_1",
  task_type: "单要素",
  item_set_type: "训练集",
  presentation_order: 1,
};

const audio: AudioAsset = {
  raw_audio_id: "audio-1",
  turn_key: "SE_1#1",
  session_id: "S1",
  is_simulation: false,
  data_classification: "research",
  audio_format: "webm",
  status: "recorded",
  is_reliability_sample: false,
  withdrawn: false,
  contains_direct_identifier: false,
};

function attempt(overrides: Partial<AttemptEvent> = {}): AttemptEvent {
  return {
    id: 7,
    session_id: "S1",
    item_id: "SE_1",
    turn_seq: 1,
    response_role: "命名",
    attempt_seq: 1,
    raw_audio_id: "audio-1",
    prompt_level: 0,
    cue_type: null,
    duration_seconds: 2.5,
    asr_text: "苹果",
    asr_confidence: 0.91,
    asr_engine_version: "asr-v1",
    operational_answer_type: "正确",
    operational_score: 1,
    operational_needs_review: false,
    judge_mode: "规则确定式",
    judge_engine_version: "rule-1",
    judge_reason: null,
    matched_on: "target",
    contains_target: true,
    judge_portrait_used: false,
    processing_status: "completed",
    created_at: "2026-07-18T01:00:00",
    processed_at: "2026-07-18T01:00:03",
    is_simulation: false,
    ...overrides,
  };
}

function turn(overrides: Partial<TurnEvent> = {}): TurnEvent {
  return {
    id: 9,
    item_event_id: 3,
    source_attempt_id: 7,
    turn_seq: 1,
    response_role: "命名",
    raw_audio_id: "audio-1",
    asr_text: "苹果",
    asr_confidence: 0.91,
    confirmed_response_text: "苹果",
    duration_seconds: 2.5,
    prompt_level: 0,
    cue_type: null,
    ai_answer_type: "正确",
    ai_score: 1,
    ai_needs_review: false,
    ai_judge_mode: "规则确定式",
    judge_portrait_used: false,
    reviewer_id: "R1",
    reviewed_score: 1,
    score_locked: true,
    element_value: 1,
    ...overrides,
  };
}

function interaction(event_seq: number, event_type: string, payload: object, overrides: Partial<InteractionEvent> = {}): InteractionEvent {
  return {
    id: event_seq,
    session_id: "S1",
    event_seq,
    item_id: "SE_1",
    turn_seq: 1,
    attempt_id: 7,
    attempt_seq: 1,
    event_type,
    payload_json: JSON.stringify(payload),
    created_at: `2026-07-18T01:00:0${event_seq}`,
    is_simulation: false,
    ...overrides,
  };
}

const healthyEvents = [
  interaction(1, "attempt_received", { raw_audio_id: "audio-1", prompt_level: 0, processing_status: "received" }),
  interaction(2, "asr_completed", { asr_engine_version: "asr-v1", asr_confidence: 0.91, degraded: false }),
  interaction(3, "judgement_completed", {
    answer_type: "正确", score: 1, needs_review: false, judge_mode: "规则确定式",
    judge_engine_version: "rule-1", matched_on: "target", contains_target: true,
    truth_scope: "operational_only",
  }),
];

test("a complete source attempt and locked human truth form a traceable timeline", () => {
  const timeline = buildEvidenceTimeline({
    session,
    items: [item],
    turns: [turn()],
    audios: [audio],
    attempts: [attempt()],
    interactions: healthyEvents,
  });
  assert.equal(timeline.turns.length, 1);
  assert.equal(timeline.turns[0]?.attempts[0]?.isTurnSource, true);
  assert.equal(timeline.turns[0]?.comparison.state, "same");
  assert.equal(timeline.summary.completedAttempts, 1);
  assert.equal(timeline.summary.lockedTruths, 1);
  assert.equal(timeline.summary.dangerIssues, 0);
  assert.match(timeline.turns[0]?.attempts[0]?.interactions[2]?.summary ?? "", /operational/);
});

test("technical failure stays distinct from a wrong answer and requires safe-pause evidence", () => {
  const failedAttempt = attempt({
    processing_status: "technical_failure",
    asr_text: null,
    asr_confidence: null,
    operational_answer_type: null,
    operational_score: null,
    operational_needs_review: null,
    error_code: "asr_degraded",
  });
  const timeline = buildEvidenceTimeline({
    session,
    items: [item],
    turns: [],
    audios: [audio],
    attempts: [failedAttempt],
    interactions: [
      healthyEvents[0]!,
      interaction(2, "asr_failed", { asr_engine_version: "asr-v1", error_code: "asr_degraded" }),
    ],
  });
  const messages = timeline.turns[0]?.attempts[0]?.issues.map((row) => row.message).join(" ") ?? "";
  assert.equal(timeline.summary.technicalFailures, 1);
  assert.match(messages, /不代表受试者回答错误/);
  assert.match(messages, /technical_pause/);
});

test("classification, source and ledger mismatches are surfaced rather than repaired", () => {
  const timeline = buildEvidenceTimeline({
    session,
    items: [item],
    turns: [turn({ source_attempt_id: 999 })],
    audios: [{ ...audio, data_classification: "simulation", is_simulation: true }],
    attempts: [attempt({ is_simulation: true })],
    interactions: [
      { ...healthyEvents[0]!, event_seq: 2, is_simulation: true, payload_json: "not-json" },
    ],
  });
  const codes = [
    ...timeline.issues,
    ...timeline.turns.flatMap((row) => [...row.issues, ...row.attempts.flatMap((entry) => entry.issues)]),
  ].map((row) => row.code);
  assert.ok(codes.includes("event_sequence_gap"));
  assert.ok(codes.includes("source_attempt_not_found"));
  assert.ok(codes.includes("audio_boundary_mismatch"));
  assert.ok(codes.includes("attempt_boundary_mismatch"));
  assert.ok(codes.includes("payload_invalid"));
});

test("AI operational and locked research scores are compared without equating their meaning", () => {
  const different = compareOperationalToResearch(turn({ element_value: 0, reviewed_score: 0 }), attempt());
  assert.equal(different.state, "different");
  assert.match(different.message, /AI operational 1/);
  const unlocked = compareOperationalToResearch(turn({ score_locked: false, element_value: null, reviewed_score: null }), attempt());
  assert.equal(unlocked.state, "unavailable");
  assert.match(unlocked.message, /不可宣称/);
});

test("interaction summaries expose the actual cue and feedback selection", () => {
  assert.match(interactionSummary("cue_selected", { prompt_level: 2, cue_type: "prompt_level_2" }), /2 级/);
  assert.match(interactionSummary("feedback_selected", { feedback_key: "cued1_silence" }), /沉默路径/);
});
