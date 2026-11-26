import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import type { VisitPlanReceipt } from "../types.ts";
import { isSameApprovedVisitPlan, reconcileStartedVisitPlan } from "../visitPlans.ts";
import { createAsyncOperationFence, deliverIfCurrent } from "./runPickerStartFence.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

test("an invalidated start request cannot drive the console after its screen unmounts", async () => {
  const fence = createAsyncOperationFence();
  const generation = fence.begin();
  const response = deferred<{ session_id: string }>();
  const resumed: string[] = [];

  const delivery = deliverIfCurrent(
    fence,
    generation,
    () => response.promise,
    (session) => resumed.push(session.session_id),
  );
  fence.invalidate();
  response.resolve({ session_id: "S-STALE" });

  assert.equal(await delivery, false);
  assert.deepEqual(resumed, []);
});

test("the current start request may deliver its validated session exactly once", async () => {
  const fence = createAsyncOperationFence();
  const generation = fence.begin();
  const resumed: string[] = [];
  assert.equal(await deliverIfCurrent(
    fence,
    generation,
    async () => ({ session_id: "S-CURRENT" }),
    (session) => resumed.push(session.session_id),
  ), true);
  assert.deepEqual(resumed, ["S-CURRENT"]);
});

// 执行 RunPickerScreen 里的 reconcileAfterStartRequest 源码本身,周围的组件状态换成记录器。
function reconcileSource(): string {
  const file = "./RunPickerScreen.tsx";
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: string | null = null;
  const visit = (node: ts.Node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "reconcileAfterStartRequest") {
      found = node.getText(sf);
    }
    if (!found) ts.forEachChild(node, visit);
  };
  visit(sf);
  assert.ok(found, "production reconcile must exist");
  return found;
}

function reconcileHarness(serverPlans: VisitPlanReceipt[]) {
  const events: unknown[][] = [];
  const startKeys = new Map([["vp-1:2", "vp-start-key"]]);
  const fence = createAsyncOperationFence();
  const context = vm.createContext({
    module: { exports: {} },
    api: { listPatientVisitPlans: async () => serverPlans },
    startFence: { current: fence },
    startKeys: { current: startKeys },
    reconcileStartedVisitPlan,
    isSameApprovedVisitPlan,
    planStatusLabel: (status: string) => status,
    errorText: (error: unknown) => String(error),
    enterStartedSession: async (...args: unknown[]) => { events.push(["enter", ...args]); return true; },
    rememberPending: (...args: unknown[]) => events.push(["pending", args[2]]),
    setPendingStart: (value: unknown) => events.push(["setPendingStart", value]),
    setStartError: (value: unknown) => events.push(["setStartError", value]),
  });
  vm.runInContext(ts.transpileModule(`${reconcileSource()}\nmodule.exports = reconcileAfterStartRequest;`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context);
  const reconcile = (context.module as {
    exports: (...args: unknown[]) => Promise<boolean>;
  }).exports;
  return { events, startKeys, run: (plan: VisitPlanReceipt) =>
    reconcile(plan, "vp-start-key", new Error("409"), false, fence.begin()) };
}

const approvedPlan = {
  plan_id: "vp-1", patient_id: "P-RUN-01", scheduled_date: "2026-11-26",
  scheduled_time: "09:30:00", queue_order: 1, session_sitting_no: 1, week_no: 2,
  phase_type: "正式训练", event_line: "正式训练", item_bank_version_id: "wk2-v1-20260707",
  autopilot_profile_version_id: null, is_simulation: true, data_classification: "simulation",
  status: "approved", revision: 2,
  created_by: "ACTOR-researcher", created_at: "2026-11-26T01:00:00",
  approved_by: "ACTOR-reviewer", approved_at: "2026-11-26T01:01:00",
  started_by: null, started_at: null, cancelled_by: null, cancelled_at: null, session_id: null,
} as VisitPlanReceipt;

test("a plan another account already started clears the pending start and says so", async () => {
  // 服务端对非管理员、非本人开的场隐去开始人与场次号,只留开始时间。
  const startedElsewhere = {
    ...approvedPlan, status: "started", revision: 3,
    started_by: null, started_at: "2026-11-26T01:05:00", session_id: null,
  } as VisitPlanReceipt;
  const h = reconcileHarness([startedElsewhere]);
  assert.equal(await h.run(approvedPlan), false);
  assert.deepEqual(h.events, [
    ["setPendingStart", null],
    ["setStartError", "该安排已由其他账号开训，请刷新队列。"],
  ]);
  assert.equal(h.startKeys.has("vp-1:2"), false);
});

test("a plan this account started still enters its session", async () => {
  const startedHere = {
    ...approvedPlan, status: "started", revision: 3,
    started_by: "ACTOR-researcher", started_at: "2026-11-26T01:05:00",
    session_id: `s_${"C".repeat(24)}`,
  } as VisitPlanReceipt;
  const h = reconcileHarness([startedHere]);
  assert.equal(await h.run(approvedPlan), true);
  assert.equal(h.events.length, 1);
  assert.equal(h.events[0][0], "enter");
});
