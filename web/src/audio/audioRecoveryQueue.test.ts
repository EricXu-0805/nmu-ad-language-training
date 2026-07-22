import assert from "node:assert/strict";
import test from "node:test";
import { createAudioOutboxEntry } from "./audioOutbox.ts";
import {
  orderAudioRecoveryEntries,
  recoveryMayMutateActiveUi,
  shouldBroadcastRecoveredAudio,
} from "./audioRecoveryQueue.ts";

function entry(rawAudioId: string, sessionId: string, createdAtMs: number) {
  return createAudioOutboxEntry({
    rawAudioId,
    sessionId,
    turnKey: `turn-${rawAudioId}`,
    containsDirectIdentifier: false,
    durationSeconds: 1,
    blob: new Blob([rawAudioId], { type: "audio/webm" }),
    nowMs: createdAtMs,
  });
}

test("foreign S1 outboxes drain deterministically before current S2 is evaluated", () => {
  const current = entry("current", "S2", 10);
  const oldLaterId = entry("old-b", "S1", 20);
  const oldEarlierId = entry("old-a", "S1", 20);
  assert.deepEqual(
    orderAudioRecoveryEntries([current, oldLaterId, oldEarlierId], "S2")
      .map((value) => value.rawAudioId),
    ["old-a", "old-b", "current"],
  );
});

test("only recovery for the active session may emit the local wake hint", () => {
  assert.equal(shouldBroadcastRecoveredAudio(entry("old", "S1", 1), "S2"), false);
  assert.equal(shouldBroadcastRecoveredAudio(entry("current", "S2", 2), "S2"), true);
});

test("foreign S1 terminal cleanup and late generations cannot freeze active S2 UI", () => {
  const s2Generation = { sessionId: "S2", generation: 4 };
  assert.equal(recoveryMayMutateActiveUi(s2Generation, s2Generation, "S1"), false);
  assert.equal(recoveryMayMutateActiveUi(s2Generation, s2Generation, "S2"), true);
  assert.equal(recoveryMayMutateActiveUi(
    s2Generation,
    { sessionId: "S3", generation: 5 },
    "S2",
  ), false);
  assert.equal(recoveryMayMutateActiveUi(
    s2Generation,
    { sessionId: "S2", generation: 5 },
    "S2",
  ), false);
});
