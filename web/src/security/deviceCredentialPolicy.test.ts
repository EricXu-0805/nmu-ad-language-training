import assert from "node:assert/strict";
import { test } from "node:test";
import { pickDeviceCredential } from "./deviceCredentialPolicy.ts";

const active = { sessionId: "S-B", capability: "cap-b" };
const recoveryA = { sessionId: "S-A", capability: "cap-a-recovery" };

test("bound session uses the active credential", () => {
  const pick = pickDeviceCredential({ active, recovery: null }, "S-B");
  assert.equal(pick.source, "active");
});

test("foreign session prefers its own recovery credential when allowed", () => {
  const pick = pickDeviceCredential({ active, recovery: recoveryA }, "S-A", { allowRecovery: true, activeFallback: true });
  assert.equal(pick.source, "recovery");
  assert.equal(pick.source === "recovery" ? pick.capability : "", "cap-a-recovery");
});

test("foreign session without recovery credential falls back to the active one only when asked", () => {
  const silent = pickDeviceCredential({ active, recovery: null }, "S-A", { allowRecovery: true });
  assert.equal(silent.source, null);
  const probe = pickDeviceCredential({ active, recovery: null }, "S-A", { allowRecovery: true, activeFallback: true });
  assert.equal(probe.source, "active-foreign");
  assert.equal(probe.source === "active-foreign" ? probe.capability : "", "cap-b");
});

test("no session id never triggers the foreign fallback", () => {
  const pick = pickDeviceCredential({ active, recovery: null }, undefined, { activeFallback: true });
  assert.equal(pick.source, "active");
  const none = pickDeviceCredential({ active: null, recovery: null }, undefined, { activeFallback: true });
  assert.equal(none.source, null);
});
