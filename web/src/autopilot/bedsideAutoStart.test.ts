import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { Session } from "../types.ts";
import {
  bedsideActivationRefusedBeforeStart,
  canAutoStartServerAutopilot,
  latchBedsideActivation,
  type BedsideAutoStartGates,
} from "./bedsideAutoStart.ts";
import {
  autopilotConsoleReducer,
  autopilotServerOwnsConsole,
  initialAutopilotConsoleState,
  p0aConsoleEligibility,
  type P0aConsoleEligibility,
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

test("resetting the start position cannot retry a previously attempted bedside start", () => {
  assert.equal(canAutoStartServerAutopilot(greenGates({ alreadyAttempted: true })), false);
});

type ConsoleRefusal = Extract<P0aConsoleEligibility, { allowed: false }>["reason"];

// 复现操作端的组合行为:锁存 + 确定拒因用掉激活 + 同步 attempted 闸 + 每次渲染重评策略。
// 返回真实会发出的自动启动写请求次数。
function renderCycles(
  cycles: {
    eventSessionId?: unknown;
    gates?: Partial<BedsideAutoStartGates>;
    refusal?: ConsoleRefusal | null;
  }[],
): number {
  let latched: string | null = null;
  let attempted = false;
  let posts = 0;
  for (const cycle of cycles) {
    if ("eventSessionId" in cycle) {
      latched = latchBedsideActivation(latched, cycle.eventSessionId, SESSION_ID);
    }
    if (bedsideActivationRefusedBeforeStart(SESSION_ID, latched, cycle.refusal ?? null)) {
      attempted = true;
      continue;
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
  // 「每道门都绿」含受试者云处理授权已核实(2026-11-27 起真实研究场次的启动门禁)。
  const eligibility = p0aConsoleEligibility(research, false, true, true);
  assert.equal(eligibility.allowed, true);
  assert.equal(renderCycles([
    { eventSessionId: SESSION_ID, gates: { eligibilityAllowed: eligibility.allowed } },
    { gates: { eligibilityAllowed: eligibility.allowed } },
  ]), 1);
});

test("a research session without proven cloud authorization never auto starts from the bedside", () => {
  const research: Session = {
    ...SIMULATION_SESSION,
    is_simulation: false,
    data_classification: "research",
  };
  for (const cloud of [false, null]) {
    const eligibility = p0aConsoleEligibility(research, false, true, cloud);
    assert.equal(eligibility.allowed, false);
    assert.equal(renderCycles([
      { eventSessionId: SESSION_ID, gates: { eligibilityAllowed: eligibility.allowed } },
      { gates: { eligibilityAllowed: eligibility.allowed } },
    ]), 0);
  }
});

const RESEARCH_SESSION: Session = {
  ...SIMULATION_SESSION,
  is_simulation: false,
  data_classification: "research",
};

// 一拍渲染:受试者云授权读数 → 与组件同一个 p0aConsoleEligibility → 门禁与拒因。
function researchCycle(cloud: boolean | null, eventSessionId?: string) {
  const eligibility = p0aConsoleEligibility(RESEARCH_SESSION, false, true, cloud);
  return {
    ...(eventSessionId === undefined ? {} : { eventSessionId }),
    gates: { eligibilityAllowed: eligibility.allowed },
    refusal: eligibility.allowed ? null : eligibility.reason,
  };
}

// 复核 F1:改前未授权受试者的床旁自动启动会发一次、被服务端 409
// autopilot_cloud_processing_required 拒掉,激活随之用掉。前移门禁后若不在这里用掉,
// 研究者改走人工、别处补完授权,30 s 重读一放行,本场就会无人点击自己开跑。
test("云授权明确缺失时床旁激活就此用掉:之后补完授权也不会无人点击自己开跑", () => {
  // 激活先到(授权还在核对),随后几拍读到明确缺失,再之后别处补完授权。
  assert.equal(renderCycles([
    researchCycle(null, SESSION_ID),
    researchCycle(false), researchCycle(false), researchCycle(false),
    researchCycle(true), researchCycle(true), researchCycle(true),
  ]), 0);
  // 明确缺失之后才到的激活同样当场用掉;授权后老人端再点也不重发。
  assert.equal(renderCycles([
    researchCycle(false), researchCycle(false, SESSION_ID), researchCycle(false),
    researchCycle(true), researchCycle(true, SESSION_ID), researchCycle(true),
  ]), 0);
  // 只是还在核对(没读到、首读失败)不是拒绝:授权一核实,已授权受试者照旧自动启动一次。
  assert.equal(renderCycles([
    researchCycle(null, SESSION_ID), researchCycle(null), researchCycle(null),
    researchCycle(true), researchCycle(true),
  ]), 1);
  // 没锁存、或锁存的不是本场,不用掉任何东西;其他拒因(含核对中)也不在此列。
  assert.equal(bedsideActivationRefusedBeforeStart(SESSION_ID, null, "cloud_processing_required"), false);
  assert.equal(bedsideActivationRefusedBeforeStart(SESSION_ID, "S-old", "cloud_processing_required"), false);
  assert.equal(bedsideActivationRefusedBeforeStart(SESSION_ID, SESSION_ID, "cloud_processing_required"), true);
  for (const refusal of [
    null, "cloud_processing_unverified", "runtime_blocked", "account_required",
    "scope_unsupported", "classification_unverified",
  ] as const) {
    assert.equal(bedsideActivationRefusedBeforeStart(SESSION_ID, SESSION_ID, refusal), false, String(refusal));
  }
});

test("ServerAutopilotControl:云授权明确缺失先用掉激活,再谈自动启动;面板不再承诺自动放行", () => {
  // 本测试环境不能 mount React,以接线守卫锁住关键连接。
  const source = readFileSync(
    new URL("../console/scoring/ServerAutopilotControl.tsx", import.meta.url), "utf8");
  assert.match(source, /const eligibilityRefusal = eligibility\.allowed \? null : eligibility\.reason;/);
  const effect = source.slice(
    source.indexOf("if (!allowsBedsideDefaultStart("),
    source.indexOf("if (!classificationProven) return null;"));
  assert.match(effect,
    /if \(bedsideActivationRefusedBeforeStart\(session\.session_id, bedsideActivation, eligibilityRefusal\)\) \{\s*autoStartAttempted\.current = true;\s*return;\s*\}/);
  assert.ok(effect.indexOf("bedsideActivationRefusedBeforeStart(") < effect.indexOf("canAutoStartServerAutopilot("),
    "必须在自动启动判定之前用掉");
  // 核对中 → 明确缺失时 allowed 一直是 false,依赖里必须有拒因本身,否则 effect 不重跑、激活用不掉。
  assert.match(effect, /\}, \[[^\]]*\beligibilityRefusal\b[^\]]*\]\);/);
  assert.doesNotMatch(source, /自动放行/);
  assert.match(source,
    /cloudBlocked \? \(\s*<>\{AUTOPILOT_CLOUD_AUTHORIZATION_REQUIRED_HINT\}。授权保存后半分钟内启动按钮恢复可点，届时请研究者自己点「启动 AI 自动带练」/);
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
