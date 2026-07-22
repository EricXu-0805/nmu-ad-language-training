import type { SessionRuntimeStatus } from "./types";

export const SESSION_TERMINAL_STATUSES: ReadonlySet<SessionRuntimeStatus> = new Set([
  "intervention_completed",
  "completed",
  "aborted",
  "failed",
]);

export function isSessionTerminalStatus(
  status: SessionRuntimeStatus | null | undefined,
): status is "intervention_completed" | "completed" | "aborted" | "failed" {
  return status != null && SESSION_TERMINAL_STATUSES.has(status);
}

export type SessionEntryAction = "resume" | "review" | "view" | null;

export interface SessionStatusMeta {
  label: string;
  tone: "ok" | "primary" | "warn" | "danger" | "muted";
  action: SessionEntryAction;
  actionLabel?: string;
}

export function sessionStatusMeta(status: SessionRuntimeStatus | null | undefined): SessionStatusMeta {
  switch (status ?? "active") {
    case "active":
      return { label: "进行中", tone: "primary", action: "resume", actionLabel: "继续这场" };
    case "paused":
      return { label: "已暂停", tone: "warn", action: "resume", actionLabel: "继续这场" };
    case "intervention_completed":
      return {
        label: "干预已结束 · 待研究复核",
        tone: "warn",
        action: "review",
        actionLabel: "进入研究复核",
      };
    case "completed":
      return { label: "已完成 · 查看/导出", tone: "ok", action: "view", actionLabel: "查看/导出" };
    case "aborted":
      return { label: "已中止 · 只读", tone: "muted", action: null };
    case "failed":
      return { label: "异常结束 · 只读", tone: "danger", action: null };
    default:
      return { label: "未知状态 · 只读", tone: "danger", action: null };
  }
}

export type WrapupMode = "bedside" | "review" | "completed" | "closed";

export function wrapupMode(status: SessionRuntimeStatus): WrapupMode {
  if (status === "active" || status === "paused") return "bedside";
  if (status === "intervention_completed") return "review";
  if (status === "completed") return "completed";
  return "closed";
}

/** A training page observing server auto-finish must leave the bedside console. */
export function shouldAutoRouteTrainingToWrapup(
  status: SessionRuntimeStatus | null | undefined,
): boolean {
  return isSessionTerminalStatus(status);
}

export function canExportCompletedSession(input: {
  status: SessionRuntimeStatus;
  strictCompletionGatePassed: boolean;
  roleAllowsExport: boolean;
}): boolean {
  return input.status === "completed"
    && input.strictCompletionGatePassed
    && input.roleAllowsExport;
}
