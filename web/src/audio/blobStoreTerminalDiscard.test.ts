import assert from "node:assert/strict";
import test from "node:test";
import { advanceAudioOutbox, createAudioOutboxEntry } from "./audioOutbox.ts";
import {
  planTerminalOutboxDiscard,
  TerminalOutboxIntegrityError,
} from "./blobStore.ts";

const blob = new Blob(["voice"], { type: "audio/webm" });
const captured = createAudioOutboxEntry({
  rawAudioId: "aud-discard",
  sessionId: "S-1",
  turnKey: "turn-1",
  containsDirectIdentifier: false,
  durationSeconds: 1,
  blob,
  nowMs: 10,
});
const entry = advanceAudioOutbox(captured, "registered", { nowMs: 20 });

test("terminal discard transaction is idempotent only when blob and outbox are both absent", () => {
  assert.equal(planTerminalOutboxDiscard({
    expectedEntry: entry,
    storedEntry: undefined,
    storedBlob: undefined,
    hasLegacyMarker: false,
  }), "already-discarded");
  assert.throws(() => planTerminalOutboxDiscard({
    expectedEntry: entry,
    storedEntry: entry,
    storedBlob: undefined,
    hasLegacyMarker: false,
  }), TerminalOutboxIntegrityError);
  assert.throws(() => planTerminalOutboxDiscard({
    expectedEntry: entry,
    storedEntry: undefined,
    storedBlob: blob,
    hasLegacyMarker: false,
  }), TerminalOutboxIntegrityError);
});

test("integrity abort is decided before either atomic delete on any changed snapshot", () => {
  const cases = [
    { storedEntry: { ...entry, updatedAtMs: 999 }, storedBlob: blob, hasLegacyMarker: false },
    { storedEntry: entry, storedBlob: new Blob(["voice-longer"], { type: "audio/webm" }), hasLegacyMarker: false },
    { storedEntry: entry, storedBlob: blob, hasLegacyMarker: true },
  ];
  for (const changed of cases) {
    assert.throws(
      () => planTerminalOutboxDiscard({ expectedEntry: entry, ...changed }),
      TerminalOutboxIntegrityError,
    );
  }
  assert.equal(planTerminalOutboxDiscard({
    expectedEntry: entry,
    storedEntry: { ...entry },
    storedBlob: blob,
    hasLegacyMarker: false,
  }), "discarded");
});
