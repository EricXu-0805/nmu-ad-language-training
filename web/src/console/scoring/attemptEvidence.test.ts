import assert from "node:assert/strict";
import test from "node:test";
import type { AttemptEvent, AttemptProcessRequest, AttemptProcessResult, InteractionEvent } from "../../types.ts";
import {
  autopilotFailureKindForErrorCode,
  cueTypeForPrompt,
  decideAttemptProcessResult,
} from "./attemptEvidence.ts";

const request: AttemptProcessRequest = {
  item_id: "SE_1",
  turn_seq: 1,
  response_role: "命名",
  raw_audio_id: "aud-1",
  prompt_level: 1,
  cue_type: "prompt_level_1",
  duration_seconds: 2.5,
};

function attempt(overrides: Partial<AttemptEvent> = {}): AttemptEvent {
  return {
    id: 7,
    session_id: "S1",
    item_id: request.item_id,
    turn_seq: request.turn_seq,
    response_role: request.response_role,
    attempt_seq: 1,
    raw_audio_id: request.raw_audio_id,
    prompt_level: request.prompt_level,
    cue_type: request.cue_type,
    duration_seconds: request.duration_seconds,
    asr_text: "苹果",
    operational_answer_type: "正确",
    operational_score: 1,
    operational_needs_review: false,
    contains_target: true,
    judge_portrait_used: false,
    processing_status: "completed",
    created_at: "2026-07-18T00:00:00",
    is_simulation: false,
    ...overrides,
  };
}

function result(row: AttemptEvent): AttemptProcessResult {
  const eventTypes = row.processing_status === "technical_failure"
    ? ["attempt_received", "technical_pause"]
    : row.processing_status === "completed"
      ? ["attempt_received", "asr_completed", "judgement_completed"]
      : ["attempt_received"];
  return {
    status: row.processing_status,
    idempotent: false,
    truth_scope: "operational_only",
    attempt: row,
    interactions: eventTypes.map((event_type, index): InteractionEvent => ({
      id: index + 1,
      session_id: row.session_id,
      event_seq: index + 1,
      item_id: row.item_id,
      turn_seq: row.turn_seq,
      attempt_id: row.id,
      attempt_seq: row.attempt_seq,
      event_type,
      payload_json: "{}",
      created_at: "2026-07-18T00:00:00",
      is_simulation: row.is_simulation,
    })),
  };
}

test("completed operational attempt is accepted only for the exact request", () => {
  assert.equal(decideAttemptProcessResult(result(attempt()), request, "S1", false).kind, "completed");
  assert.equal(decideAttemptProcessResult(result(attempt({ raw_audio_id: "aud-other" })), request, "S1", false).kind, "invalid");
  assert.equal(decideAttemptProcessResult(result(attempt({ judge_portrait_used: true })), request, "S1", false).kind, "invalid");
  assert.equal(decideAttemptProcessResult(result(attempt({ is_simulation: true })), request, "S1", false).kind, "invalid");
});

test("technical failure and partial server states remain fail-closed", () => {
  const failed = attempt({ processing_status: "technical_failure", error_code: "asr_degraded", asr_text: null });
  assert.deepEqual(decideAttemptProcessResult(result(failed), request, "S1", false), {
    kind: "technical_failure",
    attempt: failed,
    errorCode: "asr_degraded",
  });
  assert.equal(decideAttemptProcessResult(result(attempt({ processing_status: "received" })), request, "S1", false).kind, "invalid");
});

test("a completed status without the persisted evidence chain is rejected", () => {
  const broken = result(attempt());
  broken.interactions = broken.interactions.filter((event) => event.event_type !== "judgement_completed");
  assert.equal(decideAttemptProcessResult(broken, request, "S1", false).kind, "invalid");
});

test("cue and error codes map to stable ledger-safe values", () => {
  assert.deepEqual([0, 1, 2, 3].map(cueTypeForPrompt), [null, "prompt_level_1", "prompt_level_2", "tell_answer"]);
  assert.equal(autopilotFailureKindForErrorCode("asr_degraded"), "asr");
  assert.equal(autopilotFailureKindForErrorCode("judgement_exception"), "classifier");
  assert.equal(autopilotFailureKindForErrorCode("unknown"), "persistence");
});
