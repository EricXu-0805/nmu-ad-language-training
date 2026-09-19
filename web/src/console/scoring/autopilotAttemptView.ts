// 「AI 听到了什么」面板的纯 view-model:从 journal 的 attempts 投影里取权威回执
// 位置(positionItemId/positionTurnSeq)上最新的一次回答。只做展示,不参与任何
// 控制判定;位置未知或该位置还没录到回答都返回 null,由控件决定怎么说。
import type { JournalAttempt } from "../../hooks/useSessionJournal";

export interface AutopilotPositionRef {
  itemId: string;
  turnSeq: number;
}

export type AutopilotHeard =
  | { kind: "pending" }
  | { kind: "silence" }
  | { kind: "text"; text: string };

export type AutopilotVerdict =
  | { kind: "pending" }
  | { kind: "failed"; errorCode: string | null }
  | { kind: "judged"; answerType: string; score: number | null; needsReview: boolean };

export interface AutopilotAttemptView {
  attemptSeq: number;
  promptLabel: string;
  heard: AutopilotHeard;
  verdict: AutopilotVerdict;
}

export function promptLevelLabel(level: number): string {
  return level <= 0 ? "自发" : `第 ${level} 级提示`;
}

/** 同一位置多次回答取 attempt_seq 最大的一次;同序号(不应出现)再按 id 取新。 */
export function latestAttemptAtPosition(
  attempts: readonly JournalAttempt[],
  position: AutopilotPositionRef | null,
): JournalAttempt | null {
  if (!position) return null;
  let latest: JournalAttempt | null = null;
  for (const attempt of attempts) {
    if (attempt.itemId !== position.itemId || attempt.turnSeq !== position.turnSeq) continue;
    if (!latest
        || attempt.attemptSeq > latest.attemptSeq
        || (attempt.attemptSeq === latest.attemptSeq && attempt.attemptId > latest.attemptId)) {
      latest = attempt;
    }
  }
  return latest;
}

export function autopilotAttemptView(
  attempts: readonly JournalAttempt[],
  position: AutopilotPositionRef | null,
): AutopilotAttemptView | null {
  const latest = latestAttemptAtPosition(attempts, position);
  if (!latest) return null;
  const text = latest.asrText?.trim() ?? "";
  // received = 还没转写;asr_completed 起才有权威转写可看。
  const heard: AutopilotHeard = latest.processingStatus === "received"
    ? { kind: "pending" }
    : text ? { kind: "text", text } : { kind: "silence" };
  const verdict: AutopilotVerdict = latest.processingStatus === "technical_failure"
    ? { kind: "failed", errorCode: latest.errorCode }
    : latest.processingStatus !== "completed" || latest.answerType === null
      ? { kind: "pending" }
      : {
        kind: "judged",
        answerType: latest.answerType,
        score: latest.score,
        needsReview: latest.needsReview === true,
      };
  return {
    attemptSeq: latest.attemptSeq,
    promptLabel: promptLevelLabel(latest.promptLevel),
    heard,
    verdict,
  };
}
