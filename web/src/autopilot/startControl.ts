import type { Session } from "../types";

export const P0A_SCOPE_KEY = "p0a_sim_first_single_v1" as const;

export type AutopilotServerStatus =
  | "idle"
  | "running"
  | "waiting_tts"
  | "waiting_recording"
  | "processing_attempt"
  | "manual_draining"
  | "paused"
  | "scope_completed"
  | "failed";
const SAFE_SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/;
const SAFE_ERROR_CODE = /^[a-z][a-z0-9_]{0,95}$/;

export interface AutopilotStartRequest {
  idempotency_key: string;
  expected_revision: 0;
}

export interface AutopilotTakeoverRequest {
  idempotency_key: string;
  expected_revision: number;
}

/** Account-only projection. Patient content and device identifiers are forbidden. */
export interface AutopilotStatusReceipt {
  scopeKey: "disabled" | typeof P0A_SCOPE_KEY;
  mode: "disabled" | "autonomous" | "manual";
  status: AutopilotServerStatus;
  stateRevision: number;
  serverOwned: boolean;
  commandKind: "tts" | "record" | null;
  lastErrorCode: string | null;
}

export type P0aConsoleEligibility =
  | { allowed: true }
  | {
    allowed: false;
    reason:
      | "research_session"
      | "classification_unverified"
      | "scope_unsupported"
      | "account_required"
      | "runtime_blocked";
  };

/** Unknown and explicitly incomplete content both fail closed before ownership. */
export function completePlanAllowsAutopilotStart(
  operationalAutopilotReady: boolean | null,
): boolean {
  return operationalAutopilotReady === true;
}

export interface AutopilotConsoleState {
  sessionId: string;
  phase:
    | "checking"
    | "idle"
    | "starting"
    | AutopilotServerStatus
    | "uncertain"
    | "rejected";
  receipt: AutopilotStatusReceipt | null;
  error: string | null;
}

/**
 * Read responses captured before a control write can never reopen the manual
 * plane while that write is in flight. The epoch is intentionally local: the
 * persisted state revision remains the server's authority.
 */
export class AutopilotControlOperationEpoch {
  #value = 0;

  captureRead(): number { return this.#value; }

  beginWrite(): number {
    this.#value += 1;
    return this.#value;
  }

  invalidate(): void { this.#value += 1; }

  accepts(readEpoch: number): boolean { return readEpoch === this.#value; }
}

export type AutopilotConsoleAction =
  | { type: "reset"; sessionId: string }
  | { type: "status_received"; sessionId: string; receipt: AutopilotStatusReceipt }
  | { type: "status_uncertain"; sessionId: string; error: string }
  | { type: "start_requested"; sessionId: string }
  | { type: "start_rejected"; sessionId: string; error: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length
    && actual.every((key, index) => key === expected[index]);
}

/**
 * The start command is deterministic for one session. A lost HTTP response can
 * therefore be retried without minting a second control fact. The current UI
 * creates short opaque ASCII session ids; legacy/untrusted ids stay fail-closed.
 */
export function buildAutopilotStartRequest(sessionId: string): AutopilotStartRequest {
  if (!SAFE_SESSION_ID.test(sessionId)) {
    throw new Error("当前场次标识不能安全用于自动驾驶启动");
  }
  return {
    idempotency_key: `p0a.start.${sessionId}`,
    expected_revision: 0,
  };
}

/**
 * Releasing server ownership is a separate, revision-fenced control fact. The
 * deterministic key makes a lost takeover response safe to retry without
 * creating a second audit event or guessing that manual control was restored.
 */
export function buildAutopilotTakeoverRequest(
  sessionId: string,
  stateRevision: number,
): AutopilotTakeoverRequest {
  if (!SAFE_SESSION_ID.test(sessionId)) {
    throw new Error("当前场次标识不能安全用于人工接管");
  }
  if (!Number.isSafeInteger(stateRevision) || stateRevision < 1) {
    throw new Error("人工接管缺少可验证的服务器状态版本");
  }
  return {
    idempotency_key: `p0a.takeover.${sessionId}.${stateRevision}`,
    expected_revision: stateRevision,
  };
}

/** Strictly reduce either GET status or POST start to the same content-free DTO. */
export function parseAutopilotStatusReceipt(value: unknown): AutopilotStatusReceipt {
  if (!isRecord(value)
      || !hasExactKeys(value, [
        "scope_key", "mode", "status", "state_revision", "server_owned",
        "current_command_kind", "last_error_code",
      ])
      || (value.scope_key !== "disabled" && value.scope_key !== P0A_SCOPE_KEY)
      || (value.mode !== "disabled" && value.mode !== "autonomous" && value.mode !== "manual")
      || ![
        "idle", "running", "waiting_tts", "waiting_recording",
        "processing_attempt", "manual_draining", "paused", "scope_completed", "failed",
      ].includes(String(value.status))
      || typeof value.state_revision !== "number"
      || !Number.isSafeInteger(value.state_revision)
      || value.state_revision < 0
      || typeof value.server_owned !== "boolean"
      || (value.current_command_kind !== null
        && value.current_command_kind !== "tts"
        && value.current_command_kind !== "record")
      || (value.last_error_code !== null
        && (typeof value.last_error_code !== "string"
          || !SAFE_ERROR_CODE.test(value.last_error_code)))) {
    throw new Error("自动驾驶状态响应不符合最小收据契约");
  }

  const status = value.status as AutopilotServerStatus;
  const disabled = value.scope_key === "disabled";
  if (disabled) {
    if (value.mode !== "disabled" || status !== "idle" || value.state_revision !== 0
        || value.server_owned || value.current_command_kind !== null
        || value.last_error_code !== null) {
      throw new Error("自动驾驶禁用状态内部矛盾");
    }
  } else {
    if (value.state_revision < 1) {
      throw new Error("已启用的自动驾驶状态缺少有效版本");
    }
    if (value.mode === "disabled" || value.server_owned !== (value.mode === "autonomous")) {
      throw new Error("自动驾驶所有权状态内部矛盾");
    }
    if (value.mode === "autonomous" && status === "idle") {
      throw new Error("服务器持有控制权时不得声明空闲状态");
    }
    if (value.mode === "manual"
        && (![
          "paused", "scope_completed", "failed",
        ].includes(status) || value.current_command_kind !== null)) {
      throw new Error("人工接管状态不符合服务器释放契约");
    }
    const expectedKind = status === "waiting_tts"
      ? "tts"
      : status === "waiting_recording" || status === "processing_attempt"
        || status === "manual_draining"
        ? "record"
        : null;
    if (value.current_command_kind !== expectedKind) {
      throw new Error("自动驾驶状态与当前命令不一致");
    }
  }

  return {
    scopeKey: value.scope_key,
    mode: value.mode,
    status,
    stateRevision: value.state_revision,
    serverOwned: value.server_owned,
    commandKind: value.current_command_kind,
    lastErrorCode: value.last_error_code,
  };
}

export function sameAutopilotStatusReceipt(
  left: AutopilotStatusReceipt,
  right: AutopilotStatusReceipt,
): boolean {
  return left.scopeKey === right.scopeKey
    && left.mode === right.mode
    && left.status === right.status
    && left.stateRevision === right.stateRevision
    && left.serverOwned === right.serverOwned
    && left.commandKind === right.commandKind
    && left.lastErrorCode === right.lastErrorCode;
}

export const parseAutopilotStartReceipt = parseAutopilotStatusReceipt;
export type AutopilotStartReceipt = AutopilotStatusReceipt;

export function p0aConsoleEligibility(
  session: Session,
  runtimeBlocked = false,
  hasNamedAccount = true,
): P0aConsoleEligibility {
  if (session.data_classification === "research" || session.is_simulation === false) {
    return { allowed: false, reason: "research_session" };
  }
  if (session.data_classification !== "simulation" || session.is_simulation !== true) {
    return { allowed: false, reason: "classification_unverified" };
  }
  if (session.week_no !== 2
      || session.phase_type !== "正式训练"
      || session.event_line !== "正式训练") {
    return { allowed: false, reason: "scope_unsupported" };
  }
  if (!hasNamedAccount) return { allowed: false, reason: "account_required" };
  if (runtimeBlocked) return { allowed: false, reason: "runtime_blocked" };
  return { allowed: true };
}

export function initialAutopilotConsoleState(sessionId: string): AutopilotConsoleState {
  // Until the account-only GET proves otherwise, an old tab must not become a
  // second driver during the first render after refresh.
  return { sessionId, phase: "checking", receipt: null, error: null };
}

export function autopilotConsoleReducer(
  state: AutopilotConsoleState,
  action: AutopilotConsoleAction,
): AutopilotConsoleState {
  if (action.type === "reset") return initialAutopilotConsoleState(action.sessionId);
  if (action.sessionId !== state.sessionId) return state;
  if (action.type === "status_received") {
    if (state.receipt && action.receipt.stateRevision < state.receipt.stateRevision) return state;
    if (state.receipt
        && action.receipt.stateRevision === state.receipt.stateRevision
        && !sameAutopilotStatusReceipt(state.receipt, action.receipt)) {
      return {
        ...state,
        phase: "uncertain",
        error: "服务器返回了同版本但互相矛盾的控制状态",
      };
    }
    return {
      ...state,
      phase: action.receipt.serverOwned ? action.receipt.status : "idle",
      receipt: action.receipt,
      error: null,
    };
  }
  if (action.type === "status_uncertain") {
    return { ...state, phase: "uncertain", error: action.error };
  }
  if (action.type === "start_requested") {
    if (state.phase !== "idle" && state.phase !== "rejected") return state;
    return { ...state, phase: "starting", error: null };
  }
  return { ...state, phase: "rejected", receipt: null, error: action.error };
}

export function autopilotServerOwnsConsole(state: AutopilotConsoleState): boolean {
  if (state.receipt?.serverOwned) return true;
  if (state.phase === "idle" || state.phase === "rejected") return false;
  if (state.phase === "checking" || state.phase === "starting" || state.phase === "uncertain") {
    return true;
  }
  if (state.receipt) return state.receipt.serverOwned;
  return true;
}
