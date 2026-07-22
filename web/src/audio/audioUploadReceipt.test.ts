import assert from "node:assert/strict";
import test from "node:test";
import { completeAudioOutboxAfterServerAck, validateAudioCaptureReceiptAck, validateAudioUploadReceipt } from "./audioUploadReceipt.ts";

const checksum = "a".repeat(64);
const expected = { rawAudioId: "aud-1", bytes: 321, checksum };

test("upload receipt must prove the exact id, byte count, and checksum", () => {
  assert.deepEqual(validateAudioUploadReceipt({
    raw_audio_id: "aud-1",
    bytes: 321,
    checksum: checksum.toUpperCase(),
    format: "webm",
  }, expected), {
    raw_audio_id: "aud-1",
    bytes: 321,
    checksum,
    format: "webm",
  });
});

test("mismatched or malformed upload receipts stay fail-closed", () => {
  assert.throws(() => validateAudioUploadReceipt({ raw_audio_id: "aud-2", bytes: 321, checksum }, expected), /编号/);
  assert.throws(() => validateAudioUploadReceipt({ raw_audio_id: "aud-1", bytes: 320, checksum }, expected), /字节数/);
  assert.throws(() => validateAudioUploadReceipt({ raw_audio_id: "aud-1", bytes: 321, checksum: "bad" }, expected), /校验值/);
});

test("local audio deletion requires an exact server capture-ledger ACK", () => {
  assert.deepEqual(validateAudioCaptureReceiptAck({
    seq: 9,
    audioReceipt: { serverSeq: 7, rawAudioId: "aud-1", idempotent: false },
  }, "aud-1"), { serverSeq: 7, rawAudioId: "aud-1", idempotent: false });
  assert.throws(() => validateAudioCaptureReceiptAck({ seq: 9 }, "aud-1"), /编号/);
  assert.throws(() => validateAudioCaptureReceiptAck({
    audioReceipt: { serverSeq: 7, rawAudioId: "aud-other", idempotent: false },
  }, "aud-1"), /编号/);
  for (const serverSeq of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => validateAudioCaptureReceiptAck({
      audioReceipt: { serverSeq, rawAudioId: "aud-1", idempotent: false },
    }, "aud-1"), /序号/);
  }
  assert.throws(() => validateAudioCaptureReceiptAck({
    audioReceipt: { serverSeq: 7, rawAudioId: "aud-1", idempotent: "false" },
  }, "aud-1"), /幂等/);
});

test("missing or mismatched capture ACK keeps the local outbox untouched", async () => {
  let completed = 0;
  const complete = async () => { completed += 1; };
  await assert.rejects(() => completeAudioOutboxAfterServerAck(
    { seq: 9 }, "aud-1", complete), /编号/);
  await assert.rejects(() => completeAudioOutboxAfterServerAck({
    audioReceipt: { serverSeq: 7, rawAudioId: "aud-other", idempotent: false },
  }, "aud-1", complete), /编号/);
  assert.equal(completed, 0);
  await completeAudioOutboxAfterServerAck({
    audioReceipt: { serverSeq: 7, rawAudioId: "aud-1", idempotent: true },
  }, "aud-1", complete);
  assert.equal(completed, 1);
});
