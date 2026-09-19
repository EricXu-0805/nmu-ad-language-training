import assert from "node:assert/strict";
import test from "node:test";
import type { JournalAttempt } from "../../hooks/useSessionJournal.ts";
import {
  autopilotAttemptView,
  latestAttemptAtPosition,
  promptLevelLabel,
} from "./autopilotAttemptView.ts";

function attempt(patch: Partial<JournalAttempt> = {}): JournalAttempt {
  return {
    attemptId: 101,
    itemId: "SE_螺母",
    turnSeq: 1,
    attemptSeq: 1,
    promptLevel: 0,
    asrText: "刘世茂",
    answerType: "错误",
    score: 0,
    needsReview: false,
    processingStatus: "completed",
    errorCode: null,
    createdAt: "2026-09-17T02:00:00Z",
    ...patch,
  };
}

const POSITION = { itemId: "SE_螺母", turnSeq: 1 };

test("提示等级标签:0 = 自发,其余按级数", () => {
  assert.equal(promptLevelLabel(0), "自发");
  assert.equal(promptLevelLabel(1), "第 1 级提示");
  assert.equal(promptLevelLabel(3), "第 3 级提示");
});

test("只取当前位置的回答,多次回答取 attempt_seq 最大的一次", () => {
  const rows = [
    attempt({ attemptId: 101, attemptSeq: 1 }),
    attempt({ attemptId: 103, attemptSeq: 2, promptLevel: 1, asrText: "螺母", answerType: "正确", score: 1 }),
    attempt({ attemptId: 102, itemId: "SE_茶杯", attemptSeq: 5 }),
    attempt({ attemptId: 104, turnSeq: 2, attemptSeq: 9 }),
  ];
  assert.equal(latestAttemptAtPosition(rows, POSITION)?.attemptId, 103);
  assert.equal(latestAttemptAtPosition(rows, { itemId: "SE_茶杯", turnSeq: 1 })?.attemptId, 102);
  assert.equal(latestAttemptAtPosition(rows, { itemId: "SE_书", turnSeq: 1 }), null);
  assert.equal(latestAttemptAtPosition(rows, null), null);
  // 同序号(不应出现)按 id 取新,不按数组顺序。
  assert.equal(latestAttemptAtPosition([
    attempt({ attemptId: 202, attemptSeq: 3 }),
    attempt({ attemptId: 201, attemptSeq: 3 }),
  ], POSITION)?.attemptId, 202);
});

test("已完成的回答:识别原文 + AI 判类与分数;提示等级从 attempt 自己的 prompt_level 来", () => {
  assert.deepEqual(autopilotAttemptView([
    attempt({ attemptSeq: 2, promptLevel: 2, asrText: " 螺母 ", answerType: "正确", score: 1 }),
  ], POSITION), {
    attemptSeq: 2,
    promptLabel: "第 2 级提示",
    heard: { kind: "text", text: "螺母" },
    verdict: { kind: "judged", answerType: "正确", score: 1, needsReview: false },
  });
  // 2026-09-17 实测的误识别原样展示,不替 AI 改字。
  assert.deepEqual(autopilotAttemptView([attempt()], POSITION)?.heard, { kind: "text", text: "刘世茂" });
  assert.deepEqual(
    autopilotAttemptView([attempt({ needsReview: true, score: null })], POSITION)?.verdict,
    { kind: "judged", answerType: "错误", score: null, needsReview: true },
  );
});

test("完成但转写为空 = 没有识别到语音;received/asr_completed 分别是转写中与判分中", () => {
  assert.deepEqual(autopilotAttemptView([
    attempt({ asrText: "", answerType: "沉默", score: 0 }),
  ], POSITION)?.heard, { kind: "silence" });
  assert.deepEqual(autopilotAttemptView([
    attempt({ asrText: null, answerType: "沉默" }),
  ], POSITION)?.heard, { kind: "silence" });
  const received = autopilotAttemptView([
    attempt({ processingStatus: "received", asrText: null, answerType: null, score: null }),
  ], POSITION);
  assert.deepEqual(received?.heard, { kind: "pending" });
  assert.deepEqual(received?.verdict, { kind: "pending" });
  const asrDone = autopilotAttemptView([
    attempt({ processingStatus: "asr_completed", asrText: "查呗", answerType: null, score: null }),
  ], POSITION);
  assert.deepEqual(asrDone?.heard, { kind: "text", text: "查呗" });
  assert.deepEqual(asrDone?.verdict, { kind: "pending" });
  // completed 却没有判类:仍算判分中,不编一个判类出来。
  assert.deepEqual(autopilotAttemptView([
    attempt({ answerType: null }),
  ], POSITION)?.verdict, { kind: "pending" });
});

test("技术失败不显示成判分中,带错误码", () => {
  assert.deepEqual(autopilotAttemptView([
    attempt({ processingStatus: "technical_failure", asrText: null, answerType: null, score: null, errorCode: "asr_timeout" }),
  ], POSITION)?.verdict, { kind: "failed", errorCode: "asr_timeout" });
  assert.equal(autopilotAttemptView([], POSITION), null);
});
