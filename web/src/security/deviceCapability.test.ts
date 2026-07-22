import assert from "node:assert/strict";
import test from "node:test";
import {
  createDeviceCapabilityStore,
  DEVICE_CAPABILITY_STORAGE_KEY,
  DEVICE_ID_STORAGE_KEY,
  DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY,
  LEGACY_PIN_STORAGE_KEY,
  parseDevicePairResponse,
  type DeviceStorageLike,
} from "./deviceCapability.ts";

class MemoryStorage implements DeviceStorageLike {
  values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

class WriteThenThrowOnceStorage extends MemoryStorage {
  failKey: string | null = null;
  override setItem(key: string, value: string): void {
    super.setItem(key, value);
    if (this.failKey === key) {
      this.failKey = null;
      throw new Error("simulated storage failure after write");
    }
  }
}

const NOW = Date.parse("2026-07-18T00:00:00Z");
const TOKEN = "a".repeat(43);
const RESPONSE = {
  capability: TOKEN,
  sessionId: "S-2026-07-18",
  expiresAt: "2026-07-18T00:45:00+00:00",
};

test("upgrade destroys legacy PINs and persistent device credentials without migration", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  local.setItem(LEGACY_PIN_STORAGE_KEY, "legacy-local-pin");
  session.setItem(LEGACY_PIN_STORAGE_KEY, "legacy-session-pin");
  local.setItem(DEVICE_CAPABILITY_STORAGE_KEY, JSON.stringify(RESPONSE));
  local.setItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY, JSON.stringify([RESPONSE]));
  local.setItem(DEVICE_ID_STORAGE_KEY, "b".repeat(32));

  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "c".repeat(32));

  assert.equal(store.get(), null);
  assert.equal(local.getItem(LEGACY_PIN_STORAGE_KEY), null);
  assert.equal(session.getItem(LEGACY_PIN_STORAGE_KEY), null);
  assert.equal(local.getItem(DEVICE_CAPABILITY_STORAGE_KEY), null);
  assert.equal(local.getItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY), null);
  assert.equal(local.getItem(DEVICE_ID_STORAGE_KEY), null);
});

test("capability and random non-PII device id live only in sessionStorage", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "d".repeat(48));

  assert.deepEqual(store.save(RESPONSE), RESPONSE);
  assert.equal(store.get()?.capability, TOKEN);
  assert.equal(store.getOrCreateDeviceId(), "d".repeat(48));
  assert.equal(store.getOrCreateDeviceId(), "d".repeat(48));
  assert.equal(local.values.size, 0);
  assert.ok(session.getItem(DEVICE_CAPABILITY_STORAGE_KEY));
  assert.equal(session.getItem(DEVICE_ID_STORAGE_KEY), "d".repeat(48));

  store.clear();
  assert.equal(store.get(), null);
  assert.equal(session.getItem(DEVICE_ID_STORAGE_KEY), "d".repeat(48));
});

test("strict parser rejects unknown fields, malformed tokens, naive timestamps, and expiry", () => {
  assert.throws(() => parseDevicePairResponse({ ...RESPONSE, subject: "forbidden" }, NOW));
  assert.throws(() => parseDevicePairResponse({ ...RESPONSE, capability: "short" }, NOW));
  assert.throws(() => parseDevicePairResponse({ ...RESPONSE, expiresAt: "2026-07-18T00:45:00" }, NOW));
  assert.throws(() => parseDevicePairResponse({ ...RESPONSE, expiresAt: "2026-07-17T23:59:59Z" }, NOW));
});

test("stored records are revalidated and expired or malformed values are removed", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "e".repeat(48));
  session.setItem(DEVICE_CAPABILITY_STORAGE_KEY, JSON.stringify({ ...RESPONSE, expiresAt: "bad" }));

  assert.equal(store.get(), null);
  assert.equal(session.getItem(DEVICE_CAPABILITY_STORAGE_KEY), null);
});

test("session switch demotes active credential to recovery without promoting it on reload", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "f".repeat(48));
  const active = store.save(RESPONSE);
  store.retainForRecovery(active);
  store.clear();

  const reloaded = createDeviceCapabilityStore(local, session, () => NOW, () => "g".repeat(48));
  assert.equal(reloaded.get(), null);
  assert.deepEqual(reloaded.getRecovery(RESPONSE.sessionId), RESPONSE);
});

test("same-session re-pair removes the hard-revoked recovery token", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "h".repeat(48));
  store.retainForRecovery(RESPONSE);
  const replacement = { ...RESPONSE, capability: "z".repeat(43) };

  store.save(replacement);

  assert.deepEqual(store.get(), replacement);
  assert.equal(store.getRecovery(RESPONSE.sessionId), null);
});

test("multiple old-session recovery tokens are pruned independently by original TTL", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  let now = NOW;
  const store = createDeviceCapabilityStore(local, session, () => now, () => "i".repeat(48));
  const first = { ...RESPONSE, sessionId: "S-OLD-1", expiresAt: "2026-07-18T00:10:00Z" };
  const second = {
    ...RESPONSE,
    capability: "y".repeat(43),
    sessionId: "S-OLD-2",
    expiresAt: "2026-07-18T00:30:00Z",
  };
  store.retainForRecovery(first);
  store.retainForRecovery(second);
  now = Date.parse("2026-07-18T00:15:00Z");

  assert.equal(store.getRecovery("S-OLD-1"), null);
  assert.deepEqual(store.getRecovery("S-OLD-2"), second);
  const persisted = JSON.parse(
    session.getItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY) ?? "[]") as unknown[];
  assert.equal(persisted.length, 1);
});

test("clearing an exact old recovery slot leaves the active session untouched", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "m".repeat(48));
  const active = store.save(RESPONSE);
  const recovery = {
    ...RESPONSE,
    capability: "v".repeat(43),
    sessionId: "S-OLD",
  };
  store.retainForRecovery(recovery);

  assert.equal(store.removeRecoveryIfMatches(recovery), true);
  assert.deepEqual(store.get(), active);
  assert.equal(store.getRecovery(recovery.sessionId), null);
});

test("a late failure can clear or demote only its exact active token", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "j".repeat(48));
  const old = store.save(RESPONSE);
  const replacement = { ...RESPONSE, capability: "r".repeat(43) };
  store.save(replacement);

  assert.equal(store.clearIfMatches(old), false);
  assert.equal(store.demoteIfMatches(old), false);
  assert.deepEqual(store.get(), replacement);

  const oldRecovery = { ...old, sessionId: "S-RECOVERY" };
  const newRecovery = { ...oldRecovery, capability: "s".repeat(43) };
  store.retainForRecovery(oldRecovery);
  store.retainForRecovery(newRecovery);
  assert.equal(store.removeRecoveryIfMatches(oldRecovery), false);
  assert.deepEqual(store.getRecovery(oldRecovery.sessionId), newRecovery);
});

test("pair save rolls both slots back if storage throws after writing the new active token", () => {
  const local = new MemoryStorage();
  const session = new WriteThenThrowOnceStorage();
  const store = createDeviceCapabilityStore(local, session, () => NOW, () => "k".repeat(48));
  const oldActive = store.save(RESPONSE);
  const recovery = {
    ...RESPONSE,
    capability: "t".repeat(43),
    sessionId: "S-RECOVERY",
  };
  store.retainForRecovery(recovery);
  session.failKey = DEVICE_CAPABILITY_STORAGE_KEY;

  assert.throws(() => store.save({ ...RESPONSE, capability: "u".repeat(43) }));
  assert.deepEqual(store.get(), oldActive);
  assert.deepEqual(store.getRecovery(recovery.sessionId), recovery);
});

test("expiry crossing while a request is in flight still counts as exact-token removal", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  let now = NOW;
  const store = createDeviceCapabilityStore(local, session, () => now, () => "l".repeat(48));
  const inFlight = store.save(RESPONSE);
  now = Date.parse("2026-07-18T00:45:01Z");

  assert.equal(store.clearIfMatches(inFlight), true);
  assert.equal(store.get(), null);
  assert.equal(session.getItem(DEVICE_CAPABILITY_STORAGE_KEY), null);
});
