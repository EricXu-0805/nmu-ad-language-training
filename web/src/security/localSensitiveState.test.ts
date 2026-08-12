import assert from "node:assert/strict";
import test from "node:test";
import {
  clearConsoleWorkspaceState,
  clearResearchBrowserState,
  clearSensitiveResearchState,
  isSensitiveResearchStorageKey,
  journalForLocalStorage,
  type StorageLike,
} from "./localSensitiveState.ts";

class MemoryStorage implements StorageLike {
  private values = new Map<string, string>();
  get length(): number { return this.values.size; }
  key(index: number): string | null { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string): void { this.values.delete(key); }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  has(key: string): boolean { return this.values.has(key); }
}

test("logout cleanup removes research identifiers but keeps device preferences", () => {
  const storage = new MemoryStorage();
  for (const key of [
    "nmu:pin", "nmu:device-capability:v1", "nmu:device-recovery-capabilities:v1",
    "nmu:device-id:v1",
    "nmu:console:state",
    "nmu:console:state:account%3Aresearcher-a",
    "nmu:console:state:researcher-a",
    "nmu:console:state:local%3AM0",
    "nmu:journal:S-1", "nmu:profile:P-1",
    "nmu:console:apFailure:S-1", "nmu:tts:log", "nmu:session", "nmu:cursor", "nmu:rapport",
    "nmu:tts", "nmu:tts:voice", "nmu:console:autoAdvance",
  ]) storage.setItem(key, "value");

  assert.equal(clearSensitiveResearchState(storage), 15);
  assert.equal(storage.has("nmu:pin"), false);
  assert.equal(storage.has("nmu:device-capability:v1"), false);
  assert.equal(storage.has("nmu:device-recovery-capabilities:v1"), false);
  assert.equal(storage.has("nmu:device-id:v1"), false);
  assert.equal(storage.has("nmu:console:state:account%3Aresearcher-a"), false);
  assert.equal(storage.has("nmu:console:state:researcher-a"), false);
  assert.equal(storage.has("nmu:console:state:local%3AM0"), false);
  assert.equal(storage.has("nmu:journal:S-1"), false);
  assert.equal(storage.has("nmu:profile:P-1"), false);
  assert.equal(storage.has("nmu:session"), false);
  assert.equal(storage.has("nmu:cursor"), false);
  assert.equal(storage.has("nmu:rapport"), false);
  assert.equal(storage.has("nmu:tts"), true);
  assert.equal(storage.has("nmu:tts:voice"), true);
  assert.equal(storage.has("nmu:console:autoAdvance"), true);
});

test("logout clears local and session research state without touching pending audio storage", () => {
  const local = new MemoryStorage();
  const session = new MemoryStorage();
  local.setItem("nmu:console:state", "patient P-1");
  local.setItem("nmu:journal:S-1", "transcript cache");
  local.setItem("nmu:patient-pause:v1", "opaque reduction-only intent");
  session.setItem("nmu:pin", "secret");
  session.setItem("nmu:device-capability:v1", "short-lived bearer");
  // IndexedDB is a separate store and is intentionally not an argument to this cleanup.
  const pendingIndexedDbAudio = new Map([["A-1", "unconfirmed-audio"]]);

  assert.equal(clearResearchBrowserState(local, session), 4);
  assert.equal(local.has("nmu:console:state"), false);
  assert.equal(local.has("nmu:journal:S-1"), false);
  assert.equal(local.has("nmu:patient-pause:v1"), true);
  assert.equal(session.has("nmu:pin"), false);
  assert.equal(session.has("nmu:device-capability:v1"), false);
  assert.equal(pendingIndexedDbAudio.get("A-1"), "unconfirmed-audio");
});

test("console crash recovery clears every current and legacy scoped workspace only", () => {
  const storage = new MemoryStorage();
  for (const key of [
    "nmu:console:state",
    "nmu:console:state:account%3Aresearcher-a",
    "nmu:console:state:researcher-a",
    "nmu:console:state:local%3AM0",
    "nmu:journal:S-1",
    "nmu:profile:P-1",
    "nmu:console:autoAdvance",
  ]) storage.setItem(key, "value");

  assert.equal(clearConsoleWorkspaceState(storage), 4);
  assert.equal(storage.has("nmu:console:state"), false);
  assert.equal(storage.has("nmu:console:state:account%3Aresearcher-a"), false);
  assert.equal(storage.has("nmu:console:state:researcher-a"), false);
  assert.equal(storage.has("nmu:console:state:local%3AM0"), false);
  assert.equal(storage.has("nmu:journal:S-1"), true);
  assert.equal(storage.has("nmu:profile:P-1"), true);
  assert.equal(storage.has("nmu:console:autoAdvance"), true);
});

test("key policy does not erase unrelated application data", () => {
  assert.equal(isSensitiveResearchStorageKey("nmu:journal:S-2"), true);
  assert.equal(isSensitiveResearchStorageKey("nmu:profile:P-2"), true);
  assert.equal(
    isSensitiveResearchStorageKey("nmu:console:state:account%3Aresearcher-b"),
    true,
  );
  assert.equal(isSensitiveResearchStorageKey("nmu:console:state:researcher-b"), true);
  assert.equal(isSensitiveResearchStorageKey("nmu:session"), true);
  assert.equal(isSensitiveResearchStorageKey("nmu:tts"), false);
  assert.equal(isSensitiveResearchStorageKey("other-app"), false);
});

test("journal persistence strips transcript text while retaining evidence references", () => {
  const original = {
    sessionId: "S-1",
    turns: {
      "A#1": {
        turnId: 7,
        asrText: "含敏感回答",
        confirmedText: "研究者确认文本",
        locked: true,
      },
    },
  };
  const safe = journalForLocalStorage(original);
  assert.deepEqual(safe.turns["A#1"], { turnId: 7, locked: true });
  assert.equal(original.turns["A#1"].asrText, "含敏感回答");
});
