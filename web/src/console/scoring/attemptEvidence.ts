import type { AttemptEvent, AttemptProcessRequest, AttemptProcessResult } from "../../types";

export type AttemptProcessDecision =
  | { kind: "completed"; attempt: AttemptEvent }
  | { kind: "technical_failure"; attempt: AttemptEvent; errorCode: string }
  | { kind: "invalid"; message: string };

function sameNullable<T>(left: T | null | undefined, right: T | null | undefined): boolean {
  return (left ?? null) === (right ?? null);
}

export function cueTypeForPrompt(level: number): string | null {
  if (level <= 0) return null;
  if (level === 1) return "prompt_level_1";
  if (level === 2) return "prompt_level_2";
  return "tell_answer";
}

/** Runtime response guard: no UI branch may trust a mismatched or portrait-tainted attempt. */
export function decideAttemptProcessResult(
  result: AttemptProcessResult,
  request: AttemptProcessRequest,
  sessionId: string,
  expectedSimulation: boolean,
): AttemptProcessDecision {
  const attempt = result?.attempt;
  const interactions = result?.interactions;
  if (!attempt || result.truth_scope !== "operational_only") {
    return { kind: "invalid", message: "逐次处理未返回 operational-only 证据" };
  }
  if (attempt.session_id !== sessionId
      || attempt.item_id !== request.item_id
      || attempt.turn_seq !== request.turn_seq
      || attempt.response_role !== request.response_role
      || attempt.raw_audio_id !== request.raw_audio_id
      || attempt.prompt_level !== request.prompt_level
      || !sameNullable(attempt.cue_type, request.cue_type)
      || !sameNullable(attempt.duration_seconds, request.duration_seconds)
      || attempt.is_simulation !== expectedSimulation) {
    return { kind: "invalid", message: "逐次处理回执与本场次录音位置或提示上下文不一致" };
  }
  if (!Number.isInteger(attempt.id) || attempt.id <= 0
      || !Number.isInteger(attempt.attempt_seq) || attempt.attempt_seq <= 0
      || attempt.judge_portrait_used !== false
      || result.status !== attempt.processing_status
      || !Array.isArray(interactions)
      || interactions.some((event) => event.session_id !== sessionId
        || event.attempt_id !== attempt.id
        || event.is_simulation !== expectedSimulation)) {
    return { kind: "invalid", message: "逐次处理回执违反画像禁入或状态一致性约束" };
  }
  const evidenceTypes = new Set(interactions.map((event) => event.event_type));
  if (!evidenceTypes.has("attempt_received")) {
    return { kind: "invalid", message: "逐次处理回执缺少 attempt_received 证据" };
  }
  if (result.status === "technical_failure") {
    return typeof attempt.error_code === "string" && attempt.error_code.length > 0
        && evidenceTypes.has("technical_pause")
      ? { kind: "technical_failure", attempt, errorCode: attempt.error_code }
      : { kind: "invalid", message: "技术失败回执缺少 error_code 或 technical_pause 证据" };
  }
  if (result.status !== "completed") {
    return { kind: "invalid", message: `逐次处理停在未完成状态 ${result.status}` };
  }
  if (typeof attempt.asr_text !== "string"
      || typeof attempt.operational_answer_type !== "string"
      || typeof attempt.operational_needs_review !== "boolean"
      || !evidenceTypes.has("asr_completed")
      || !evidenceTypes.has("judgement_completed")) {
    return { kind: "invalid", message: "已完成 attempt 缺少权威转写或 operational 判类" };
  }
  return { kind: "completed", attempt };
}

export function autopilotFailureKindForErrorCode(errorCode: string): "asr" | "classifier" | "persistence" {
  if (errorCode.startsWith("asr_")) return "asr";
  if (errorCode.startsWith("judgement_")) return "classifier";
  return "persistence";
}
