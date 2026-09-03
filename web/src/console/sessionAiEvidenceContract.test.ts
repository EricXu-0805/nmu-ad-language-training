import assert from "node:assert/strict";
import test from "node:test";
import {
  parseConfirmationRevisionsByTurn,
  parseRapportUtteranceList,
  parseTtsServeEvidenceList,
} from "./sessionAiEvidenceContract.ts";

const SID = "S1";
const HASH = "a".repeat(64);

function ttsRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    session_id: SID,
    command_id: 7,
    utterance_id: null,
    source: "autopilot_command",
    engine_version: "qwen-tts-v1",
    cache_hit: false,
    result: "served",
    byte_count: 4096,
    text_sha256: HASH,
    is_simulation: false,
    created_at: "2026-07-28T10:00:00",
    ...overrides,
  };
}

test("parseTtsServeEvidenceList: valid populated list parses", () => {
  const rows = parseTtsServeEvidenceList([
    ttsRow(),
    ttsRow({ id: 2, command_id: null, source: "live_speak", result: "degraded", byte_count: null, cache_hit: false }),
  ], SID, false);
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.result, "served");
  assert.equal(rows[1]!.result, "degraded");
});

test("parseTtsServeEvidenceList: valid empty array parses to empty", () => {
  assert.deepEqual(parseTtsServeEvidenceList([], SID, false), []);
});

test("parseTtsServeEvidenceList: rejects non-array payload", () => {
  assert.throws(() => parseTtsServeEvidenceList({}, SID, false));
  assert.throws(() => parseTtsServeEvidenceList(null, SID, false));
});

test("parseTtsServeEvidenceList: rejects missing field", () => {
  const { text_sha256, ...missing } = ttsRow();
  void text_sha256;
  assert.throws(() => parseTtsServeEvidenceList([missing], SID, false));
});

test("parseTtsServeEvidenceList: rejects unknown extra field", () => {
  assert.throws(() => parseTtsServeEvidenceList([{ ...ttsRow(), extra: 1 }], SID, false));
});

test("parseTtsServeEvidenceList: rejects a row from a foreign session", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ session_id: "OTHER" })], SID, false));
});

test("parseTtsServeEvidenceList: rejects unknown source/result enum values", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ source: "phone_call" })], SID, false));
  assert.doesNotThrow(() => parseTtsServeEvidenceList([
    ttsRow({ id: 3, source: "rapport_utterance", command_id: null, utterance_id: 7 }),
  ], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([
    ttsRow({ id: 4, source: "rapport_utterance", command_id: null, utterance_id: null }),
  ], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([
    ttsRow({ id: 5, source: "live_speak", command_id: null, utterance_id: 7 }),
  ], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ result: "cancelled" })], SID, false));
});

test("parseTtsServeEvidenceList: enforces byte_count/result binding", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ byte_count: null })], SID, false), /served/);
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ byte_count: 0 })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList(
    [ttsRow({ source: "live_speak", command_id: null, result: "degraded", byte_count: 10 })],
    SID, false,
  ), /byte_count/);
});

test("parseTtsServeEvidenceList: enforces source/command_id binding", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ command_id: null })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList(
    [ttsRow({ source: "live_speak", result: "degraded", byte_count: null })],
    SID, false,
  ));
});

test("parseTtsServeEvidenceList: rejects a degraded row that claims a cache hit", () => {
  assert.throws(() => parseTtsServeEvidenceList(
    [ttsRow({ source: "live_speak", command_id: null, result: "degraded", byte_count: null, cache_hit: true })],
    SID, false,
  ), /cache_hit/);
});

test("parseTtsServeEvidenceList: rejects malformed text_sha256 (wrong length, uppercase, non-hex)", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ text_sha256: "abc" })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ text_sha256: HASH.toUpperCase() })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ text_sha256: "z".repeat(64) })], SID, false));
});

test("parseTtsServeEvidenceList: rejects malformed created_at (impossible calendar date)", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ created_at: "not-a-date" })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ created_at: "2026-02-30T00:00:00" })], SID, false));
});

test("parseTtsServeEvidenceList: accepts a naive (no timezone) timestamp, matching the backend's actual encoding", () => {
  assert.doesNotThrow(() => parseTtsServeEvidenceList([ttsRow({ created_at: "2026-07-28T10:00:00" })], SID, false));
});

test("parseTtsServeEvidenceList: rejects an out-of-range UTC offset or a year of 0000", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ created_at: "2026-07-28T10:00:00+14:01" })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ created_at: "2026-07-28T10:00:00+15:00" })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ created_at: "0000-07-28T10:00:00" })], SID, false));
  // +14:00 恰好是合法边界。
  assert.doesNotThrow(() => parseTtsServeEvidenceList([ttsRow({ created_at: "2026-07-28T10:00:00+14:00" })], SID, false));
});

test("parseTtsServeEvidenceList: is_simulation must exactly match the verified journal session, not just be a boolean", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ is_simulation: true })], SID, false), /is_simulation/);
  assert.doesNotThrow(() => parseTtsServeEvidenceList([ttsRow({ is_simulation: true })], SID, true));
});

test("parseTtsServeEvidenceList: rejects a non-positive or non-integer command_id/id/byte_count", () => {
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ id: 0 })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ command_id: -1 })], SID, false));
  assert.throws(() => parseTtsServeEvidenceList([ttsRow({ byte_count: 1.5 })], SID, false));
});

test("parseTtsServeEvidenceList: rejects duplicate row ids, but the same command_id repeated across distinct rows is legitimate", () => {
  // 同一 command 真实重复朗读多次是合法场景,不能拿 command_id 去重。
  assert.doesNotThrow(() => parseTtsServeEvidenceList(
    [ttsRow({ id: 1, command_id: 7 }), ttsRow({ id: 2, command_id: 7 })],
    SID, false,
  ));
  assert.throws(
    () => parseTtsServeEvidenceList([ttsRow({ id: 1 }), ttsRow({ id: 1 })], SID, false),
    /重复的行 id/,
  );
});

// ---- confirmation revisions ----

const TURNS = [
  { id: 10, confirmation_revision: 0 },
  { id: 11, confirmation_revision: 2 },
];

function revisionRow(turnId: number, revision: number, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    turn_id: turnId,
    revision,
    actor_display_id: "researcher-1",
    changed_at: "2026-07-28T10:00:00",
    ...overrides,
  };
}

test("parseConfirmationRevisionsByTurn: revision=0 with no ledger rows is structurally valid (unbackfilled history)", () => {
  const byTurn = parseConfirmationRevisionsByTurn([], [TURNS[0]!]);
  assert.deepEqual(byTurn.get(10), { turnId: 10, finalRevision: 0, entries: [] });
});

test("parseConfirmationRevisionsByTurn: a ledger row against a turn declaring revision=0 is a structural contradiction", () => {
  assert.throws(
    () => parseConfirmationRevisionsByTurn([revisionRow(10, 1)], TURNS),
    /结构矛盾/,
  );
});

test("parseConfirmationRevisionsByTurn: contiguous 1..N rows matching the final revision parse cleanly", () => {
  const byTurn = parseConfirmationRevisionsByTurn([
    revisionRow(11, 1), revisionRow(11, 2),
  ], TURNS);
  const turn11 = byTurn.get(11)!;
  assert.equal(turn11.finalRevision, 2);
  assert.equal(turn11.entries.length, 2);
  assert.deepEqual(turn11.entries.map((e) => e.revision), [1, 2]);
});

test("parseConfirmationRevisionsByTurn: rejects a row pointing at an unknown turn", () => {
  assert.throws(() => parseConfirmationRevisionsByTurn([revisionRow(999, 1)], TURNS), /未知的 turn/);
});

test("parseConfirmationRevisionsByTurn: a malformed huge confirmation_revision (e.g. 1e9) is rejected upfront by the business cap, never reaching any per-integer loop", () => {
  const start = process.hrtime.bigint();
  assert.throws(
    () => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: 1_000_000_000 }]),
    /超出合理业务上限/,
  );
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  // 若真的跑进了 1..1e9 的枚举循环，这个断言会在慢机器上也明显超时；
  // 上限校验必须在循环之前就拒绝，耗时应停留在亚毫秒级。
  assert.ok(elapsedMs < 200, `耗时 ${elapsedMs}ms，怀疑未在循环前拒绝`);
});

test("parseConfirmationRevisionsByTurn: rejects a negative or non-integer confirmation_revision on a known turn", () => {
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: -1 }]));
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: 1.5 }]));
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: Number.NaN }]));
});

test("parseConfirmationRevisionsByTurn: rejects a non-positive or non-integer turn id, and a duplicate turn id, in knownTurns", () => {
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: 0, confirmation_revision: 0 }]));
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: -1, confirmation_revision: 0 }]));
  assert.throws(() => parseConfirmationRevisionsByTurn([], [{ id: 1.5, confirmation_revision: 0 }]));
  assert.throws(() => parseConfirmationRevisionsByTurn(
    [], [{ id: 11, confirmation_revision: 0 }, { id: 11, confirmation_revision: 0 }],
  ), /重复出现/);
});

test("parseConfirmationRevisionsByTurn: a normal small confirmation_revision is accepted, and one integer above the business cap is rejected", () => {
  // 不构造十万行账本去证明 100_000 本身被接受——那既慢又不是本测试的职责；
  // 这里只证明"正常小数值可用"与"刚好超过上限被拒绝"这两件事。
  assert.doesNotThrow(() => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: 0 }]));
  assert.throws(
    () => parseConfirmationRevisionsByTurn([], [{ id: 11, confirmation_revision: 100_001 }]),
    /超出合理业务上限/,
  );
});

test("parseConfirmationRevisionsByTurn: rejects duplicate revisions for the same turn", () => {
  assert.throws(
    () => parseConfirmationRevisionsByTurn([revisionRow(11, 1), revisionRow(11, 1)], TURNS),
    /重复/,
  );
});

test("parseConfirmationRevisionsByTurn: a short account (fewer rows than the declared final revision) is rejected by the length check, not the gap scanner", () => {
  // 声明 final=2，但账本只出现 revision 1，缺 2——行数本身已经不符，不需要进枚举循环。
  assert.throws(
    () => parseConfirmationRevisionsByTurn([revisionRow(11, 1)], TURNS),
    /行数与最终 revision 不一致/,
  );
});

test("parseConfirmationRevisionsByTurn: rejects a gap in the revision sequence even when the row count matches the declared final revision", () => {
  // 行数(3)与 confirmation_revision(3) 一致，但缺 revision 3、多出 revision 4——必须仍被连续性扫描抓到。
  const turnsWithThree = [{ id: 11, confirmation_revision: 3 }];
  assert.throws(
    () => parseConfirmationRevisionsByTurn(
      [revisionRow(11, 1), revisionRow(11, 2), revisionRow(11, 4)],
      turnsWithThree,
    ),
    /缺少 revision 3/,
  );
});

test("parseConfirmationRevisionsByTurn: rejects a final-revision mismatch against Turn.confirmation_revision", () => {
  // 账本连续 1..3，但 Turn 只声明 final=2。
  const turnsWithMismatch = [{ id: 11, confirmation_revision: 2 }];
  assert.throws(
    () => parseConfirmationRevisionsByTurn(
      [revisionRow(11, 1), revisionRow(11, 2), revisionRow(11, 3)],
      turnsWithMismatch,
    ),
    /行数与最终 revision 不一致/,
  );
});

test("parseConfirmationRevisionsByTurn: rejects malformed rows (missing/extra field, bad actor, bad time)", () => {
  const { actor_display_id, ...missingActor } = revisionRow(11, 1);
  void actor_display_id;
  assert.throws(() => parseConfirmationRevisionsByTurn([missingActor, revisionRow(11, 2)], TURNS));
  assert.throws(() => parseConfirmationRevisionsByTurn(
    [{ ...revisionRow(11, 1), extra: true }, revisionRow(11, 2)], TURNS,
  ));
  assert.throws(() => parseConfirmationRevisionsByTurn(
    [revisionRow(11, 1, { changed_at: "not-a-time" }), revisionRow(11, 2)], TURNS,
  ));
});

test("parseConfirmationRevisionsByTurn: rejects non-array payload", () => {
  assert.throws(() => parseConfirmationRevisionsByTurn({}, TURNS));
});

function utteranceRow(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    session_id: SID,
    event_seq: 1,
    section_key: "介绍机构环境",
    question_idx: 0,
    source: "llm",
    origin: "auto",
    reply_id: null,
    text: "好的，谢谢您告诉我。",
    asr_text: "我年轻时在学校教书",
    asr_engine_version: "qwen3-asr-flash/1",
    reply_engine_version: "qwen-plus/1",
    degraded_reason: null,
    raw_audio_id: "aud-1",
    text_sha256: HASH,
    created_at: "2026-08-31T10:00:00",
    is_simulation: false,
    ...overrides,
  };
}

const BANK_ROW = {
  id: 2, event_seq: 2, source: "bank", origin: "manual", reply_id: "a1",
  asr_text: null, asr_engine_version: null, reply_engine_version: null,
  raw_audio_id: null,
};

test("parseRapportUtteranceList: llm 行与句库行都能通过", () => {
  const rows = parseRapportUtteranceList([utteranceRow(), utteranceRow(BANK_ROW)], SID, false);
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!.source, "llm");
  assert.equal(rows[0]!.asrText, "我年轻时在学校教书");
  assert.equal(rows[1]!.replyId, "a1");
});

test("parseRapportUtteranceList: 空数组与非数组", () => {
  assert.deepEqual(parseRapportUtteranceList([], SID, false), []);
  assert.throws(() => parseRapportUtteranceList({}, SID, false));
  assert.throws(() => parseRapportUtteranceList(undefined, SID, false));
});

test("parseRapportUtteranceList: 缺键/多键整行拒收", () => {
  const { text_sha256, ...missing } = utteranceRow();
  void text_sha256;
  assert.throws(() => parseRapportUtteranceList([missing], SID, false));
  assert.throws(() => parseRapportUtteranceList([{ ...utteranceRow(), extra: 1 }], SID, false));
});

test("parseRapportUtteranceList: 场次与模拟标志围栏", () => {
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ session_id: "S2" })], SID, false));
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ is_simulation: true })], SID, false));
});

test("parseRapportUtteranceList: source 与 reply_id 绑定关系", () => {
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ ...BANK_ROW, reply_id: null })], SID, false));
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ reply_id: "a1" })], SID, false));
});

test("parseRapportUtteranceList: 降级原因闭集", () => {
  const ok = parseRapportUtteranceList(
    [utteranceRow({ source: "fallback", degraded_reason: "cloud_not_authorized", asr_text: null })], SID, false);
  assert.equal(ok[0]!.degradedReason, "cloud_not_authorized");
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ degraded_reason: "mystery" })], SID, false));
});

test("parseRapportUtteranceList: 行 id 去重与 event_seq 严格递增", () => {
  assert.throws(() => parseRapportUtteranceList(
    [utteranceRow(), utteranceRow({ event_seq: 2 })], SID, false));
  assert.throws(() => parseRapportUtteranceList(
    [utteranceRow({ event_seq: 2 }), utteranceRow({ ...BANK_ROW, event_seq: 2 })], SID, false));
  assert.throws(() => parseRapportUtteranceList(
    [utteranceRow({ event_seq: 3 }), utteranceRow({ ...BANK_ROW, event_seq: 2 })], SID, false));
});

test("parseRapportUtteranceList: raw_audio_id 上界与服务端 160 一致", () => {
  const ok = parseRapportUtteranceList([utteranceRow({ raw_audio_id: "a".repeat(160) })], SID, false);
  assert.equal(ok[0]!.rawAudioId!.length, 160);
  assert.throws(() => parseRapportUtteranceList([utteranceRow({ raw_audio_id: "a".repeat(161) })], SID, false));
});

test("parseRapportUtteranceList: round_limit 是合法降级原因(聊满收束)", () => {
  const rows = parseRapportUtteranceList([utteranceRow({
    source: "fallback", reply_id: null, asr_text: null, asr_engine_version: null,
    reply_engine_version: null, degraded_reason: "round_limit",
  })], SID, false);
  assert.equal(rows[0]!.degradedReason, "round_limit");
});
