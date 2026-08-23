import { ApiError } from "../apiResponse.ts";
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
  takeoverReady: boolean;
  commandKind: "tts" | "record" | null;
  /** 只读展示投影:自动带练当前/最后触及的计划位置。不参与任何控制判定。 */
  positionItemId: string | null;
  positionTurnSeq: number | null;
  lastErrorCode: string | null;
}

export type P0aConsoleEligibility =
  | { allowed: true }
  | {
    allowed: false;
    reason:
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
  /**
   * 最近一次启动被拒的拒因,常驻到用户再次点启动或服务器真的持有为止——
   * 权威 no-owner 轮询只把强横幅降级为持久提示,不许无痕抹掉(D1)。
   */
  lastStartRejection: string | null;
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
        "takeover_ready", "current_command_kind", "position_item_id",
        "position_turn_seq", "last_error_code",
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
      || typeof value.takeover_ready !== "boolean"
      || (value.current_command_kind !== null
        && value.current_command_kind !== "tts"
        && value.current_command_kind !== "record")
      || (value.last_error_code !== null
        && (typeof value.last_error_code !== "string"
          || !SAFE_ERROR_CODE.test(value.last_error_code)))) {
    throw new Error("自动驾驶状态响应不符合最小收据契约");
  }
  // 位置字段成对出现:半个位置无法映射进冻结计划,按契约违规拒收。
  const positionAbsent = value.position_item_id === null
    && value.position_turn_seq === null;
  const positionValid = typeof value.position_item_id === "string"
    && value.position_item_id.length >= 1
    && value.position_item_id.length <= 128
    && typeof value.position_turn_seq === "number"
    && Number.isSafeInteger(value.position_turn_seq)
    && value.position_turn_seq >= 1;
  if (!positionAbsent && !positionValid) {
    throw new Error("自动驾驶状态位置投影不符合收据契约");
  }

  const status = value.status as AutopilotServerStatus;
  const disabled = value.scope_key === "disabled";
  if (disabled) {
    if (value.mode !== "disabled" || status !== "idle" || value.state_revision !== 0
        || value.server_owned || value.takeover_ready || value.current_command_kind !== null
        || value.position_item_id !== null || value.position_turn_seq !== null
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
        ].includes(status) || value.current_command_kind !== null || value.takeover_ready)) {
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
    if (value.takeover_ready
        && (value.mode !== "autonomous"
          || !["paused", "scope_completed", "failed"].includes(status)
          || value.current_command_kind !== null)) {
      throw new Error("自动驾驶接管就绪状态与安全收口契约矛盾");
    }
  }

  return {
    scopeKey: value.scope_key,
    mode: value.mode,
    status,
    stateRevision: value.state_revision,
    serverOwned: value.server_owned,
    takeoverReady: value.takeover_ready,
    commandKind: value.current_command_kind,
    positionItemId: (value.position_item_id ?? null) as string | null,
    positionTurnSeq: (value.position_turn_seq ?? null) as number | null,
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
    && left.takeoverReady === right.takeoverReady
    && left.commandKind === right.commandKind
    && left.positionItemId === right.positionItemId
    && left.positionTurnSeq === right.positionTurnSeq
    && left.lastErrorCode === right.lastErrorCode;
}

export function receiptAllowsAutopilotTakeover(
  receipt: AutopilotStatusReceipt | null,
): boolean {
  return receipt?.serverOwned === true && receipt.takeoverReady === true;
}

/**
 * 服务端 _require_gate 在写入任何控制事实之前就拒绝的确定性门禁码(D1)。
 * 这些 409 与「可能已有 owner/幂等冲突」不同:可以安全按写前拒绝呈现拒因,
 * 不必折成会被下一次权威轮询无痕抹掉的 uncertain。幂等/revision/CAS 冲突
 * 一律不在此列,继续 fail-closed 等权威核实。
 */
const PREWRITE_START_REJECTION_CODES = new Set([
  "autopilot_p0a_disabled",
  "autopilot_real_sessions_disabled",
  "autopilot_cloud_processing_required",
  "autopilot_recording_not_allowed",
  "autopilot_consent_denied",
  "autopilot_subject_withdrawn",
  "autopilot_scope_unsupported",
  "autopilot_classification_invalid",
  "autopilot_plan_not_fully_supported",
  "autopilot_runtime_inactive",
]);

export function isPrewriteStartRejection(error: unknown): boolean {
  if (!(error instanceof ApiError)
      || error.status !== 409
      || error.detailEnvelope !== "nested-detail"
      || error.detailData === null
      || typeof error.detailData !== "object"
      || Array.isArray(error.detailData)) return false;
  const code = (error.detailData as { code?: unknown }).code;
  return typeof code === "string" && PREWRITE_START_REJECTION_CODES.has(code);
}

export const parseAutopilotStartReceipt = parseAutopilotStatusReceipt;
export type AutopilotStartReceipt = AutopilotStatusReceipt;

export function p0aConsoleEligibility(
  session: Session,
  runtimeBlocked = false,
  hasNamedAccount = true,
): P0aConsoleEligibility {
  // 只有两种可证明的分类组合可继续：严格模拟对与严格真实研究对。
  // 任何其他组合（legacy_unknown、字段缺失、错配）都按不可证明拒绝——
  // classification_unverified 的场次永远不会渲染启动入口，也永远不会自动开跑。
  const provenSimulation = session.is_simulation === true
    && session.data_classification === "simulation";
  const provenResearch = session.is_simulation === false
    && session.data_classification === "research";
  if (!provenSimulation && !provenResearch) {
    return { allowed: false, reason: "classification_unverified" };
  }
  if (!Number.isInteger(session.week_no)
      || session.week_no < 2 || session.week_no > 8
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
  return { sessionId, phase: "checking", receipt: null, error: null, lastStartRejection: null };
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
      // 服务器真的持有 = 拒因条件已消失;仍无 owner 时持久保留拒因。
      lastStartRejection: action.receipt.serverOwned ? null : state.lastStartRejection,
    };
  }
  if (action.type === "status_uncertain") {
    return { ...state, phase: "uncertain", error: action.error };
  }
  if (action.type === "start_requested") {
    if (state.phase !== "idle" && state.phase !== "rejected") return state;
    return { ...state, phase: "starting", error: null, lastStartRejection: null };
  }
  return {
    ...state,
    phase: "rejected",
    receipt: null,
    error: action.error,
    lastStartRejection: action.error,
  };
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
