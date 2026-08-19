// 服务端托管观察台 view-model：锁定判定、暴露判定与重同步 fence 都是纯函数，
// 唯一事实源仍是 ServerAutopilotControl 上报的 (owned, phase) 与服务端 runtime/journal。
import type { AutopilotConsoleState } from "../../autopilot/startControl";
import type { SessionPlan, SessionRuntimeState } from "../../types";

export type ObserverOwnershipPhase = AutopilotConsoleState["phase"];

export interface ObserverOwnership {
  owned: boolean;
  phase: ObserverOwnershipPhase;
}

export type ManualResyncStatus = "idle" | "pending" | "failed";

/**
 * 服务器可能推进过本场：权威回执证明拥有、start 写已发出、或写后状态不明。
 * 初次挂载的只读 checking 询问本身不构成自动化暴露——它得到明确 no-owner 时
 * 走既有恢复门，不追加重同步请求。
 */
export function ownershipImpliesAutomationExposure(ownership: ObserverOwnership): boolean {
  return ownership.owned && ownership.phase !== "checking";
}

/** owned/uncertain → 明确 no-owner 的下降沿：只有出现过自动化暴露才需要重同步。 */
export function manualResyncRequired(
  previousOwned: boolean,
  next: ObserverOwnership,
  exposed: boolean,
): boolean {
  return previousOwned && !next.owned && exposed;
}

/**
 * 观察台锁定 = 服务器拥有或尚不能证伪拥有（checking/starting/uncertain 都以
 * owned=true 上报），或重同步未完成；重同步失败继续 fail-closed。
 */
export function manualSurfaceLocked(
  ownership: ObserverOwnership,
  resync: ManualResyncStatus,
): boolean {
  return ownership.owned || resync === "pending" || resync === "failed";
}

export interface ObserverResyncFence {
  sessionId: string;
  epoch: number;
}

/** A 场次的迟到重同步结果不得改写 B 场次；重新上锁也会作废在途结果。 */
export function observerResyncResultCurrent(
  fence: ObserverResyncFence,
  current: ObserverResyncFence,
): boolean {
  return fence.sessionId === current.sessionId && fence.epoch === current.epoch;
}

export interface ObserverPhaseView {
  label: string;
  detail: string;
  tone: "ok" | "warn" | "danger";
  /** true = 权威回执已证实服务器持有；false = 仅为安全而锁定，尚未证实。 */
  proven: boolean;
}

export function observerPhaseView(
  phase: ObserverOwnershipPhase,
  resync: ManualResyncStatus,
): ObserverPhaseView {
  if (resync === "pending") {
    return {
      label: "正在恢复人工控制",
      detail: "正在同步最新进度，完成前暂不能操作。",
      tone: "warn",
      proven: false,
    };
  }
  if (resync === "failed") {
    return {
      label: "恢复人工控制失败",
      detail: "最新进度未能从服务器取回，人工操作继续保持关闭；可重试同步。",
      tone: "danger",
      proven: false,
    };
  }
  switch (phase) {
    case "starting":
      return {
        label: "正在等待服务器启动确认",
        detail: "启动请求已发出但尚未拿到权威回执，不能视为已启动成功；人工操作先行关闭。",
        tone: "warn",
        proven: false,
      };
    case "uncertain":
      return {
        label: "状态待核实 · 人工控制已暂缓",
        detail: "服务器回执缺失或互相矛盾；在权威状态核实前，本页不猜测控制权归属。",
        tone: "danger",
        proven: false,
      };
    case "running":
      return { label: "服务端托管执行中", detail: "服务器已证实持有本场控制权，正在按冻结协议推进。", tone: "ok", proven: true };
    case "waiting_tts":
      return { label: "AI 正在播报", detail: "服务器已签发朗读命令，等待老人端播报完成回执。", tone: "ok", proven: true };
    case "waiting_recording":
      return { label: "正在等待老人回答", detail: "服务器已签发录音命令，等待老人端完成收音回报。", tone: "ok", proven: true };
    case "processing_attempt":
      return { label: "正在转写并判断", detail: "服务器正在处理刚才的回答。", tone: "ok", proven: true };
    case "manual_draining":
      return { label: "正在完成当前收麦并准备人工接管", detail: "服务器正在安全结束当前命令；收麦证明完成前不放行人工操作。", tone: "warn", proven: true };
    case "paused":
      return { label: "服务端已暂停", detail: "服务器保持当前题位与提示等级；恢复人工流程需走显式接管。", tone: "warn", proven: true };
    case "scope_completed":
      return { label: "当前范围已完成", detail: "本次冻结计划内可自动范围已结束，等待服务器确认下一控制模式。", tone: "ok", proven: true };
    case "failed":
      return { label: "服务端执行失败", detail: "服务器保留控制权并停止推进，需研究者按既有流程处置。", tone: "danger", proven: true };
    default:
      // checking，以及防御性兜底（idle/rejected 正常不会渲染观察台）。
      return {
        label: "正在核对服务端控制权",
        detail: "尚未证实服务器是否持有本场；核对完成前人工操作保持关闭。",
        tone: "warn",
        proven: false,
      };
  }
}

export interface ExactPlanCursor {
  itemIdx: number;
  turnIdx: number;
}

/**
 * "精确 cursor 是否有效"的唯一判定，观察台展示与人工恢复共用：只信当前精确
 * session 的服务端 runtime.cursor 与冻结计划映射。计划缺失、runtime/cursor
 * 属于别场、下标非法或不落在计划内的 item/turn 上都返回 null——绝不默认到 0、
 * 绝不 clamp、绝不用本地缓存位置猜。
 */
export function exactPlanCursor(
  runtime: SessionRuntimeState | null,
  sessionId: string,
  plan: SessionPlan | null,
): ExactPlanCursor | null {
  if (!plan || runtime?.sessionId !== sessionId) return null;
  const cursor = runtime.cursor;
  if (!cursor) return null;
  if (cursor.sessionId !== undefined && cursor.sessionId !== sessionId) return null;
  const { itemIdx, turnIdx } = cursor;
  if (!Number.isSafeInteger(itemIdx) || !Number.isSafeInteger(turnIdx)
      || itemIdx < 0 || turnIdx < 0) return null;
  if (!plan.items[itemIdx]?.turns[turnIdx]) return null;
  return { itemIdx, turnIdx };
}

export interface ObserverPlanPosition {
  itemOrdinal: number;
  itemTotal: number;
  turnOrdinal: number;
  turnTotal: number;
  itemLabel: string;
  taskType: string;
  responseRole: string;
}

/** 任何不能通过 exactPlanCursor 的位置显示"待同步"，绝不显示估计进度。 */
export function observerPlanPosition(
  runtime: SessionRuntimeState | null,
  sessionId: string,
  plan: SessionPlan | null,
): ObserverPlanPosition | null {
  const cursor = exactPlanCursor(runtime, sessionId, plan);
  if (!cursor || !plan) return null;
  const item = plan.items[cursor.itemIdx];
  const turn = item.turns[cursor.turnIdx];
  return {
    itemOrdinal: cursor.itemIdx + 1,
    itemTotal: plan.items.length,
    turnOrdinal: cursor.turnIdx + 1,
    turnTotal: item.turns.length,
    itemLabel: item.item_id.replace(/^(SE|DE)_/, ""),
    taskType: item.task_type,
    responseRole: turn.response_role,
  };
}
