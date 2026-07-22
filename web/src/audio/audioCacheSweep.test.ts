import assert from "node:assert/strict";
import test from "node:test";
import { serverConfirmsUploadedAudio } from "./audioCachePolicy.ts";
import { AudioSweepPreservationRegistry } from "./audioSweepPreservation.ts";

test("legacy cache sweep only accepts a matching server asset with a real checksum", () => {
  const checksum = "1".repeat(64);
  assert.equal(serverConfirmsUploadedAudio("aud-1", { raw_audio_id: "aud-1", checksum }), true);
  assert.equal(serverConfirmsUploadedAudio("aud-1", { raw_audio_id: "aud-2", checksum }), false);
  assert.equal(serverConfirmsUploadedAudio("aud-1", { raw_audio_id: "aud-1", checksum: null }), false);
  assert.equal(serverConfirmsUploadedAudio("aud-1", { raw_audio_id: "aud-1", checksum: "pending" }), false);
});

test("overlapping sweep claims are visible to every active caller", () => {
  const registry = new AudioSweepPreservationRegistry();
  const first = registry.claim(new Set(["shared", "first-only"]));
  const second = registry.claim(new Set(["shared", "late-claim"]));

  assert.equal(registry.isPreserved("late-claim"), true);
  first.release();
  assert.equal(registry.isPreserved("shared"), true);
  assert.equal(registry.isPreserved("first-only"), false);
  second.release();
  second.release();
  assert.equal(registry.isPreserved("shared"), false);
  assert.equal(registry.isPreserved("late-claim"), false);
});
