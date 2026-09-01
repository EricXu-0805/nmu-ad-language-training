import assert from "node:assert/strict";
import test from "node:test";
import type { ConfirmationRevisionTurnRecord, TtsServeEvidenceRecord } from "./sessionAiEvidenceContract.ts";
import type { SessionAiUsageContract } from "./sessionAiUsageContract.ts";
import {
  buildAiUsageSection,
  buildConfirmationRevisionSection,
  buildConfirmationRevisionTurnViewModel,
  buildTtsServeEvidenceSection,
  sessionAiEvidenceEmptyNotice,
} from "./sessionAiEvidenceViewModel.ts";

function ttsRecord(overrides: Partial<TtsServeEvidenceRecord> = {}): TtsServeEvidenceRecord {
  return {
    id: 1,
    commandId: 7,
    source: "autopilot_command",
    engineVersion: "qwen-tts-v1",
    cacheHit: false,
    result: "served",
    byteCount: 4096,
    textSha256: "a".repeat(64),
    isSimulation: false,
    createdAt: "2026-07-28T10:00:00",
    ...overrides,
  };
}

test("buildTtsServeEvidenceSection: an empty record list is a distinct 'empty' status, not silently blank", () => {
  const section = buildTtsServeEvidenceSection([]);
  assert.deepEqual(section, { status: "empty", notice: sessionAiEvidenceEmptyNotice });
});

test("buildTtsServeEvidenceSection: aggregates served/cache-hit/degraded counts and sorts rows by time", () => {
  const section = buildTtsServeEvidenceSection([
    ttsRecord({ id: 2, createdAt: "2026-07-28T10:05:00", cacheHit: true }),
    ttsRecord({ id: 1, createdAt: "2026-07-28T10:00:00" }),
    ttsRecord({
      id: 3, createdAt: "2026-07-28T10:10:00", source: "live_speak", commandId: null,
      result: "degraded", byteCount: null, cacheHit: false,
    }),
  ]);
  assert.equal(section.status, "ready");
  if (section.status !== "ready") return;
  assert.deepEqual(section.summary, { served: 2, degraded: 1, cacheHits: 1 });
  assert.deepEqual(section.rows.map((row) => row.key), ["tts-1", "tts-2", "tts-3"]);
});

test("buildTtsServeEvidenceSection: a degraded row never claims a cache result, and reads as unavailable rather than 'not hit'", () => {
  const section = buildTtsServeEvidenceSection([
    ttsRecord({ result: "degraded", byteCount: null, commandId: null, source: "live_speak", cacheHit: false }),
  ]);
  assert.equal(section.status, "ready");
  if (section.status !== "ready") return;
  assert.equal(section.rows[0]!.cacheLabel, "缓存不适用(服务器未返回音频)");
  assert.doesNotMatch(section.rows[0]!.cacheLabel, /未命中/);
});

test("buildTtsServeEvidenceSection: a served row does distinguish hit vs miss", () => {
  const hit = buildTtsServeEvidenceSection([ttsRecord({ cacheHit: true })]);
  const miss = buildTtsServeEvidenceSection([ttsRecord({ cacheHit: false })]);
  assert.equal(hit.status === "ready" ? hit.rows[0]!.cacheLabel : "", "缓存命中");
  assert.equal(miss.status === "ready" ? miss.rows[0]!.cacheLabel : "", "缓存未命中");
});

// ---- confirmation revision turn view model ----

function trackedRecord(overrides: Partial<ConfirmationRevisionTurnRecord> = {}): ConfirmationRevisionTurnRecord {
  return {
    turnId: 11,
    finalRevision: 2,
    entries: [
      { revision: 1, actorDisplayId: "researcher-1", changedAt: "2026-07-28T09:00:00" },
      { revision: 2, actorDisplayId: "researcher-2", changedAt: "2026-07-28T09:05:00" },
    ],
    ...overrides,
  };
}

test("buildConfirmationRevisionTurnViewModel: finalRevision=0 with no confirmed text uses a neutral, content-blind notice", () => {
  const view = buildConfirmationRevisionTurnViewModel({ turnId: 10, finalRevision: 0, entries: [] }, false);
  assert.equal(view.status, "no_ledger");
  if (view.status !== "no_ledger") return;
  assert.doesNotMatch(view.notice, /从未确认/);
  assert.match(view.notice, /可能为历史兼容记录/);
});

test("buildConfirmationRevisionTurnViewModel: finalRevision=0 WITH confirmed text must not claim 'never confirmed' either", () => {
  const view = buildConfirmationRevisionTurnViewModel({ turnId: 10, finalRevision: 0, entries: [] }, true);
  assert.equal(view.status, "no_ledger");
  if (view.status !== "no_ledger") return;
  assert.doesNotMatch(view.notice, /从未确认/);
  assert.match(view.notice, /已存在确认文本/);
  assert.match(view.notice, /修订记录未补录/);
});

test("buildConfirmationRevisionTurnViewModel: finalRevision>0 lists every revision entry, content-blind", () => {
  const view = buildConfirmationRevisionTurnViewModel(trackedRecord(), false);
  assert.equal(view.status, "tracked");
  if (view.status !== "tracked") return;
  assert.equal(view.finalRevision, 2);
  assert.deepEqual(view.entries.map((e) => e.revision), [1, 2]);
  assert.equal(view.entries[0]!.actorDisplayId, "researcher-1");
});

test("buildConfirmationRevisionSection: sorts turns by turnId and resolves the has-confirmed-text lookup per turn", () => {
  const byTurn = new Map<number, ConfirmationRevisionTurnRecord>([
    [11, trackedRecord()],
    [10, { turnId: 10, finalRevision: 0, entries: [] }],
  ]);
  const section = buildConfirmationRevisionSection(byTurn, new Map([[10, true]]));
  assert.equal(section.status, "ready");
  if (section.status !== "ready") return;
  assert.deepEqual(section.turns.map((t) => t.turnId), [10, 11]);
  const turn10 = section.turns[0]!;
  assert.equal(turn10.status, "no_ledger");
  if (turn10.status === "no_ledger") assert.match(turn10.notice, /已存在确认文本/);
});

// ---- AI usage view model ----

function usageContract(overrides: Partial<SessionAiUsageContract> = {}): SessionAiUsageContract {
  return {
    sessionId: "S1",
    tts: { engines: [] },
    asr: { engines: [], degradedAttempts: 0 },
    judge: { modes: [] },
    ...overrides,
  };
}

test("buildAiUsageSection: a fully empty contract reports hasAnyRecords=false, not a bare zero", () => {
  const model = buildAiUsageSection(usageContract());
  assert.equal(model.hasAnyRecords, false);
  assert.deepEqual(model.ttsRows, []);
  assert.deepEqual(model.asrRows, []);
  assert.deepEqual(model.judgeRows, []);
});

test("buildAiUsageSection: populated tts/asr/judge sections are kept separate and hasAnyRecords=true", () => {
  const model = buildAiUsageSection(usageContract({
    tts: { engines: [{ engineVersion: "qwen-tts-v1", served: 3, cacheHits: 1, degraded: 1 }] },
    asr: { engines: [{ engineVersion: "asr-v1", attempts: 5 }], degradedAttempts: 1 },
    judge: { modes: [{ judgeMode: "规则确定式", judgeEngineVersion: "rule-1", attempts: 5 }] },
  }));
  assert.equal(model.hasAnyRecords, true);
  assert.equal(model.ttsRows.length, 1);
  assert.match(model.ttsRows[0]!.detail, /实际返回 3 次/);
  assert.equal(model.asrRows.length, 2); // engine row + degraded summary row
  assert.equal(model.judgeRows.length, 1);
  assert.match(model.judgeRows[0]!.label, /规则确定式/);
});

test("buildAiUsageSection: an empty judge_engine_version still labels by judge_mode alone, not a stray separator", () => {
  const model = buildAiUsageSection(usageContract({
    judge: { modes: [{ judgeMode: "规则确定式", judgeEngineVersion: "", attempts: 2 }] },
  }));
  assert.equal(model.judgeRows[0]!.label, "规则确定式");
});

import { buildRapportReplaySection } from "./sessionAiEvidenceViewModel.ts";

function replayUtterance(overrides: Record<string, unknown> = {}) {
  return {
    id: 1, eventSeq: 1, sectionKey: "介绍机构环境", questionIdx: 0,
    source: "llm", origin: "auto", replyId: null, text: "好的，谢谢您告诉我。",
    asrText: "我以前在学校教书", asrEngineVersion: "qwen3-asr-flash/1",
    replyEngineVersion: "qwen-plus/1", degradedReason: null, rawAudioId: "aud-1",
    textSha256: "a".repeat(64), createdAt: "2026-08-31T10:00:00", isSimulation: false,
    ...overrides,
  } as never;
}

function replayServe(overrides: Record<string, unknown> = {}) {
  return {
    id: 10, commandId: null, utteranceId: 1, source: "rapport_utterance",
    engineVersion: "qwen-tts/1", cacheHit: false, result: "served",
    byteCount: 2048, textSha256: "a".repeat(64), isSimulation: false,
    createdAt: "2026-08-31T10:00:01",
    ...overrides,
  } as never;
}

test("buildRapportReplaySection: 无账本行为 empty", () => {
  assert.deepEqual(buildRapportReplaySection([], []), { status: "empty" });
});

test("buildRapportReplaySection: 定稿与语音返回合看,三种说出状态各归其位", () => {
  const section = buildRapportReplaySection([
    replayUtterance(),
    replayUtterance({ id: 2, eventSeq: 2, source: "bank", origin: "manual",
      replyId: "a1", asrText: null, degradedReason: "llm_unavailable" }),
    replayUtterance({ id: 3, eventSeq: 3, source: "script", origin: "manual", asrText: null }),
  ], [
    replayServe(),
    replayServe({ id: 11, utteranceId: 2, result: "degraded", byteCount: null }),
    // 指向他句的 live_speak 证据不得串行。
    replayServe({ id: 12, utteranceId: null, source: "live_speak" }),
  ]);
  assert.equal(section.status, "ready");
  if (section.status !== "ready") return;
  assert.equal(section.summary.total, 3);
  assert.equal(section.summary.auto, 1);
  assert.equal(section.summary.llm, 1);
  assert.equal(section.summary.degraded, 1);
  assert.equal(section.summary.unspoken, 1);
  assert.equal(section.rows[0]!.spokenLabel, "已返回语音");
  assert.equal(section.rows[1]!.spokenLabel, "已改用本机语音(服务器未返回音频)");
  assert.equal(section.rows[2]!.spokenLabel, "只有定稿，没有语音返回记录");
  assert.equal(section.rows[1]!.degradedLabel, "AI 没给出可用回应（服务不可用或生成越界被拒），已退回固定句");
  assert.equal(section.rows[0]!.positionLabel, "介绍机构环境 · 第 1 问");
  assert.equal(section.rows[0]!.originLabel, "老人说完自动回应");
});
