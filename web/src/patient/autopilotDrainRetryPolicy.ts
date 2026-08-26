import { ApiError } from "../apiResponse.ts";
import { exactAutopilotApiCode } from "./autopilotProbePolicy.ts";

export type AutopilotDrainFailureDisposition =
  | "released"
  | "retry"
  | "repair-credential"
  | "blocked";

const RETRYABLE_CONFLICTS = new Set([
  "autopilot_drain_target_unavailable",
  "autopilot_drain_not_current",
  "autopilot_drain_cas_conflict",
  "autopilot_drain_pause_mismatch",
  "autopilot_command_not_current",
  "autopilot_revision_conflict",
]);

/** Retry only uncertainty/transient races; permanent contracts never create a request storm. */
export function classifyAutopilotDrainFailure(
  error: unknown,
): AutopilotDrainFailureDisposition {
  const code = exactAutopilotApiCode(error);
  if (code === "autopilot_not_active" || code === "autopilot_p0a_disabled") {
    return "released";
  }
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403) return "repair-credential";
    if (error.status === 0 || error.status === 408 || error.status === 429
        || error.status >= 500) return "retry";
    if (error.status === 409 && code !== null && RETRYABLE_CONFLICTS.has(code)) {
      return "retry";
    }
    return "blocked";
  }
  // Browser fetch rejects network failures with TypeError. Parser/contract
  // failures are ordinary Error values and must remain hard-blocked.
  return error instanceof TypeError ? "retry" : "blocked";
}

export function drainRetryDelayMs(attempt: number): number {
  if (!Number.isSafeInteger(attempt) || attempt < 0) return 15_000;
  return Math.min(1_500 * (2 ** Math.min(attempt, 4)), 15_000);
}

/**
 * outbox 里滞留的旧回执是否已被服务器按代际永久围栏。
 *
 * 暂停→继续/转人工→切回会轮换 control/runner generation,暂停瞬间在途被拒的
 * 回执从此重放必得 409 autopilot_command_not_current——服务器从未记录过它,也
 * 永远不会接受它。不丢弃就是死锁:老人端每次进场(含整页刷新,outbox 持久在
 * IndexedDB)都会先重放这条死回执、被拒、闩死并停止拉取新命令,而控制台全绿。
 * 服务器只要求回执序号严格递增(不要求连续),丢弃跳号是安全的。
 * 只认这一个精确拒因;其余(含 runtime_inactive 的暂停窗口)一律保守保留。
 */
export function pendingAckPermanentlyFenced(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
    && exactAutopilotApiCode(error) === "autopilot_command_not_current";
}
