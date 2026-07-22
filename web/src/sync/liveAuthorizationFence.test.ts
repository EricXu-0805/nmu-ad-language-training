import assert from "node:assert/strict";
import test from "node:test";
import { LiveAuthorizationFence } from "./liveAuthorizationFence.ts";

test("a queued old-token 200 is rejected after active capability loss or replacement", () => {
  const fence = new LiveAuthorizationFence();
  const oldProbe = fence.capture("old-capability");
  fence.invalidate();

  assert.equal(fence.accepts(oldProbe, null), false);
  assert.equal(fence.accepts(oldProbe, "replacement-capability"), false);
  assert.equal(
    fence.accepts(fence.capture("replacement-capability"), "replacement-capability"),
    true,
  );
});

test("ordinary first-probe network failure does not invalidate the retry generation", () => {
  const fence = new LiveAuthorizationFence();
  const failedNetworkProbe = fence.capture(null);
  const retryProbe = fence.capture(null);

  assert.equal(fence.accepts(failedNetworkProbe, null), true);
  assert.equal(fence.accepts(retryProbe, null), true);
});
