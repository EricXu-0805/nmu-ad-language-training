import assert from "node:assert/strict";
import test from "node:test";
import { parseWeek1Warmup, parseWeek1WarmupContent, warmupIdentity, warmupTtsContext, type Week1Warmup } from "./week1Warmup.ts";
import { parseSyncPayload } from "./sync/messages.ts";
import { parseWarmupReceipt } from "./sync/warmupRequest.ts";
import { parsePatientPresentation, presentationMatches } from "./patient/presentationContent.ts";
import { observeWarmupPlayback } from "./patient/warmupPlaybackFence.ts";
const warmup: Week1Warmup = { versionId: "wk1-warmup-v1", topic: "weather", beat: "ask", seq: 11 };
const step = { sessionId: "SIM-WARMUP", sectionKey: "认识机器人", questionIdx: 0, beat: "ask", recording: "idle", wseq: 11, warmup };
const projection = { schema_version: 1, mode: "rapport", session_id: "SIM-WARMUP", script_version_id: "week1-v1", section_key: "认识机器人", question_idx: 0, beat: "ask", speaker: "机器人", text: "今天天气怎么样？", wseq: 11, warmup };
const request = { action: "start" as const, topic: "weather" as const, expectedWseq: 10, expectedRevision: 3, idempotencyKey: "stable-request" };
test("warmup strictly validates version, topic, beat and sequence across live and patient transport", () => {
  assert.deepEqual(parseWeek1Warmup(warmup), warmup);
  assert.deepEqual(parseSyncPayload("rapportStep", step)?.warmup, warmup);
  const parsed = parsePatientPresentation(projection);
  assert.equal(presentationMatches(parsed, { mode: "rapport", sessionId: "SIM-WARMUP", sectionKey: "认识机器人", questionIdx: 0, beat: "ask", wseq: 11, warmup }), true);
  for (const invalid of [{ ...warmup, versionId: "unknown" }, { ...warmup, topic: "other" }, { ...warmup, beat: "record" }, { ...warmup, seq: 0 }, { ...warmup, seq: 1.2 }, { ...warmup, transcript: "unexpected" }]) {
    assert.equal(parseWeek1Warmup(invalid), null);
    assert.equal(parseSyncPayload("rapportStep", { ...step, warmup: invalid }), null);
    assert.throws(() => parsePatientPresentation({ ...projection, warmup: invalid }));
  }
  assert.equal(parseSyncPayload("rapportStep", { ...step, recording: "armed" }), null);
  assert.equal(parseSyncPayload("rapportStep", { ...step, wseq: 10 }), null);
});
test("the original formal position never authorizes another warmup identity or the legacy namespace", () => {
  const parsed = parsePatientPresentation(projection);
  const expected = { mode: "rapport" as const, sessionId: "SIM-WARMUP", sectionKey: "认识机器人", questionIdx: 0, beat: "ask" as const, wseq: 11 };
  assert.equal(presentationMatches(parsed, expected), false);
  for (const change of [{ topic: "food" }, { beat: "reply" }, { seq: 10 }]) assert.equal(presentationMatches(parsed, { ...expected, warmup: { ...warmup, ...change } as Week1Warmup }), false);
  const done = { ...warmup, beat: "done" as const };
  assert.throws(() => parsePatientPresentation({ ...projection, warmup: done }));
  assert.doesNotThrow(() => parsePatientPresentation({ ...projection, warmup: done, speaker: "研究者", text: null }));
  assert.equal(warmupTtsContext("SIM-WARMUP", done), null);
  assert.match(warmupTtsContext("SIM-WARMUP", warmup)!, /plane:warmup.*version:wk1-warmup-v1.*seq:11/);
});
test("a validated exact server receipt is required before publishing any warmup action", () => {
  const receipt = { seq: 12, wseq: 11, runtimeRevision: 4, rapportStep: step };
  assert.deepEqual(parseWarmupReceipt(receipt, "SIM-WARMUP", request).rapportStep.warmup, warmup);
  for (const changed of [{ wseq: 10 }, { runtimeRevision: 3 }, { seq: -1 }, { rapportStep: { ...step, sessionId: "OTHER" } }, { rapportStep: { ...step, warmup: { ...warmup, topic: "food" } } }, { rapportStep: { ...step, warmup: { ...warmup, beat: "reply" } } }, { extra: true }]) assert.throws(() => parseWarmupReceipt({ ...receipt, ...changed }, "SIM-WARMUP", request));
  assert.throws(() => parseWarmupReceipt(receipt, "SIM-WARMUP", { ...request, action: "continue" }));
  const { warmup: _warmup, ...formal } = step;
  assert.equal(parseWarmupReceipt({ ...receipt, rapportStep: formal }, "SIM-WARMUP", { ...request, action: "continue" }).rapportStep.warmup, undefined);
});
test("only the complete four-topic content bundle is accepted, with no inferred text", () => {
  const content = { versionId: "wk1-warmup-v1", topics: ["weather", "food", "sleep", "activity"].map((id) => ({ id, label: id, ask: "问句", reply: "回应" })) };
  assert.deepEqual(parseWeek1WarmupContent(content), content);
  for (const invalid of [{ ...content, topics: content.topics.slice(1) }, { ...content, topics: content.topics.map(() => content.topics[0]) }, { ...content, topics: content.topics.map((t) => ({ ...t, ask: "" })) }, { ...content, versionId: "unreviewed" }]) assert.throws(() => parseWeek1WarmupContent(invalid));
});
const observe = (state: Parameters<typeof observeWarmupPlayback>[0], value?: Week1Warmup, changes = {}) => observeWarmupPlayback(state, { sessionId: "SIM-WARMUP", hasStep: true, warmup: value, connectionReady: true, paused: false, terminal: false, ...changes });
test("refreshing or joining a tablet restores silently until an explicit replay", () => {
  const restored = observe(null, warmup);
  assert.equal(restored.allowedIdentity, null);
  assert.equal(observe(restored.state, warmup).allowedIdentity, null);
  assert.equal(observe(restored.state, { ...warmup, seq: 12 }).allowedIdentity, warmupIdentity({ ...warmup, seq: 12 }));
});
for (const gate of ["paused", "terminal", "connectionReady"] as const) test(`${gate} permanently revokes the current optional speech`, () => {
  const before = observe(null);
  const started = observe(before.state, warmup);
  assert.equal(started.allowedIdentity, warmupIdentity(warmup));
  const blocked = observe(started.state, warmup, { [gate]: gate !== "connectionReady" });
  assert.equal(blocked.allowedIdentity, null);
  assert.equal(observe(blocked.state, warmup).allowedIdentity, null);
  assert.notEqual(observe(blocked.state, { ...warmup, seq: 12 }).allowedIdentity, null);
});
test("done, a different session and initially unconnected arrival never start a voice", () => {
  const before = observe(null);
  assert.equal(observe(before.state, { ...warmup, beat: "done" }).allowedIdentity, null);
  assert.equal(observe(before.state, warmup, { sessionId: "OTHER" }).allowedIdentity, null);
  const offline = observe(before.state, warmup, { connectionReady: false });
  assert.equal(observe(offline.state, warmup).allowedIdentity, null);
});


test("pause, withdrawal and restored live projections accept only a stopped, paused done warmup", () => {
  const paused = { ...step, recording: "stopped", paused: true, wseq: 12, warmup: { ...warmup, beat: "done" as const } };
  const coldRestore = parseSyncPayload("rapportStep", paused);
  assert.equal(coldRestore?.warmup?.beat, "done");
  assert.equal(coldRestore?.recording, "stopped");
  const restoredFence = observeWarmupPlayback(null, { sessionId: "SIM-WARMUP", hasStep: true,
    warmup: coldRestore!.warmup, paused: true, terminal: false, connectionReady: true });
  assert.equal(restoredFence.allowedIdentity, null);
  for (const unsafe of [{ ...paused, paused: false }, { ...paused, warmup }, { ...paused, recording: "armed" }, { ...paused, recording: "recording" }]) {
    assert.equal(parseSyncPayload("rapportStep", unsafe), null);
  }
});
