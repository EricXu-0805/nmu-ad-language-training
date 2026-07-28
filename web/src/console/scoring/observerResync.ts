// 观察台"重同步"是一次性恢复事务：所有权释放后，先证明服务器确实已经放手，
// 再取当前精确场次的最新 runtime + journal，全部核验通过才恢复人工位置。
// 这里只做编排,读取动作全部注入,方便用 deferred Promise 在测试里精确控制乱序。
// 它不是第二状态机——没有内部状态,每次调用都是独立的一次性事务。
import type { ServerSessionJournal } from "../../hooks/useSessionJournal.ts";
import type { SessionPlan, SessionRuntimeState } from "../../types.ts";
import {
  exactPlanCursor,
  type ExactPlanCursor,
  type ObserverOwnership,
  ownershipImpliesAutomationExposure,
} from "./observerConsoleModel.ts";

export interface ResyncStatusSnapshot {
  serverOwned: boolean;
  stateRevision: number;
}

export interface ResyncGuard {
  /**
   * 精确 session、精确 epoch、且当前所有权仍明确 owned=false，三者缺一即为假。
   * 调用方必须用同步更新的 ref 实现（不能只依赖 React state/effect），因为所有权
   * 回调可能在这次事务的任一异步边界之间发生，且不等下一次渲染。
   */
  isLive(): boolean;
}

export interface ResyncDeps {
  fetchStatus(sessionId: string): Promise<ResyncStatusSnapshot>;
  fetchRuntime(sessionId: string): Promise<SessionRuntimeState | null>;
  fetchJournal(sessionId: string): Promise<ServerSessionJournal>;
  getPlan(): SessionPlan | null;
  /**
   * 本页此前（通过既有 runtime 轮询）已经见过的最高 revision。哪怕 session 与
   * fence 都对得上，一次读到比这个下限还旧的 runtime 仍是过期数据——例如读到
   * 了慢副本，或恰好与一次尚未落地的写交错——不得据此恢复题位。读取时机是"核验
   * 完 runtime 之后"，允许它在事务进行期间继续被后台轮询推高，核得更严。
   */
  getRuntimeRevisionFloor(): number;
}

export type ResyncOutcome =
  | { kind: "applied"; cursor: ExactPlanCursor; journal: ServerSessionJournal }
  | { kind: "stale" }
  | { kind: "failed"; error: unknown };

/**
 * 恢复真值只能来自本次事务里发起的、事后核验过的读取——不能用调用方可能复用的
 * 旧 in-flight 请求。runtime/journal 只在第一次 no-owner 核验通过之后才发起；
 * apply 前再核验一次 no-owner 且状态版本不变，挡住 owner acquire→release 的 ABA。
 * 任何一步失败或过期都不返回可 apply 的结果，调用方据此保持锁定、可安全重试。
 */
export async function runManualResyncTransaction(
  sessionId: string,
  guard: ResyncGuard,
  deps: ResyncDeps,
): Promise<ResyncOutcome> {
  if (!guard.isLive()) return { kind: "stale" };
  try {
    const statusBefore = await deps.fetchStatus(sessionId);
    if (!guard.isLive()) return { kind: "stale" };
    if (statusBefore.serverOwned) {
      return { kind: "failed", error: new Error("服务器已重新持有控制权，取消本次恢复") };
    }

    const [runtime, journal] = await Promise.all([
      deps.fetchRuntime(sessionId),
      deps.fetchJournal(sessionId),
    ]);
    if (!guard.isLive()) return { kind: "stale" };
    if (!runtime || runtime.sessionId !== sessionId) {
      return { kind: "failed", error: new Error("服务器未返回当前场次的最新运行时状态") };
    }
    if (runtime.revision < deps.getRuntimeRevisionFloor()) {
      return { kind: "failed", error: new Error("服务器返回的运行时版本落后于本页已知版本，拒绝据此恢复") };
    }
    if (journal.session.session_id !== sessionId) {
      return { kind: "failed", error: new Error("服务器返回了其他场次的记录，已拒绝恢复") };
    }

    const statusAfter = await deps.fetchStatus(sessionId);
    if (!guard.isLive()) return { kind: "stale" };
    if (statusAfter.serverOwned) {
      return { kind: "failed", error: new Error("服务器已重新持有控制权，取消本次恢复") };
    }
    if (statusAfter.stateRevision !== statusBefore.stateRevision) {
      return { kind: "failed", error: new Error("服务器控制状态在核验期间发生变化，已取消本次恢复") };
    }

    const plan = deps.getPlan();
    if (!plan) {
      return { kind: "failed", error: new Error("冻结计划尚未载入，无法证明精确恢复位置") };
    }
    const cursor = exactPlanCursor(runtime, sessionId, plan);
    if (!cursor) {
      return { kind: "failed", error: new Error("服务器游标缺失、非法或不在冻结计划内，无法证明精确恢复位置") };
    }

    if (!guard.isLive()) return { kind: "stale" };
    return { kind: "applied", cursor, journal };
  } catch (error) {
    if (!guard.isLive()) return { kind: "stale" };
    return { kind: "failed", error };
  }
}

export interface OwnershipTransitionEffect {
  /** 一旦真过就保持真，直到调用方按场次切换显式重置——不因单次渲染看到的快照而丢失。 */
  automationExposure: boolean;
  /** true 时调用方必须同步作废当前 resync fence 并把 resync 状态打回 idle。 */
  invalidateResync: boolean;
}

/**
 * 所有权回调的纯判定：每次服务器上报都会调用一次,不等 React 提交渲染。
 * exposure 按调用逐次累积（哪怕两次回调被同一次 render 批处理),invalidateResync
 * 只在"重新证明持有 && 当前有非 idle 的重同步"时为真，用来同步失效旧 fence。
 */
export function deriveOwnershipTransition(
  previousAutomationExposure: boolean,
  next: ObserverOwnership,
  resyncIsIdle: boolean,
): OwnershipTransitionEffect {
  return {
    automationExposure: previousAutomationExposure || ownershipImpliesAutomationExposure(next),
    invalidateResync: next.owned && !resyncIsIdle,
  };
}
