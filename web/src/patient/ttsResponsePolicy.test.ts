import assert from "node:assert/strict";
import test from "node:test";

import { mustDiscardSynthesizedSpeech } from "./ttsResponsePolicy.ts";

test("post-synthesis authority loss discards bytes without system-speech resurrection", () => {
  assert.equal(mustDiscardSynthesizedSpeech(409, JSON.stringify({
    detail: {
      code: "tts_authorization_changed",
      action: "discard_synthesized_audio",
    },
  })), true);
  assert.equal(mustDiscardSynthesizedSpeech(409, JSON.stringify({
    code: "tts_authorization_changed",
    action: "discard_synthesized_audio",
  })), true);
});

test("unrelated errors retain the ordinary fallback policy", () => {
  assert.equal(mustDiscardSynthesizedSpeech(500, "provider unavailable"), false);
  assert.equal(mustDiscardSynthesizedSpeech(409, JSON.stringify({
    detail: { code: "another_conflict" },
  })), false);
  assert.equal(mustDiscardSynthesizedSpeech(409, "not-json"), false);
});
