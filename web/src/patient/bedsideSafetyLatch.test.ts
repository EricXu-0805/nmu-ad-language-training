import assert from "node:assert/strict";
import test from "node:test";
import {
  bedsideSafetyIsLatched,
  latchBedsideSafetyStop,
  reconcileBedsideSafetyLatch,
} from "./bedsideSafetyLatch.ts";

test("an old active projection cannot release a newly received bedside safety stop", () => {
  const stopped = latchBedsideSafetyStop("S-A", "S-A");
  assert.equal(bedsideSafetyIsLatched(stopped, "S-A"), true);
  assert.deepEqual(reconcileBedsideSafetyLatch(stopped, "S-A", false), stopped);
});

test("the latch releases only after authoritative paused then resumed projections", () => {
  const stopped = latchBedsideSafetyStop("S-A", "S-A");
  const paused = reconcileBedsideSafetyLatch(stopped, "S-A", true);
  assert.equal(paused?.observedServerPause, true);
  assert.equal(reconcileBedsideSafetyLatch(paused, "S-A", false), null);
});

test("cross-session safety stops are ignored and a session switch drops the old latch", () => {
  assert.equal(latchBedsideSafetyStop("S-B", "S-A"), null);
  const stopped = latchBedsideSafetyStop("S-A", "S-A");
  assert.equal(reconcileBedsideSafetyLatch(stopped, "S-B", false), null);
});

test("a repeated stop received while already server-paused remains releasable on resume", () => {
  const repeated = reconcileBedsideSafetyLatch(
    latchBedsideSafetyStop("S-A", "S-A"), "S-A", true,
  );
  assert.equal(repeated?.observedServerPause, true);
  assert.equal(reconcileBedsideSafetyLatch(repeated, "S-A", false), null);
});
