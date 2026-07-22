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
    runtime_status,
  };
}

test("terminal sessions enter wrapup while active sessions enter their live screen", () => {
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("active") }).screen, "training");
  assert.equal(consoleReducer(initialConsole, { t: "sessionStarted", session: session("paused", 1) }).screen, "unsupported");
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
