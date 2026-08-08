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

test("intervention completion keeps empty plans closed for training weeks", () => {
  const common = { journalReady: true, planReady: true };
  assert.equal(localInterventionCompletionGate({
    ...common, weekNo: 3, totalTurns: 0,
  }).canRequest, false);
});

test("week one follows the rapport farewell gate, never the empty scoring plan", () => {
  const common = {
    journalReady: true, planReady: true, weekNo: 1, lockedTurns: 0, totalTurns: 0,
  };
  // 位置未核对 → fail-closed。
  const unknown = localCompletionGate({ ...common, rapport: null });
  assert.equal(unknown.canRequest, false);
  assert.match(unknown.label, /关系建立位置/);
  assert.equal(localInterventionCompletionGate({
    journalReady: true, planReady: true, weekNo: 1, totalTurns: 0, rapport: null,
  }).canRequest, false);
  // 未停在道别节 → 指路道别/中止,不放行。
  const early = localCompletionGate({ ...common, rapport: { atFarewell: false } });
  assert.equal(early.canRequest, false);
  assert.match(early.label, /道别/);
  assert.match(early.detail, /中止/);
  // 停在道别节 → 可请求;服务端仍是权威。
  const ready = localCompletionGate({ ...common, rapport: { atFarewell: true } });
  assert.equal(ready.canRequest, true);
  assert.match(ready.detail, /道别位置/);
  assert.equal(localInterventionCompletionGate({
    journalReady: true, planReady: true, weekNo: 1, totalTurns: 0,
    rapport: { atFarewell: true },
  }).canRequest, true);
  // 记录未加载完成时仍先等加载。
  assert.equal(localCompletionGate({
    ...common, journalReady: false, rapport: { atFarewell: true },
  }).canRequest, false);
});

test("rapport rejection surfaces the rapport-shaped server assessment", () => {
  const failure = parseCompletionFailure({
    detailData: {
      message: "床旁干预结束门禁未通过；保持当前位置，不切换受试者",
      assessment: {
        ready: false,
        protocol: "rapport",
        at_farewell: false,
        recording_idle: true,
        audio_total: 3,
        audio_verified: 3,
        issues: [{
          code: "rapport_not_at_farewell",
          detail: "关系建立须停在道别节才能结束；请先播报道别话术",
        }],
      },
    },
  });
  assert.equal(failure.assessment?.protocol, "rapport");
  assert.equal(failure.assessment?.atFarewell, false);
  assert.equal(failure.assessment?.recordingIdle, true);
  assert.equal(failure.assessment?.audioTotal, 3);
  assert.equal(failure.assessment?.audioVerified, 3);
  assert.equal(failure.assessment?.issues[0]?.code, "rapport_not_at_farewell");
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
