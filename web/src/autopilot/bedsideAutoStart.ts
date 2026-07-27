import type { AutopilotConsoleState } from "./startControl";

// 床旁激活 → 自动启动的操作端纯策略。信号只是本机触发条件:它不新建启动链路,
// 只在全部现有门禁明确放行时,替研究者按一次既有的「启动」——随后仍是
// prepareServerOwnership → POST /autopilot/start → 服务端全量 fail-closed 重验。
// 写请求每个 exact-session 激活周期最多一次;被拒/不确定后绝不自动重试。

/**
 * Latch the bedside activation signal for the exact current session. Stale,
 * empty, or non-string sessionIds and cross-session events leave the latch
 * unchanged; duplicates of an already-latched signal are idempotent.
 */
export function latchBedsideActivation(
  current: string | null,
  eventSessionId: unknown,
  currentSessionId: string,
): string | null {
  if (typeof eventSessionId !== "string" || eventSessionId === "") return current;
  if (eventSessionId !== currentSessionId) return current;
  return currentSessionId;
}

export interface BedsideAutoStartGates {
  sessionId: string;
  latchedActivationSessionId: string | null;
  /** p0aConsoleEligibility(...).allowed — 双重模拟范围/具名账号/runtime 可启动。 */
  eligibilityAllowed: boolean;
  /** 完整冻结计划内容门禁(60 个交付缺口任一未补即 true)。 */
  completePlanBlocked: boolean;
  /** provider readiness 消费既有有效探针结果;自动流程绝不代跑探针。 */
  providerStartAllowed: boolean;
  phase: AutopilotConsoleState["phase"];
  /** 服务器权威回执在手且明确证明当前没有 owner。 */
  receiptProvesNoOwner: boolean;
  interactionBlocked: boolean;
  patientMicOn: boolean;
  planPositionReady: boolean;
  startInFlight: boolean;
  alreadyAttempted: boolean;
}

/**
 * Only the proven-idle phase may auto-start: checking/starting/uncertain and
 * every server-owned status stay locked, and a pre-write rejection ("rejected")
 * is surfaced to the researcher instead of being auto-retried.
 */
export function canAutoStartServerAutopilot(gates: BedsideAutoStartGates): boolean {
  return gates.latchedActivationSessionId === gates.sessionId
    && gates.eligibilityAllowed
    && !gates.completePlanBlocked
    && gates.providerStartAllowed
    && gates.phase === "idle"
    && gates.receiptProvesNoOwner
    && !gates.interactionBlocked
    && !gates.patientMicOn
    && gates.planPositionReady
    && !gates.startInFlight
    && !gates.alreadyAttempted;
}
