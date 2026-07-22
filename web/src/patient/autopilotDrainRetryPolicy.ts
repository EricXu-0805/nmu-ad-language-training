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
