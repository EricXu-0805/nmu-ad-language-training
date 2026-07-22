import type { AuthConfig, AuthIdentity } from "../types";
import { clearResearchBrowserState } from "../security/localSensitiveState.ts";

export type ConsoleAuthMode = "loading" | "login" | "ok" | "error";

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function parseAuthConfig(value: unknown): AuthConfig {
  const candidate = record(value);
  if (!candidate
    || typeof candidate.auth_required !== "boolean"
    || typeof candidate.accounts_enabled !== "boolean"
    || typeof candidate.pin_enabled !== "boolean") {
    throw new Error("认证配置响应缺少有效的布尔字段");
  }
  return {
    auth_required: candidate.auth_required,
    accounts_enabled: candidate.accounts_enabled,
    pin_enabled: candidate.pin_enabled,
  };
}

export function parseAuthIdentity(value: unknown): AuthIdentity {
  const candidate = record(value);
  if (!candidate
    || typeof candidate.display_id !== "string" || !candidate.display_id.trim()
    || typeof candidate.role !== "string" || !candidate.role.trim()
    || typeof candidate.username !== "string" || !candidate.username.trim()) {
    throw new Error("认证身份响应不完整");
  }
  return {
    display_id: candidate.display_id,
    role: candidate.role,
    username: candidate.username,
  };
}

export function isUnauthenticatedResponse(error: unknown): boolean {
  return error !== null && typeof error === "object" && (error as { status?: unknown }).status === 401;
}

export function handleConfirmedAccountAuthenticationLoss(
  transition: () => void,
  clear: () => number = clearResearchBrowserState,
): number {
  // A server-confirmed 401 is a privacy boundary, not merely a navigation
  // event.  Clear Web Storage before the login screen can be shown on a
  // shared bedside browser.  The cleanup intentionally preserves IndexedDB
  // audio that still lacks a server upload/disposition receipt.
  const cleared = clear();
  transition();
  return cleared;
}

export function shouldMountConsoleWorkspace(mode: ConsoleAuthMode): boolean {
  return mode === "ok";
}

export function identityCanOperateTraining(identity: AuthIdentity | null): boolean {
  return identity === null || identity.role === "researcher" || identity.role === "admin";
}

export function identityCanExportResearchData(identity: AuthIdentity | null): boolean {
  return identity?.role === "data_steward" || identity?.role === "admin";
}

export function identityCanPhysicallyDeleteAudio(identity: AuthIdentity | null): boolean {
  return identity?.role === "admin";
}
