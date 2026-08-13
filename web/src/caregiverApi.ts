import { ApiError, apiNetworkError, decodeJsonApiResponse } from "./apiResponse.ts";
import { parseAutopilotStatusReceipt } from "./autopilot/startControl.ts";
import type {
  CaregiverAbortReasonCode,
  CaregiverApi,
  CaregiverCompletionScope,
  CaregiverDataClassification,
  CaregiverEndRequest,
  CaregiverHelpReasonCode,
  CaregiverPlan,
  CaregiverRuntimeState,
  CaregiverSessionStatus,
  CaregiverSessionSummary,
  CaregiverToday,
} from "./caregiver/caregiverPolicy.ts";
import {
  CAREGIVER_DEMO_COMPLETION_SCOPE,
  CAREGIVER_DEMO_POSITION_COUNT,
  CAREGIVER_DEMO_PROFILE_VERSION,
} from "./caregiver/caregiverPolicy.ts";
import { csrfHeader } from "./security/csrf.ts";

const REQUEST_TIMEOUT_MS = 12_000;
const ACCOUNT_REAUTH_REQUIRED_EVENT = "nmu:account-reauth-required";
const DATA_CLASSIFICATIONS = new Set<CaregiverDataClassification>(["research", "simulation"]);
const COMPLETION_SCOPES = new Set<CaregiverCompletionScope>([
  "canonical_full_source",
  "demo_plan_only",
]);
const RUNTIME_STATES = new Set<CaregiverRuntimeState>([
  "active",
  "paused",
  "intervention_completed",
  "completed",
  "aborted",
  "failed",
]);
function record(value: unknown, message = "服务器返回了无法确认的照护状态"): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ApiError(502, message, value, "noncanonical-json");
  }
  return value as Record<string, unknown>;
}

function stringField(value: Record<string, unknown>, key: string): string {
  const result = value[key];
  if (typeof result !== "string" || !result || result.trim() !== result) {
    throw new ApiError(502, `服务器照护回执缺少 ${key}`, value, "noncanonical-json");
  }
  return result;
}

function integerField(value: Record<string, unknown>, key: string, minimum = 0): number {
  const result = value[key];
  if (!Number.isSafeInteger(result) || (result as number) < minimum) {
    throw new ApiError(502, `服务器照护回执中的 ${key} 无效`, value, "noncanonical-json");
  }
  return result as number;
}

function nullableIntegerField(value: Record<string, unknown>, key: string): number | null {
  if (value[key] === null) return null;
  return integerField(value, key);
}

function booleanField(value: Record<string, unknown>, key: string): boolean {
  const result = value[key];
  if (typeof result !== "boolean") {
    throw new ApiError(502, `服务器照护回执中的 ${key} 无效`, value, "noncanonical-json");
  }
  return result;
}

function nullableStringField(value: Record<string, unknown>, key: string): string | null {
  const result = value[key];
  if (result === null) return null;
  if (typeof result !== "string" || !result || result.trim() !== result) {
    throw new ApiError(502, `服务器照护回执中的 ${key} 无效`, value, "noncanonical-json");
  }
  return result;
}

function dataClassificationField(candidate: Record<string, unknown>): CaregiverDataClassification {
  const value = stringField(candidate, "data_classification");
  if (!DATA_CLASSIFICATIONS.has(value as CaregiverDataClassification)) {
    throw new ApiError(502, "服务器照护回执中的 data_classification 无效", candidate, "noncanonical-json");
  }
  return value as CaregiverDataClassification;
}

function completionScopeField(candidate: Record<string, unknown>): CaregiverCompletionScope | null {
  const value = nullableStringField(candidate, "completion_scope");
  if (value !== null && !COMPLETION_SCOPES.has(value as CaregiverCompletionScope)) {
    throw new ApiError(502, "服务器照护回执中的 completion_scope 无效", candidate, "noncanonical-json");
  }
  return value as CaregiverCompletionScope | null;
}

function sessionBoundary(candidate: Record<string, unknown>) {
  return {
    isSimulation: booleanField(candidate, "is_simulation"),
    dataClassification: dataClassificationField(candidate),
    autopilotProfileVersionId: nullableStringField(candidate, "autopilot_profile_version_id"),
    completionScope: completionScopeField(candidate),
    resolvedPositionCount: nullableIntegerField(candidate, "resolved_position_count"),
    operationalDemoReady: booleanField(candidate, "operational_demo_ready"),
  };
}

async function request(method: string, path: string, body?: unknown): Promise<unknown> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...csrfHeader(method),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (response.status === 401) {
      window.dispatchEvent(new Event(ACCOUNT_REAUTH_REQUIRED_EVENT));
    }
    return decodeJsonApiResponse({
      status: response.status,
      ok: response.ok,
      statusText: response.statusText,
      text,
      retryAfter: response.headers.get("Retry-After"),
    });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(408, "请求超时，请保持页面打开，系统会继续确认");
    }
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
  }
}

function sessionSummary(value: unknown): CaregiverSessionSummary {
  const candidate = record(value);
  const weekNo = integerField(candidate, "week_no", 1);
  return {
    sessionId: stringField(candidate, "session_id"),
    participantLabel: stringField(candidate, "participant_code"),
    weekLabel: `第 ${weekNo} 周`,
    phaseLabel: stringField(candidate, "phase_type"),
    ...sessionBoundary(candidate),
  };
}

function planProjection(value: unknown): CaregiverPlan {
  const candidate = record(value);
  const weekNo = integerField(candidate, "week_no", 1);
  const scheduledTime = nullableStringField(candidate, "scheduled_time");
  if (candidate.is_simulation !== true
      || candidate.data_classification !== "simulation"
      || candidate.autopilot_profile_version_id !== CAREGIVER_DEMO_PROFILE_VERSION
      || candidate.completion_scope !== CAREGIVER_DEMO_COMPLETION_SCOPE
      || candidate.resolved_position_count !== CAREGIVER_DEMO_POSITION_COUNT
      || candidate.operational_demo_ready !== true) {
    throw new ApiError(
      502,
      "服务器照护安排未通过本机20题合成模拟门禁",
      candidate,
      "noncanonical-json",
    );
  }
  return {
    planId: stringField(candidate, "plan_id"),
    participantLabel: stringField(candidate, "participant_code"),
    scheduledDate: stringField(candidate, "scheduled_date"),
    scheduledTime,
    weekLabel: `第 ${weekNo} 周`,
    phaseLabel: stringField(candidate, "phase_type"),
    revision: integerField(candidate, "revision", 1),
    isSimulation: true,
    dataClassification: "simulation",
    autopilotProfileVersionId: CAREGIVER_DEMO_PROFILE_VERSION,
    completionScope: CAREGIVER_DEMO_COMPLETION_SCOPE,
    resolvedPositionCount: CAREGIVER_DEMO_POSITION_COUNT,
    operationalDemoReady: true,
  };
}

function parseToday(value: unknown): CaregiverToday {
  const candidate = record(value);
  if (!Array.isArray(candidate.plans)) {
    throw new ApiError(502, "服务器今日照护安排无效", candidate, "noncanonical-json");
  }
  if (candidate.current_session !== null
      && (candidate.current_session === undefined
        || typeof candidate.current_session !== "object"
        || Array.isArray(candidate.current_session))) {
    throw new ApiError(502, "服务器当前照护场次无效", candidate, "noncanonical-json");
  }
  return {
    asOfDateLabel: stringField(candidate, "as_of_date"),
    plans: candidate.plans.map(planProjection),
    withheldCount: integerField(candidate, "withheld_count"),
    currentSession: candidate.current_session === null
      ? null
      : sessionSummary(candidate.current_session),
  };
}

function runtimeState(candidate: Record<string, unknown>): CaregiverRuntimeState {
  const state = candidate.runtime_status;
  if (typeof state !== "string" || !RUNTIME_STATES.has(state as CaregiverRuntimeState)) {
    throw new ApiError(502, "服务器场次运行状态无效", candidate, "noncanonical-json");
  }
  return state as CaregiverRuntimeState;
}

function practiceState(
  runtime: CaregiverRuntimeState,
  mode: string,
  status: string,
): CaregiverSessionStatus["practiceState"] {
  if (["intervention_completed", "completed", "aborted", "failed"].includes(runtime)) return "ended";
  if (mode === "manual") return "taken_over";
  if (status === "manual_draining") return "pausing";
  if (status === "paused") return "paused";
  if (status === "scope_completed") return "ended";
  if (status === "failed") return "error";
  if (mode === "disabled" && status === "idle") return "not_started";
  if (["running", "waiting_tts", "waiting_recording", "processing_attempt"].includes(status)) {
    return "running";
  }
  return "error";
}

function parseStatus(value: unknown): CaregiverSessionStatus {
  const candidate = record(value);
  const boundary = sessionBoundary(candidate);
  const runtime = runtimeState(candidate);
  const rawAutopilot = record(candidate.autopilot, "服务器缺少自动练习状态");
  let autopilot: ReturnType<typeof parseAutopilotStatusReceipt>;
  try {
    autopilot = parseAutopilotStatusReceipt(rawAutopilot);
  } catch {
    throw new ApiError(
      502,
      "服务器自动练习状态无效",
      rawAutopilot,
      "noncanonical-json",
    );
  }
  const mode = autopilot.mode;
  const autopilotStatus = autopilot.status;
  const presence = record(candidate.patient_presence, "服务器缺少老人画面连接状态");
  if (typeof presence.online !== "boolean" || typeof candidate.active_bedside_session !== "boolean") {
    throw new ApiError(502, "服务器老人画面连接状态无效", candidate, "noncanonical-json");
  }
  const currentPractice = practiceState(runtime, mode, autopilotStatus);
  const runtimeOpen = runtime === "active" || runtime === "paused";
  return {
    sessionId: stringField(candidate, "session_id"),
    runtimeState: runtime,
    practiceState: currentPractice,
    patientPresence: candidate.active_bedside_session
      ? (presence.online ? "online" : "offline")
      : "unknown",
    runtimeRevision: integerField(candidate, "runtime_revision"),
    practiceRevision: autopilot.stateRevision,
    takeoverReady: autopilot.takeoverReady,
    ...boundary,
    allowed: {
      startPractice: runtime === "active"
        && candidate.active_bedside_session
        && boundary.operationalDemoReady
        && currentPractice === "not_started",
      pause: runtime === "active",
      help: runtimeOpen,
      takeOver: runtime === "paused"
        && mode === "autonomous"
        && currentPractice === "paused"
        && autopilot.takeoverReady,
      end: runtimeOpen,
    },
  };
}

async function getSessionStatus(sessionId: string): Promise<CaregiverSessionStatus> {
  return parseStatus(await request(
    "GET",
    `/caregiver/sessions/${encodeURIComponent(sessionId)}/status`,
  ));
}

async function mutateThenStatus(
  method: string,
  path: string,
  sessionId: string,
  body?: unknown,
): Promise<CaregiverSessionStatus> {
  await request(method, path, body);
  return getSessionStatus(sessionId);
}

function abortBody(requestBody: Extract<CaregiverEndRequest, { kind: "abort" }>): {
  reason_code: CaregiverAbortReasonCode;
  expected_revision: number;
  idempotency_key: string;
} {
  return {
    reason_code: requestBody.reasonCode,
    expected_revision: requestBody.expectedRevision,
    idempotency_key: requestBody.idempotencyKey,
  };
}

export const caregiverApi: CaregiverApi = {
  listToday: async () => parseToday(await request("GET", "/caregiver/today")),
  startVisitPlan: async (planId, command) => {
    const response = record(await request(
      "POST",
      `/caregiver/visit-plans/${encodeURIComponent(planId)}/start`,
      {
        idempotency_key: command.idempotencyKey,
        expected_revision: command.expectedRevision,
      },
    ));
    return sessionSummary(response.session);
  },
  activateSession: async (sessionId) => mutateThenStatus(
    "PUT",
    `/caregiver/sessions/${encodeURIComponent(sessionId)}/activation`,
    sessionId,
  ),
  startPractice: async (sessionId, command) => mutateThenStatus(
    "POST",
    `/sessions/${encodeURIComponent(sessionId)}/autopilot/start`,
    sessionId,
    {
      idempotency_key: command.idempotencyKey,
      expected_revision: command.expectedRevision,
    },
  ),
  getSessionStatus,
  pauseSession: async (sessionId) => mutateThenStatus(
    "POST",
    `/sessions/${encodeURIComponent(sessionId)}/pause`,
    sessionId,
  ),
  requestHelp: async (sessionId, help) => {
    await request(
      "POST",
      `/caregiver/sessions/${encodeURIComponent(sessionId)}/help-requests`,
      {
        reason_code: help.reasonCode as CaregiverHelpReasonCode,
        idempotency_key: help.idempotencyKey,
      },
    );
    return { recorded: true, status: await getSessionStatus(sessionId) };
  },
  takeOverSession: async (sessionId, command) => mutateThenStatus(
    "POST",
    `/sessions/${encodeURIComponent(sessionId)}/autopilot/takeover`,
    sessionId,
    {
      idempotency_key: command.idempotencyKey,
      expected_revision: command.expectedRevision,
    },
  ),
  endSession: async (sessionId, end) => end.kind === "finish"
    ? mutateThenStatus(
      "POST",
      `/sessions/${encodeURIComponent(sessionId)}/finish-intervention`,
      sessionId,
    )
    : mutateThenStatus(
      "POST",
      `/sessions/${encodeURIComponent(sessionId)}/abort`,
      sessionId,
      abortBody(end),
    ),
};

// Exported only for closed-contract unit tests; the workspace receives caregiverApi by injection.
export const caregiverApiContract = {
  parseToday,
  parseStatus,
  nullableStringField,
};
