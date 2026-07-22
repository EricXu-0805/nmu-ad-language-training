import assert from "node:assert/strict";
import test from "node:test";
import { audioRecorderBlockCopy, type AudioRecorderBlockReason } from "./audioRecorderBlock.ts";

test("every fail-closed recorder state gives patient and researcher a concrete action", () => {
  const reasons: AudioRecorderBlockReason[] = [
    "lease-waiting", "lease-unavailable", "storage-checking", "legacy-audio",
    "pending-other-session", "terminal-discarded", "storage-invalid", "storage-error",
  ];
  for (const reason of reasons) {
    const copy = audioRecorderBlockCopy(reason);
    assert.ok(copy.patient.length > 8, reason);
    assert.ok(copy.researcher.length > 16, reason);
  }
  assert.match(audioRecorderBlockCopy("legacy-audio").researcher, /不要直接清除/);
  assert.equal(audioRecorderBlockCopy("terminal-discarded").patient, "录音已按数据治理要求从本设备清除");
  assert.match(audioRecorderBlockCopy("terminal-discarded").researcher, /保持停麦/);
});
