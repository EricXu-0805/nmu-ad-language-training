import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import {
  clearConsoleState,
  consoleReducer,
  initialConsole,
  loadConsoleState,
  persistConsoleState,
} from "./consoleReducer.ts";

function session(runtime_status: Session["runtime_status"], week_no = 2): Session {
  return {
    session_id: "S-1",
    patient_id: "P-1",
    visit_plan_id: `vp_${"A".repeat(24)}`,
    session_sitting_no: 1,
    training_date: "2026-07-19",
    is_simulation: true,
    data_classification: "simulation",
    week_no,
    phase_type: week_no === 1 ? "关系建立" : "正式训练",
    event_line: week_no === 1 ? "关系建立环节" : "正式训练",
    trainer_id: "R-1",
    item_bank_version_id: "v1",
    item_bank_definition_digest: "1".repeat(64),
    autopilot_protocol_version_id: "autopilot-v1",
    autopilot_protocol_definition_digest: "2".repeat(64),
    // Plan-linked, so the frozen repeat pair is complete, exactly as the
    // backend serialises it; the strict reload parser requires both halves.
    repeat_protocol_version_id: "repeat-intent-v1-20260730-proposal",
    repeat_protocol_definition_digest: "3".repeat(64),
    // Canonical full-source plan: the demo profile pair is explicitly absent,
    // and the strict reload parser requires both keys to be present as null.
    autopilot_profile_version_id: null,
    autopilot_profile_definition_digest: null,
    runtime_status,
  };
}

const DEMO_PROFILE_VERSION = "week2-single20-demo-v1";
const DEMO_PROFILE_DIGEST =
  "089c44fc5f20b541b374b24289693e066550acf6999e0b5dc382cd5f10ba71fc";

// The key production actually reads.  The legacy `…:LOCAL-M0` key is deleted
// on every load and never parsed, so seeding it would prove nothing.
const CURRENT_CACHE_KEY = "nmu:console:state:local%3AM0";

function withLocalStorage(store: Map<string, string>, run: () => void): void {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value); },
      removeItem: (key: string) => { store.delete(key); },
    },
  });
  try {
    run();
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
}

/** The exact v2 envelope shape production writes and requires on read. */
function envelope(sessionRow: unknown) {
  return JSON.stringify({
    version: 2,
    identityScope: "local:M0",
    state: {
      area: "run", screen: "training", patientId: "P-1", session: sessionRow,
    },
  });
}

test("canonical cached session round-trips with both profile fields null", () => {
  const store = new Map<string, string>();

  withLocalStorage(store, () => {
    persistConsoleState({
      area: "run", screen: "training", patientId: "P-1",
      session: session("active"),
    });
    assert.equal(store.has(CURRENT_CACHE_KEY), true);

    const restored = loadConsoleState();
    assert.equal(restored.screen, "training");
    assert.equal(restored.session?.session_id, "S-1");
    assert.equal(
      Object.hasOwn(restored.session ?? {}, "autopilot_profile_version_id"), true);
    assert.equal(
      Object.hasOwn(restored.session ?? {}, "autopilot_profile_definition_digest"),
      true);
    assert.equal(restored.session?.autopilot_profile_version_id, null);
    assert.equal(restored.session?.autopilot_profile_definition_digest, null);
    assert.equal(store.has(CURRENT_CACHE_KEY), true);
  });
});

test("the exact D1B paired-set cached session restores without losing its frozen facts", () => {
  // Seeded directly rather than through persistConsoleState: that writer also
  // validates, so a rejection there would prove the write path, not the read
  // path this attack is aimed at.  Known version, valid lowercase 64-hex
  // digest and plan-linked simulation row: this is the exact D1B runtime shape.
  const store = new Map<string, string>([[
    CURRENT_CACHE_KEY,
    envelope({
      ...session("active"),
      autopilot_profile_version_id: DEMO_PROFILE_VERSION,
      autopilot_profile_definition_digest: DEMO_PROFILE_DIGEST,
    }),
  ]]);

  withLocalStorage(store, () => {
    const restored = loadConsoleState();
    assert.equal(restored.screen, "training");
    assert.equal(restored.session?.autopilot_profile_version_id, DEMO_PROFILE_VERSION);
    assert.equal(restored.session?.autopilot_profile_definition_digest, DEMO_PROFILE_DIGEST);
    assert.equal(store.has(CURRENT_CACHE_KEY), true);
  });
});

test("a drifted paired-set cached session is discarded entirely", () => {
  const store = new Map<string, string>([[
    CURRENT_CACHE_KEY,
    envelope({
      ...session("active"),
      autopilot_profile_version_id: DEMO_PROFILE_VERSION,
      autopilot_profile_definition_digest: "0".repeat(64),
    }),
  ]]);

  withLocalStorage(store, () => {
    assert.deepEqual(loadConsoleState(), initialConsole);
    assert.equal(store.has(CURRENT_CACHE_KEY), false);
  });
});

test("terminal sessions enter wrapup while active sessions enter their live screen", () => {
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("active") }).screen, "training");
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("paused", 1) }).screen, "relationship");
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("intervention_completed") }).screen, "wrapup");
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("completed") }).screen, "wrapup");
});

test("Week-1 baseline and pretest never route to the relationship page", () => {
  for (const phase_type of ["基线测评", "前测"] as const) {
    const candidate = {
      ...session("active", 1),
      phase_type,
      event_line: "基线测评窗" as const,
    };
    assert.equal(
      consoleReducer(initialConsole, { t: "sessionStarted", session: candidate }).screen,
      "unsupported",
    );
  }
});

test("research-review and completed states never navigate back to training", () => {
  for (const status of ["intervention_completed", "completed", "aborted", "failed"] as const) {
    const started = consoleReducer(initialConsole, { t: "sessionStarted", session: session(status) });
    assert.equal(started.screen, "wrapup");
    assert.equal(consoleReducer(started, { t: "backToSession" }).screen, "wrapup");
  }
});

test("final completion updates the held session without leaving the wrapup page", () => {
  const started = consoleReducer(initialConsole, {
    t: "sessionStarted",
    session: session("intervention_completed"),
  });
  const completed = consoleReducer(started, { t: "sessionRuntimeUpdated", status: "completed" });
  assert.equal(completed.screen, "wrapup");
  assert.equal(completed.session?.runtime_status, "completed");
});

test("legacy bedside session-new cache degrades to the server-owned today queue", () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const store = new Map<string, string>([[
    "nmu:console:state:LOCAL-M0",
    JSON.stringify({
      area: "run",
      screen: "sessionNew",
      patientId: "P-legacy",
      session: session("active"),
    }),
  ]]);
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      removeItem: (key: string) => { store.delete(key); },
    },
  });
  try {
    assert.deepEqual(loadConsoleState(), initialConsole);
    assert.equal(store.has("nmu:console:state:LOCAL-M0"), false);
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("workspace cache is account-scoped and unscoped legacy state is discarded", () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value); },
      removeItem: (key: string) => { store.delete(key); },
    },
  });
  try {
    const accountA = { username: "account-a" };
    const accountB = { username: "account-b" };
    const activeA = consoleReducer(initialConsole, {
      t: "sessionStarted", session: session("active"),
    });
    persistConsoleState(activeA, accountA);
    store.set("nmu:console:state", JSON.stringify(activeA));

    assert.equal(loadConsoleState(accountA).session?.session_id, "S-1");
    assert.deepEqual(loadConsoleState(accountB), initialConsole);
    assert.equal(store.has("nmu:console:state"), false);

    const accountAKey = "nmu:console:state:account%3Aaccount-a";
    const accountBKey = "nmu:console:state:account%3Aaccount-b";
    assert.equal(store.has(accountAKey), true);
    store.set(accountBKey, store.get(accountAKey) as string);
    assert.deepEqual(loadConsoleState(accountB), initialConsole);
    assert.equal(store.has(accountBKey), false);

    clearConsoleState(accountA);
    assert.deepEqual(loadConsoleState(accountA), initialConsole);
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("malformed or forged workspace caches fail closed and are removed", () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const key = "nmu:console:state:account%3Aresearcher";
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (name: string) => store.get(name) ?? null,
      setItem: (name: string, value: string) => { store.set(name, value); },
      removeItem: (name: string) => { store.delete(name); },
    },
  });
  const identity = { username: "researcher" };
  const envelope = (state: unknown, extra: Record<string, unknown> = {}) => JSON.stringify({
    version: 2,
    identityScope: "account:researcher",
    state,
    ...extra,
  });
  const validState = {
    area: "run",
    screen: "training",
    patientId: null,
    session: session("active"),
  };
  try {
    const invalidPayloads = [
      "{not-json",
      envelope(validState, { injected: true }),
      JSON.stringify({ version: 2, identityScope: "account:someone-else", state: validState }),
      envelope({ ...validState, injected: true }),
      envelope({ ...validState, session: { ...session("active"), injected: true } }),
      envelope({ ...validState, patientId: "P-OTHER" }),
      envelope({ ...validState, screen: "picker" }),
      envelope({ ...validState, session: { ...session("active"), runtime_status: "unknown" } }),
      // Half a repeat binding is never a legacy shape: a cache carrying only
      // one side must be discarded, not reloaded as a pre-protocol session.
      envelope({ ...validState,
        session: { ...session("active"), repeat_protocol_definition_digest: null } }),
      envelope({ ...validState,
        session: { ...session("active"), repeat_protocol_version_id: null } }),
    ];
    for (const payload of invalidPayloads) {
      store.set(key, payload);
      assert.deepEqual(loadConsoleState(identity), initialConsole);
      assert.equal(store.has(key), false);
    }
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});

test("persistence normalizes a server-started session before strict reload", () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
  const store = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value); },
      removeItem: (key: string) => { store.delete(key); },
    },
  });
  try {
    const identity = { username: "researcher" };
    const started = consoleReducer(initialConsole, {
      t: "sessionStarted",
      session: { ...session(undefined), runtime_status: undefined },
    });
    persistConsoleState(started, identity);
    const restored = loadConsoleState(identity);
    assert.equal(restored.session?.runtime_status, "active");
    assert.equal(restored.session?.session_id, "S-1");
  } finally {
    if (previous) Object.defineProperty(globalThis, "localStorage", previous);
    else Reflect.deleteProperty(globalThis, "localStorage");
  }
});
