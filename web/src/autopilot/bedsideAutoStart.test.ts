import assert from "node:assert/strict";
import test from "node:test";
import type { Session } from "../types.ts";
import {
  canAutoStartServerAutopilot,
  latchBedsideActivation,
  type BedsideAutoStartGates,
} from "./bedsideAutoStart.ts";
import {
  autopilotConsoleReducer,
  autopilotServerOwnsConsole,
  initialAutopilotConsoleState,
  p0aConsoleEligibility,
} from "./startControl.ts";

const SESSION_ID = "S-a1b2c3d4";

const SIMULATION_SESSION: Session = {
  session_id: SESSION_ID,
  patient_id: "SIM-001",
  is_simulation: true,
  data_classification: "simulation",
  week_no: 2,
  phase_type: "正式训练",
  event_line: "正式训练",
  item_bank_version_id: "bank-v1",
  runtime_status: "active",
};

function greenGates(overrides: Partial<BedsideAutoStartGates> = {}): BedsideAutoStartGates {
  return {
    sessionId: SESSION_ID,
    latchedActivationSessionId: SESSION_ID,
    eligibilityAllowed: true,
    completePlanBlocked: false,
    providerStartAllowed: true,
    phase: "idle",
    receiptProvesNoOwner: true,
    interactionBlocked: false,
    patientMicOn: false,
    planPositionReady: true,
    startInFlight: false,
    alreadyAttempted: false,
    ...overrides,
  };
}

// 复现操作端的组合行为:锁存 + 同步 attempted 闸 + 每次渲染重评策略。
// 返回真实会发出的自动启动写请求次数。
function renderCycles(
  cycles: { eventSessionId?: unknown; gates?: Partial<BedsideAutoStartGates> }[],
): number {
  let latched: string | null = null;
  let attempted = false;
  let posts = 0;
  for (const cycle of cycles) {
    if ("eventSessionId" in cycle) {
      latched = latchBedsideActivation(latched, cycle.eventSessionId, SESSION_ID);
    }
    const gates = greenGates({
      ...cycle.gates,
      latchedActivationSessionId: latched,
      alreadyAttempted: attempted,
    });
    if (canAutoStartServerAutopilot(gates)) {
      attempted = true;
      posts += 1;
    }
  }
  return posts;
}

test("one exact-session activation with every gate green starts exactly once", () => {
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID },
    {}, {}, {},
  ]), 1);
});

test("a signal that arrives before readiness waits, then starts once when released", () => {
  assert.equal(renderCycles([
    {
      eventSessionId: SESSION_ID,
      gates: { phase: "checking", receiptProvesNoOwner: false, providerStartAllowed: false },
    },
    { gates: { providerStartAllowed: false } },
    {},
    {},
  ]), 1);
});

test("duplicate events, re-renders and StrictMode replays never produce a second POST", () => {
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID },
    { eventSessionId: SESSION_ID },
    { eventSessionId: SESSION_ID },
    {}, {},
  ]), 1);
});

test("stale, empty or malformed session ids never latch", () => {
  assert.equal(latchBedsideActivation(null, "S-old", SESSION_ID), null);
  assert.equal(latchBedsideActivation(null, "", SESSION_ID), null);
  assert.equal(latchBedsideActivation(null, 42, SESSION_ID), null);
  assert.equal(latchBedsideActivation(null, undefined, SESSION_ID), null);
  // 已锁存的当前场次不会被旧场次的迟到事件顶掉。
  assert.equal(latchBedsideActivation(SESSION_ID, "S-old", SESSION_ID), SESSION_ID);
  assert.equal(renderCycles([{ eventSessionId: "S-old" }, {}]), 0);
});

test("unpassed provider readiness blocks auto start entirely", () => {
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID, gates: { providerStartAllowed: false } },
    { gates: { providerStartAllowed: false } },
  ]), 0);
});

test("the complete-plan content gate blocks auto start", () => {
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID, gates: { completePlanBlocked: true } },
    { gates: { completePlanBlocked: true } },
  ]), 0);
});

test("interaction blockers, live microphone and missing plan position block auto start", () => {
  assert.equal(canAutoStartServerAutopilot(greenGates({ interactionBlocked: true })), false);
  assert.equal(canAutoStartServerAutopilot(greenGates({ patientMicOn: true })), false);
  assert.equal(canAutoStartServerAutopilot(greenGates({ planPositionReady: false })), false);
});

test("only the proven-idle phase may auto start", () => {
  for (const phase of [
    "checking", "starting", "uncertain", "rejected", "running", "waiting_tts",
    "waiting_recording", "processing_attempt", "manual_draining", "paused",
    "scope_completed", "failed",
  ] as const) {
    assert.equal(canAutoStartServerAutopilot(greenGates({ phase })), false);
  }
  assert.equal(canAutoStartServerAutopilot(greenGates({ startInFlight: true })), false);
});

test("a lost start response locks the manual plane and is never auto-retried", () => {
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID },
    { gates: { phase: "uncertain" } },
    { gates: { phase: "uncertain" } },
    // 即使权威查询稍后又证明 idle,本激活周期的写请求额度已经用掉。
    {},
  ]), 1);
  const uncertain = autopilotConsoleReducer(
    initialAutopilotConsoleState(SESSION_ID),
    { type: "status_uncertain", sessionId: SESSION_ID, error: "响应丢失" },
  );
  assert.equal(autopilotServerOwnsConsole(uncertain), true);
});

test("a proven research session may auto start once when every gate is green", () => {
  const research: Session = {
    ...SIMULATION_SESSION,
    is_simulation: false,
    data_classification: "research",
  };
  const eligibility = p0aConsoleEligibility(research);
  assert.equal(eligibility.allowed, true);
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID, gates: { eligibilityAllowed: eligibility.allowed } },
    { gates: { eligibilityAllowed: eligibility.allowed } },
  ]), 1);
});

test("classification-unverified sessions can never auto start", () => {
  for (const spoiled of [
    { ...SIMULATION_SESSION, data_classification: undefined },
    { ...SIMULATION_SESSION, data_classification: "legacy_unknown" as const },
    { ...SIMULATION_SESSION, is_simulation: false },
    { ...SIMULATION_SESSION, data_classification: "research" as const },
  ] satisfies Session[]) {
    const eligibility = p0aConsoleEligibility(spoiled);
    assert.deepEqual(eligibility, { allowed: false, reason: "classification_unverified" });
    assert.equal(renderCycles([
      { eventSessionId: SESSION_ID, gates: { eligibilityAllowed: eligibility.allowed } },
      { gates: { eligibilityAllowed: eligibility.allowed } },
    ]), 0);
  }
});

test("re-opening the bedside overlay while the server owns control never re-posts", () => {
  const serverOwnedCycle = {
    gates: { phase: "waiting_tts" as const, receiptProvesNoOwner: false },
  };
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID },
    serverOwnedCycle,
    { eventSessionId: SESSION_ID, ...serverOwnedCycle },
    serverOwnedCycle,
  ]), 1);
  // 新标签页(attempted 归零)在服务器已持有时同样不写。
  assert.equal(canAutoStartServerAutopilot(
    greenGates({ phase: "waiting_tts", receiptProvesNoOwner: false })), false);
});
