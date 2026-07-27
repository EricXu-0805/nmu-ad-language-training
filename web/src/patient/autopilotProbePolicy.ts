import { ApiError } from "../apiResponse.ts";

export type AutopilotProbeDisposition = "legacy-inactive" | "legacy-disabled" | "blocked";

export const MAX_AUTOPILOT_PROBE_RETRIES = 5;

export type AutopilotProbeFailurePlan =
  | { action: "stop-legacy"; disposition: Exclude<AutopilotProbeDisposition, "blocked"> }
  | { action: "retry"; delayMs: number }
  | { action: "stop-blocked"; retryExhausted: boolean };

export function exactAutopilotApiCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.detailEnvelope !== "nested-detail"
      || error.detailData === null || typeof error.detailData !== "object"
      || Array.isArray(error.detailData)) return null;
  const row = error.detailData as Record<string, unknown>;
  const keys = Object.keys(row).sort();
  if (keys.length !== 2 || keys[0] !== "code" || keys[1] !== "message") return null;
  return typeof row.code === "string" && typeof row.message === "string" ? row.code : null;
}

/** Only these two exact 409s prove that mounting the legacy runner is safe. */
export function classifyAutopilotProbeError(error: unknown): AutopilotProbeDisposition {
  if (!(error instanceof ApiError) || error.status !== 409) return "blocked";
  const code = exactAutopilotApiCode(error);
  if (code === "autopilot_not_active") return "legacy-inactive";
  if (code === "autopilot_p0a_disabled") return "legacy-disabled";
  return "blocked";
}

/** Network uncertainty stays fail-closed but should recover without a reload. */
export function isRetryableAutopilotProbeError(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status === 0 || error.status === 408 || error.status === 429
      || error.status >= 500;
  }
  return error instanceof TypeError;
}

/**
 * Turn one failed `/autopilot/next` read into a bounded lifecycle decision.
 *
 * `autopilot_not_active` and an explicitly disabled P0a gate are stable facts for
 * the current capability/session epoch.  They admit the manual runner once, but
 * must never create a 1.5 second 409 request storm: polling stops until an
 * explicit serverOwned wake (sync/autopilotWake.ts — console 权威回执证明服务器
 * 已持有本场次后发的一次性唤醒) or a capability/session epoch change.  Network
 * uncertainty remains fail-closed and retries with a finite exponential budget;
 * re-pairing (a new capability epoch) is the explicit way to obtain a fresh
 * budget.
 */
export function planAutopilotProbeFailure(
  error: unknown,
  retryAttempt: number,
): AutopilotProbeFailurePlan {
  const disposition = classifyAutopilotProbeError(error);
  if (disposition !== "blocked") {
    return { action: "stop-legacy", disposition };
  }
  if (!isRetryableAutopilotProbeError(error)) {
    return { action: "stop-blocked", retryExhausted: false };
  }
  if (!Number.isSafeInteger(retryAttempt) || retryAttempt < 0
      || retryAttempt >= MAX_AUTOPILOT_PROBE_RETRIES) {
    return { action: "stop-blocked", retryExhausted: true };
  }
  return {
    action: "retry",
    delayMs: Math.min(1_500 * (2 ** Math.min(retryAttempt, 4)), 15_000),
  };
}
