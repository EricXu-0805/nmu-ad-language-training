import assert from "node:assert/strict";
import test from "node:test";
import { deliverAuthoritativeAudioSaved } from "./audioSavedAuthority.ts";
import type { AudioSavedMsg } from "./messages.ts";

const validServerPayload = {
  rawAudioId: "aud-real",
  durationSeconds: 2.5,
  byteCount: 321,
  checksum: "a".repeat(64),
  turnKey: "SE_锚#1",
  sessionId: "S-1",
  containsDirectIdentifier: false,
};

test("malformed hints cannot poison seen before a valid server receipt arrives", () => {
  const seen = new Set<string>();
  const delivered: AudioSavedMsg[] = [];
  assert.equal(deliverAuthoritativeAudioSaved(
    { ...validServerPayload, durationSeconds: Number.NaN }, seen, (message) => delivered.push(message),
  ), false);
  assert.equal(seen.size, 0);

  assert.equal(deliverAuthoritativeAudioSaved(
    validServerPayload, seen, (message) => delivered.push(message),
  ), true);
  assert.equal(seen.has("aud-real"), true);
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0]?.type, "audioSaved");
});

test("only a payload-only server projection can deliver and duplicates remain idempotent", () => {
  const seen = new Set<string>();
  let delivered = 0;
  const receive = () => { delivered += 1; };

  // A BroadcastChannel-shaped object is not accepted as server authority.
  assert.equal(deliverAuthoritativeAudioSaved(
    { type: "audioSaved", ...validServerPayload }, seen, receive,
  ), false);
  assert.equal(delivered, 0);
  assert.equal(deliverAuthoritativeAudioSaved(validServerPayload, seen, receive), true);
  assert.equal(deliverAuthoritativeAudioSaved(validServerPayload, seen, receive), false);
  assert.equal(delivered, 1);
});
