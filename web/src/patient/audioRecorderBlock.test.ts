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
  assert.equal(audioRecorderBlockCopy("terminal-discarded").patient, "这段录音已经处理好了，请找工作人员");
  assert.match(audioRecorderBlockCopy("terminal-discarded").researcher, /保持停麦/);
});
