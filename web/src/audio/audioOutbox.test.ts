import assert from "node:assert/strict";
import test from "node:test";
import {
  advanceAudioOutbox,
  attachAudioCaptureReceipt,
  attachAutopilotStopReason,
  createAudioOutboxEntry,
  parseAudioOutboxEntry,
} from "./audioOutbox.ts";

const blob = new Blob(["voice"], { type: "audio/webm" });

function captured() {
  return createAudioOutboxEntry({
    rawAudioId: "aud-1",
    sessionId: "S-1",
    turnKey: "SE_锚#1",
    containsDirectIdentifier: false,
    durationSeconds: 1.25,
    blob,
    nowMs: 100,
  });
}

test("audio outbox advances monotonically and requires an upload checksum", () => {
  const registered = advanceAudioOutbox(captured(), "registered", { nowMs: 200 });
  const checksum = "a".repeat(64);
  const uploaded = advanceAudioOutbox(registered, "uploaded", { checksum, nowMs: 300 });
  assert.equal(uploaded.phase, "uploaded");
  assert.equal(uploaded.checksum, checksum);
  assert.throws(() => advanceAudioOutbox(uploaded, "registered"), /不得回退/);
  assert.throws(() => advanceAudioOutbox(captured(), "uploaded", { checksum }), /不得.*跳级/);
  assert.throws(() => advanceAudioOutbox(registered, "uploaded"), /checksum/);
});

test("autopilot stop and server receipt facts are immutable and phase-gated", () => {
  const stopped = attachAutopilotStopReason(captured(), "user_done");
  assert.equal(stopped.autopilotStopReason, "user_done");
  assert.throws(() => attachAutopilotStopReason(stopped, "max_duration"), /不得改写/);
  const registered = advanceAudioOutbox(stopped, "registered", { nowMs: 200 });
  const uploaded = advanceAudioOutbox(registered, "uploaded", {
    checksum: "a".repeat(64),
    nowMs: 300,
  });
  const receipted = attachAudioCaptureReceipt(uploaded, 7, 400);
  assert.equal(receipted.captureReceiptServerSeq, 7);
  assert.throws(() => attachAudioCaptureReceipt(receipted, 8), /不得改写/);
  assert.throws(() => attachAudioCaptureReceipt(stopped, 7), /仅已上传/);
});

test("tampered IndexedDB outbox values fail closed", () => {
  assert.throws(() => parseAudioOutboxEntry({ ...captured(), sessionId: "" }), /场次/);
  assert.throws(() => parseAudioOutboxEntry({ ...captured(), phase: "done" }), /元数据/);
  assert.throws(() => parseAudioOutboxEntry({
    ...captured(), phase: "uploaded", checksum: "not-a-checksum",
  }), /checksum/);
  assert.throws(() => parseAudioOutboxEntry({ ...captured(), blobBytes: 0 }), /元数据/);
  assert.throws(() => parseAudioOutboxEntry({ ...captured(), phase: "toString" }), /元数据/);
  assert.throws(() => parseAudioOutboxEntry({ ...captured(), unexpected: "field" }), /未允许字段/);
});
