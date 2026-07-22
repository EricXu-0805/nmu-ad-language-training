import assert from "node:assert/strict";
import test from "node:test";
import {
  automaticCacheDeleteDecision,
  createLegacyAudioOrphan,
  findLegacyBlobIds,
  parseLegacyAudioOrphan,
} from "./audioStoragePolicy.ts";

test("v1/v2 blobs without an outbox are deterministically quarantined", () => {
  assert.deepEqual(
    findLegacyBlobIds(["orphan-b", "tracked", "orphan-a"], ["tracked"]),
    ["orphan-a", "orphan-b"],
  );
  assert.deepEqual(findLegacyBlobIds(["marked"], [], ["marked"]), []);
});

test("legacy disposition markers are strict untrusted-storage records", () => {
  const marker = createLegacyAudioOrphan("aud-legacy", 1, 100);
  assert.equal(parseLegacyAudioOrphan(marker).rawAudioId, "aud-legacy");
  assert.throws(() => parseLegacyAudioOrphan({ ...marker, rawAudioId: "../escape" }), /无效/);
  assert.throws(() => parseLegacyAudioOrphan({ ...marker, extra: true }), /无效/);
});

test("automatic cleanup never deletes outbox or legacy-disposition bytes", () => {
  assert.equal(automaticCacheDeleteDecision({ explicitlyPreserved: false, hasOutbox: false, legacyMarked: false }), "delete");
  assert.equal(automaticCacheDeleteDecision({ explicitlyPreserved: true, hasOutbox: false, legacyMarked: false }), "preserve-request");
  assert.equal(automaticCacheDeleteDecision({ explicitlyPreserved: false, hasOutbox: true, legacyMarked: false }), "preserve-outbox");
  assert.equal(automaticCacheDeleteDecision({ explicitlyPreserved: false, hasOutbox: false, legacyMarked: true }), "preserve-legacy");
});
