import assert from "node:assert/strict";
import test from "node:test";
import {
  answerTimeoutAction,
  canReleaseAutopilotFailure,
  makeAutopilotFailure,
  type AutopilotFailureKind,
} from "./autopilotSafety.ts";

test("a timeout before microphone activation is a technical failure", () => {
  assert.equal(answerTimeoutAction(false), "technical-failure");
});

test("a technical-failure latch only releases after server-confirmed pause", () => {
  assert.equal(canReleaseAutopilotFailure("active"), false);
  assert.equal(canReleaseAutopilotFailure(undefined), false);
  assert.equal(canReleaseAutopilotFailure("paused"), true);
});

test("a timeout after microphone activation stops and waits for the saved audio", () => {
  assert.equal(answerTimeoutAction(true), "stop-and-await-audio");
});

test("every technical failure preserves cue level and position", () => {
  const before = { itemIdx: 4, turnIdx: 2, cueLevel: 1 };
  const kinds: AutopilotFailureKind[] = [
    "microphone",
    "audio",
    "upload",
    "asr",
    "classifier",
    "persistence",
  ];

  for (const kind of kinds) {
    const failure = makeAutopilotFailure(before, kind, `${kind} failed`);
    assert.deepEqual(
      { itemIdx: failure.itemIdx, turnIdx: failure.turnIdx, cueLevel: failure.cueLevel },
      before,
    );
    assert.equal(failure.kind, kind);
  }
});
