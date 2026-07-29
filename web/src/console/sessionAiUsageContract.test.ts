import assert from "node:assert/strict";
import test from "node:test";
import { parseSessionAiUsage } from "./sessionAiUsageContract.ts";

const SID = "S1";

function payload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: SID,
    tts: { engines: [{ engine_version: "qwen-tts-v1", served: 3, cache_hits: 1, degraded: 1 }] },
    asr: { engines: [{ engine_version: "asr-v1", attempts: 5 }], degraded_attempts: 1 },
    judge: { modes: [{ judge_mode: "规则确定式", judge_engine_version: "rule-1", attempts: 5 }] },
    ...overrides,
  };
}

test("parseSessionAiUsage: valid populated payload parses", () => {
  const result = parseSessionAiUsage(payload(), SID);
  assert.equal(result.sessionId, SID);
  assert.equal(result.tts.engines.length, 1);
  assert.equal(result.asr.engines[0]!.attempts, 5);
  assert.equal(result.judge.modes[0]!.judgeEngineVersion, "rule-1");
});

test("parseSessionAiUsage: valid empty payload parses to empty arrays/zero counts", () => {
  const result = parseSessionAiUsage(payload({
    tts: { engines: [] },
    asr: { engines: [], degraded_attempts: 0 },
    judge: { modes: [] },
  }), SID);
  assert.deepEqual(result.tts.engines, []);
  assert.deepEqual(result.asr.engines, []);
  assert.equal(result.asr.degradedAttempts, 0);
  assert.deepEqual(result.judge.modes, []);
});

test("parseSessionAiUsage: judge_engine_version may legitimately be an empty string", () => {
  const result = parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "规则确定式", judge_engine_version: "", attempts: 2 }] },
  }), SID);
  assert.equal(result.judge.modes[0]!.judgeEngineVersion, "");
});

test("parseSessionAiUsage: rejects a payload for the wrong session", () => {
  assert.throws(() => parseSessionAiUsage(payload({ session_id: "OTHER" }), SID), /其他场次/);
});

test("parseSessionAiUsage: rejects unknown or missing top-level/nested keys", () => {
  assert.throws(() => parseSessionAiUsage({ ...payload(), extra: 1 }, SID));
  const { asr: _asr, ...missingAsr } = payload();
  void _asr;
  assert.throws(() => parseSessionAiUsage(missingAsr, SID));
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: "e", served: 1, cache_hits: 0, degraded: 0, extra: true }] },
  }), SID));
});

test("parseSessionAiUsage: rejects invalid (non-safe-integer, negative) counts", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: "e", served: -1, cache_hits: 0, degraded: 0 }] },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    asr: { engines: [{ engine_version: "e", attempts: 1.5 }], degraded_attempts: 0 },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "m", judge_engine_version: "v", attempts: Number.MAX_SAFE_INTEGER + 1 }] },
  }), SID));
});

test("parseSessionAiUsage: rejects cache_hits greater than served", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: "e", served: 1, cache_hits: 2, degraded: 0 }] },
  }), SID), /cache_hits/);
});

test("parseSessionAiUsage: rejects duplicate tts/asr engine rows", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    tts: {
      engines: [
        { engine_version: "e", served: 1, cache_hits: 0, degraded: 0 },
        { engine_version: "e", served: 2, cache_hits: 0, degraded: 0 },
      ],
    },
  }), SID), /重复/);
  assert.throws(() => parseSessionAiUsage(payload({
    asr: {
      engines: [
        { engine_version: "e", attempts: 1 },
        { engine_version: "e", attempts: 2 },
      ],
      degraded_attempts: 0,
    },
  }), SID), /重复/);
});

test("parseSessionAiUsage: rejects a duplicate judge_mode/judge_engine_version pair", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    judge: {
      modes: [
        { judge_mode: "m", judge_engine_version: "v", attempts: 1 },
        { judge_mode: "m", judge_engine_version: "v", attempts: 2 },
      ],
    },
  }), SID), /组合重复/);
});

test("parseSessionAiUsage: distinct judge_engine_version values (including empty) are not treated as duplicates", () => {
  const result = parseSessionAiUsage(payload({
    judge: {
      modes: [
        { judge_mode: "m", judge_engine_version: "", attempts: 1 },
        { judge_mode: "m", judge_engine_version: "v1", attempts: 2 },
      ],
    },
  }), SID);
  assert.equal(result.judge.modes.length, 2);
});

test("parseSessionAiUsage: rejects whitespace-only or control-character engine_version/judge_mode", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: "   ", served: 1, cache_hits: 0, degraded: 0 }] },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: " e ", served: 1, cache_hits: 0, degraded: 0 }] },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    tts: { engines: [{ engine_version: "e\n", served: 1, cache_hits: 0, degraded: 0 }] },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "\t", judge_engine_version: "v", attempts: 1 }] },
  }), SID));
});

test("parseSessionAiUsage: rejects a non-empty judge_engine_version with leading/trailing whitespace or a control character", () => {
  assert.throws(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "m", judge_engine_version: " v ", attempts: 1 }] },
  }), SID));
  assert.throws(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "m", judge_engine_version: `v${String.fromCharCode(1)}`, attempts: 1 }] },
  }), SID));
  assert.doesNotThrow(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "m", judge_engine_version: "v", attempts: 1 }] },
  }), SID));
});

test("parseSessionAiUsage: an actually-empty judge_engine_version is still accepted after the whitespace hardening", () => {
  assert.doesNotThrow(() => parseSessionAiUsage(payload({
    judge: { modes: [{ judge_mode: "m", judge_engine_version: "", attempts: 1 }] },
  }), SID));
});

test("parseSessionAiUsage: rejects non-object/non-array shapes at every level", () => {
  assert.throws(() => parseSessionAiUsage(null, SID));
  assert.throws(() => parseSessionAiUsage(payload({ tts: { engines: "not-an-array" } }), SID));
  assert.throws(() => parseSessionAiUsage(payload({ asr: { engines: [], degraded_attempts: "1" } }), SID));
  assert.throws(() => parseSessionAiUsage(payload({ judge: { modes: "not-an-array" } }), SID));
});
