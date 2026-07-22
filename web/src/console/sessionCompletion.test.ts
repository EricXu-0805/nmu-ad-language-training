import assert from "node:assert/strict";
import test from "node:test";
import {
  completionIssueLabel,
  localCompletionGate,
  localInterventionCompletionGate,
  parseCompletionFailure,
} from "./sessionCompletion.ts";

test("completion stays closed until journal and plan are loaded", () => {
  assert.equal(localCompletionGate({
    journalReady: false,
    planReady: true,
    weekNo: 3,
    lockedTurns: 12,
    totalTurns: 12,
  }).canRequest, false);
});

test("intervention completion waits for evidence loading but not human score locks", () => {
  assert.equal(localInterventionCompletionGate({
    journalReady: false,
    planReady: true,
    weekNo: 3,
    totalTurns: 12,
  }).canRequest, false);
  assert.equal(localInterventionCompletionGate({
    journalReady: true,
    planReady: true,
    weekNo: 3,
    totalTurns: 12,
  }).canRequest, true);
});

test("intervention completion keeps empty and week-one plans closed", () => {
  const common = { journalReady: true, planReady: true };
  assert.equal(localInterventionCompletionGate({
    ...common, weekNo: 1, totalTurns: 0,
  }).canRequest, false);
  assert.equal(localInterventionCompletionGate({
    ...common, weekNo: 3, totalTurns: 0,
  }).canRequest, false);
});

test("week one cannot be falsely completed from an empty scoring plan", () => {
  const gate = localCompletionGate({
    journalReady: true,
    planReady: true,
    weekNo: 1,
    lockedTurns: 0,
    totalTurns: 0,
  });
  assert.equal(gate.canRequest, false);
  assert.match(gate.label, /关系建立/);
});

test("completion requires a positive plan and an exact locked-turn count", () => {
  const common = { journalReady: true, planReady: true, weekNo: 3 };
  assert.equal(localCompletionGate({ ...common, lockedTurns: 0, totalTurns: 0 }).canRequest, false);
  assert.equal(localCompletionGate({ ...common, lockedTurns: 11, totalTurns: 12 }).canRequest, false);
  assert.equal(localCompletionGate({ ...common, lockedTurns: 13, totalTurns: 12 }).canRequest, false);
  assert.equal(localCompletionGate({ ...common, lockedTurns: 12, totalTurns: 12 }).canRequest, true);
});

test("structured completion rejection keeps counts and actionable issues", () => {
  const failure = parseCompletionFailure({
    detailData: {
      message: "场次尚未满足完成条件",
      assessment: {
        ready: false,
        expected_turns: 12,
        matched_turns: 11,
        locked_turns: 10,
        completed_attempt_turns: 11,
        audio_evidenced_turns: 11,
        issues: [{
          code: "unlocked_turn",
          detail: "环节尚无锁定研究真值",
          item_id: "SE_W3_01",
          turn_seq: 1,
          response_role: "命名",
        }],
      },
    },
  });
  assert.equal(failure.message, "场次尚未满足完成条件");
  assert.equal(failure.assessment?.expectedTurns, 12);
  assert.equal(failure.assessment?.completedAttemptTurns, 11);
  assert.equal(failure.assessment?.audioEvidencedTurns, 11);
  assert.equal(failure.assessment?.issues[0]?.code, "unlocked_turn");
  assert.equal(
    completionIssueLabel(failure.assessment!.issues[0]!),
    "SE_W3_01 · 第 1 环节 · 命名：环节尚无锁定研究真值",
  );
});

test("completion rejection also accepts JSON and plain string details", () => {
  const json = parseCompletionFailure({
    detail: JSON.stringify({
      message: "记录不一致",
      assessment: { ready: false, issues: ["发现重复环节"] },
    }),
  });
  assert.equal(json.message, "记录不一致");
  assert.equal(json.assessment?.issues[0]?.detail, "发现重复环节");
  assert.equal(parseCompletionFailure({ detail: "服务暂时不可用" }).message, "服务暂时不可用");
});
