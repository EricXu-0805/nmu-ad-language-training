import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  decideAudioSavedAutomation,
  LEGACY_AUDIO_SAVED_AUTO_ADVANCE_KEY,
  retireLegacyAudioSavedAutoAdvancePreference,
} from "./audioSavedProgressSafety.ts";

const currentFreshAudio = {
  sameSession: true,
  sameTurn: true,
  alreadyRecorded: false,
  safetyFailureLatched: false,
  manualTakeoverForTurn: false,
  adjudicationEnabled: false,
  interactionBlocked: false,
} as const;

test("audioSaved alone is record-only and never authorizes progress", () => {
  assert.equal(decideAudioSavedAutomation(currentFreshAudio), "record-only");
});

test("late or replayed audio cannot drive the newly active item", () => {
  assert.equal(decideAudioSavedAutomation({
    ...currentFreshAudio,
    sameTurn: false,
    adjudicationEnabled: true,
  }), "record-only");
  assert.equal(decideAudioSavedAutomation({
    ...currentFreshAudio,
    alreadyRecorded: true,
    adjudicationEnabled: true,
  }), "record-only");
});

test("only fresh current audio may enter adjudication, never a direct advance action", () => {
  assert.equal(decideAudioSavedAutomation({
    ...currentFreshAudio,
    adjudicationEnabled: true,
  }), "adjudicate");
  assert.equal(decideAudioSavedAutomation({
    ...currentFreshAudio,
    adjudicationEnabled: true,
    safetyFailureLatched: true,
  }), "record-only");
  assert.equal(decideAudioSavedAutomation({
    ...currentFreshAudio,
    adjudicationEnabled: true,
    manualTakeoverForTurn: true,
  }), "record-only");
});

test("refresh retires the former default-on preference instead of restoring it", () => {
  const values = new Map([[LEGACY_AUDIO_SAVED_AUTO_ADVANCE_KEY, "1"]]);
  retireLegacyAudioSavedAutoAdvancePreference({
    removeItem(key: string) { values.delete(key); },
  });
  assert.equal(values.has(LEGACY_AUDIO_SAVED_AUTO_ADVANCE_KEY), false);
});

test("training console has no direct progress mutation in the audioSaved callback", () => {
  const source = readFileSync(
    new URL("./TrainingConsoleScreen.tsx", import.meta.url), "utf8",
  );
  const callbackStart = source.indexOf("useAudioSaved((m) => {");
  const callbackEnd = source.indexOf("\n  const advance =");
  assert.ok(callbackStart >= 0, "audioSaved callback should be present");
  assert.ok(callbackEnd > callbackStart, "audioSaved callback boundary should be present");
  const callback = source.slice(callbackStart, callbackEnd);

  assert.match(callback, /decideAudioSavedAutomation\(/);
  assert.doesNotMatch(callback, /\badvance\s*\(/);
  assert.doesNotMatch(callback, /\bsetItemIdx\s*\(|\bsetTurnIdx\s*\(/);
  assert.doesNotMatch(callback, /screen:\s*"thanks"/);
  assert.doesNotMatch(source, /const \[autoAdvance|toggleAutoAdvance|checked=\{autoAdvance\}/);
  assert.doesNotMatch(source, /收音后自动推进/);
  assert.doesNotMatch(source, /(?:getItem|setItem)\("nmu:console:autoAdvance"/);
  assert.match(source, /retireLegacyAudioSavedAutoAdvancePreference\(localStorage\)/);
});
