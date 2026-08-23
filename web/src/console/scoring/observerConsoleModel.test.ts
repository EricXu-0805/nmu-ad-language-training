import assert from "node:assert/strict";
import test from "node:test";
import type { SessionPlan, SessionRuntimeState } from "../../types.ts";
import {
  exactPlanCursor,
  manualResyncRequired,
  manualSurfaceLocked,
  observerPhaseView,
  observerAuthoritativePosition,
  observerPlanPosition,
  observerResyncResultCurrent,
  planCursorForReceiptPosition,
  ownershipImpliesAutomationExposure,
  type ObserverOwnershipPhase,
} from "./observerConsoleModel.ts";

const SERVER_STATUSES: ObserverOwnershipPhase[] = [
  "running", "waiting_tts", "waiting_recording", "processing_attempt",
  "manual_draining", "paused", "scope_completed", "failed",
];

test("every owned or unproven ownership phase keeps the manual surface unmounted", () => {
  for (const phase of ["checking", "starting", "uncertain", ...SERVER_STATUSES] as const) {
    assert.equal(manualSurfaceLocked({ owned: true, phase }, "idle"), true, phase);
  }
  // 明确 no-owner + 无在途重同步：正常人工恢复路径，人工面板可挂载。
  assert.equal(manualSurfaceLocked({ owned: false, phase: "idle" }, "idle"), false);
  assert.equal(manualSurfaceLocked({ owned: false, phase: "rejected" }, "idle"), false);
});

test("resync pending or failed keeps the surface locked even after no-owner is proven", () => {
  assert.equal(manualSurfaceLocked({ owned: false, phase: "idle" }, "pending"), true);
  assert.equal(manualSurfaceLocked({ owned: false, phase: "idle" }, "failed"), true);
});

test("only states where the server may have advanced the session count as exposure", () => {
  // 初次挂载的只读 checking 询问不算暴露：checking→idle 走既有恢复门，不追加请求。
  assert.equal(ownershipImpliesAutomationExposure({ owned: true, phase: "checking" }), false);
  assert.equal(ownershipImpliesAutomationExposure({ owned: true, phase: "starting" }), true);
  assert.equal(ownershipImpliesAutomationExposure({ owned: true, phase: "uncertain" }), true);
  for (const phase of SERVER_STATUSES) {
    assert.equal(ownershipImpliesAutomationExposure({ owned: true, phase }), true, phase);
  }
  assert.equal(ownershipImpliesAutomationExposure({ owned: false, phase: "idle" }), false);
  assert.equal(ownershipImpliesAutomationExposure({ owned: false, phase: "rejected" }), false);
});

test("resync is required exactly on the exposed owned→no-owner falling edge", () => {
  // owned/uncertain（含 takeover 释放后）→ 明确 no-owner：必须先重同步。
  assert.equal(manualResyncRequired(true, { owned: false, phase: "idle" }, true), true);
  // 初次进入 checking→idle：无暴露，不重同步，不制造额外请求。
  assert.equal(manualResyncRequired(true, { owned: false, phase: "idle" }, false), false);
  // 仍然 owned：继续观察，不触发。
  assert.equal(manualResyncRequired(true, { owned: true, phase: "paused" }, true), false);
  // 一直 no-owner：无下降沿。
  assert.equal(manualResyncRequired(false, { owned: false, phase: "idle" }, true), false);
});

test("late resync results from session A or an invalidated epoch never apply", () => {
  const fence = { sessionId: "S-a", epoch: 3 };
  assert.equal(observerResyncResultCurrent(fence, { sessionId: "S-a", epoch: 3 }), true);
  // 换场：A 的迟到响应不得改写 B 的状态。
  assert.equal(observerResyncResultCurrent(fence, { sessionId: "S-b", epoch: 3 }), false);
  assert.equal(observerResyncResultCurrent(fence, { sessionId: "S-b", epoch: 4 }), false);
  // 重新上锁作废在途结果。
  assert.equal(observerResyncResultCurrent(fence, { sessionId: "S-a", epoch: 4 }), false);
});

test("unproven phases are never presented as a successful server start", () => {
  const checking = observerPhaseView("checking", "idle");
  assert.equal(checking.proven, false);
  assert.match(checking.label, /正在核对服务端控制权/);
  const starting = observerPhaseView("starting", "idle");
  assert.equal(starting.proven, false);
  assert.match(starting.label, /等待服务器启动确认/);
  assert.match(starting.detail, /不能视为已启动成功/);
  const uncertain = observerPhaseView("uncertain", "idle");
  assert.equal(uncertain.proven, false);
  assert.equal(uncertain.tone, "danger");
  assert.match(uncertain.label, /状态待核实/);
  for (const view of [checking, starting, uncertain]) {
    assert.doesNotMatch(view.label, /托管执行中|已证实/);
  }
});

test("receipt-backed server statuses present truthful proven labels", () => {
  const expectations: [ObserverOwnershipPhase, RegExp][] = [
    ["running", /服务端托管执行中/],
    ["waiting_tts", /AI 正在播报/],
    ["waiting_recording", /正在等待老人回答/],
    ["processing_attempt", /正在转写并判断/],
    ["manual_draining", /完成当前收麦并准备人工接管/],
    ["paused", /服务端已暂停/],
    ["scope_completed", /当前范围已完成/],
    ["failed", /服务端执行失败/],
  ];
  for (const [phase, pattern] of expectations) {
    const view = observerPhaseView(phase, "idle");
    assert.equal(view.proven, true, phase);
    assert.match(view.label, pattern);
  }
});

test("resync progress overrides the phase copy and stays honest about the lock", () => {
  const pending = observerPhaseView("idle", "pending");
  assert.match(pending.label, /正在恢复人工控制/);
  assert.equal(pending.proven, false);
  const failed = observerPhaseView("idle", "failed");
  assert.match(failed.label, /恢复人工控制失败/);
  assert.equal(failed.tone, "danger");
  assert.match(failed.detail, /保持关闭/);
});

const PLAN: SessionPlan = {
  item_bank_version_id: "bank-v1",
  week_no: 2,
  event_line: "正式训练",
  total_items: 2,
  total_turns: 3,
  items: [
    {
      item_id: "SE_胡萝卜", task_type: "单要素", image_id: null, presentation_order: 1,
      display: {}, turns: [{ turn_seq: 1, response_role: "命名", scoring_key: null }],
    },
    {
      item_id: "DE_牙刷_牙膏", task_type: "双要素", image_id: null, presentation_order: 2,
      display: {}, turns: [
        { turn_seq: 1, response_role: "左侧命名", scoring_key: null },
        { turn_seq: 2, response_role: "关系识别", scoring_key: null },
      ],
    },
  ],
};

function runtimeAt(itemIdx: number, turnIdx: number, sessionId = "S-a"): SessionRuntimeState {
  return {
    sessionId,
    status: "active",
    revision: 5,
    cursor: { sessionId, itemIdx, turnIdx },
    rapportStep: null,
  };
}

// 人工恢复与观察台展示共用的精确判定：每一种"不能证明精确位置"都必须返回
// null（上层据此 fail-closed），绝不默认到 0、绝不 clamp。
test("exactPlanCursor accepts only a proven in-plan cursor for the exact session", () => {
  assert.deepEqual(exactPlanCursor(runtimeAt(1, 1), "S-a", PLAN), { itemIdx: 1, turnIdx: 1 });
  assert.deepEqual(exactPlanCursor(runtimeAt(0, 0), "S-a", PLAN), { itemIdx: 0, turnIdx: 0 });
  // 计划未载入。
  assert.equal(exactPlanCursor(runtimeAt(0, 0), "S-a", null), null);
  // runtime 缺失或属于别场。
  assert.equal(exactPlanCursor(null, "S-a", PLAN), null);
  assert.equal(exactPlanCursor(runtimeAt(0, 0, "S-b"), "S-a", PLAN), null);
  // cursor 缺失。
  assert.equal(exactPlanCursor({ ...runtimeAt(0, 0), cursor: null }, "S-a", PLAN), null);
  // cursor 自带的 session 字段与本场不符（外场 cursor）。
  const foreignCursor = runtimeAt(0, 0);
  foreignCursor.cursor = { sessionId: "S-b", itemIdx: 0, turnIdx: 0 };
  assert.equal(exactPlanCursor(foreignCursor, "S-a", PLAN), null);
  // 负数下标。
  assert.equal(exactPlanCursor(runtimeAt(-1, 0), "S-a", PLAN), null);
  assert.equal(exactPlanCursor(runtimeAt(0, -1), "S-a", PLAN), null);
  // 非整数（含 NaN/Infinity）。
  assert.equal(exactPlanCursor(runtimeAt(0.5, 0), "S-a", PLAN), null);
  assert.equal(exactPlanCursor(runtimeAt(0, 1.5), "S-a", PLAN), null);
  assert.equal(exactPlanCursor(runtimeAt(Number.NaN, 0), "S-a", PLAN), null);
  assert.equal(exactPlanCursor(runtimeAt(Number.POSITIVE_INFINITY, 0), "S-a", PLAN), null);
  // item 越界。
  assert.equal(exactPlanCursor(runtimeAt(2, 0), "S-a", PLAN), null);
  // turn 越界（第 1 题只有 1 个环节）。
  assert.equal(exactPlanCursor(runtimeAt(0, 1), "S-a", PLAN), null);
});

test("observer position comes only from the exact session runtime cursor and frozen plan", () => {
  assert.deepEqual(observerPlanPosition(runtimeAt(1, 1), "S-a", PLAN), {
    itemOrdinal: 2,
    itemTotal: 2,
    turnOrdinal: 2,
    turnTotal: 2,
    itemLabel: "牙刷_牙膏",
    taskType: "双要素",
    responseRole: "关系识别",
  });
  assert.equal(observerPlanPosition(runtimeAt(0, 0), "S-a", PLAN)?.itemLabel, "胡萝卜");
});

test("stale, foreign, or out-of-range positions fail closed to null", () => {
  // 别场次 runtime 不得冒充本场进度。
  assert.equal(observerPlanPosition(runtimeAt(0, 0, "S-b"), "S-a", PLAN), null);
  const foreignCursor = runtimeAt(0, 0);
  foreignCursor.cursor = { sessionId: "S-b", itemIdx: 0, turnIdx: 0 };
  assert.equal(observerPlanPosition(foreignCursor, "S-a", PLAN), null);
  // runtime 未到 / cursor 缺失 / 计划未载入：显示待同步而非估计。
  assert.equal(observerPlanPosition(null, "S-a", PLAN), null);
  assert.equal(observerPlanPosition({ ...runtimeAt(0, 0), cursor: null }, "S-a", PLAN), null);
  assert.equal(observerPlanPosition(runtimeAt(0, 0), "S-a", null), null);
  // 越界或非法游标不得被截断成看似合法的进度。
  assert.equal(observerPlanPosition(runtimeAt(2, 0), "S-a", PLAN), null);
  assert.equal(observerPlanPosition(runtimeAt(0, 1), "S-a", PLAN), null);
  assert.equal(observerPlanPosition(runtimeAt(-1, 0), "S-a", PLAN), null);
  assert.equal(observerPlanPosition(runtimeAt(0.5, 0), "S-a", PLAN), null);
});

// D3/D4:自动带练不写 live cursor,serverOwnership 期间权威位置只来自状态回执。
test("D3:receipt position maps into the frozen plan, and anything unprovable is null", () => {
  assert.deepEqual(planCursorForReceiptPosition(PLAN, "DE_牙刷_牙膏", 2), { itemIdx: 1, turnIdx: 1 });
  assert.deepEqual(planCursorForReceiptPosition(PLAN, "SE_胡萝卜", 1), { itemIdx: 0, turnIdx: 0 });
  assert.equal(planCursorForReceiptPosition(PLAN, "SE_不在计划里", 1), null);
  assert.equal(planCursorForReceiptPosition(PLAN, "SE_胡萝卜", 9), null);
  assert.equal(planCursorForReceiptPosition(PLAN, "SE_胡萝卜", 0), null);
  assert.equal(planCursorForReceiptPosition(PLAN, "SE_胡萝卜", 1.5), null);
  assert.equal(planCursorForReceiptPosition(null, "SE_胡萝卜", 1), null);
  assert.equal(planCursorForReceiptPosition(PLAN, null, null), null);
  assert.equal(planCursorForReceiptPosition(PLAN, "", 1), null);
});

test("D3:receipt position wins over the stale runtime cursor, and its change moves the view", () => {
  const staleRuntime = runtimeAt(0, 0);
  const fromReceipt = observerAuthoritativePosition(
    { itemId: "DE_牙刷_牙膏", turnSeq: 2 }, staleRuntime, "S-a", PLAN);
  assert.equal(fromReceipt?.itemOrdinal, 2);
  assert.equal(fromReceipt?.turnOrdinal, 2);
  assert.equal(fromReceipt?.itemLabel, "牙刷_牙膏");
  // 回执位置变化 → 观察位置变化(9 分钟 26 采样全 pos=1 的病根)。
  const moved = observerAuthoritativePosition(
    { itemId: "SE_胡萝卜", turnSeq: 1 }, staleRuntime, "S-a", PLAN);
  assert.equal(moved?.itemOrdinal, 1);
  // 无回执位置(如尚未拿到权威回执)→ 回退既有 runtime 判定。
  assert.equal(
    observerAuthoritativePosition(null, staleRuntime, "S-a", PLAN)?.itemOrdinal, 1);
  assert.equal(observerAuthoritativePosition(null, null, "S-a", PLAN), null);
  // 回执位置不可证明 → null(显示待同步),绝不回退到可能更旧的 cursor 冒充权威。
  assert.equal(
    observerAuthoritativePosition({ itemId: "SE_不在计划里", turnSeq: 1 }, staleRuntime, "S-a", PLAN),
    null);
});
