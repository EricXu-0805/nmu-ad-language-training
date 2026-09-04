// 强类型 API 客户端——逐个后端路由一个函数。相对路径:dev 走 Vite 代理、生产由 FastAPI 同源托管。
// 浏览器只访问同源后端；后端可按部署配置调用云 TTS/ASR/LLM。任何非 2xx 都明确抛错。
import type {
  AbnormalEvent, AttemptProcessRequest, AttemptProcessResult, AudioAsset, AudioCaptureReceipt, AuditEntry, AuditVerify, AuthConfig, AuthIdentity, ExportResult, InteractionAppendRequest, InteractionEvent, ItemBankInfo, ItemEvent,
  CloudProcessingPolicy, LiveStateResponse, Patient, PatientHeartbeatRequest, PatientHeartbeatResponse,
  PatientProfilePatch,
  PatientSummary, ScaleResult, ScoreReconstruction, Session, SessionRuntimeState, SessionRuntimeStatus, TurnEvent,
  WithdrawalReasonCode,
  VisitPlanCancelRequest, VisitPlanCreateRequest, VisitPlanMutationRequest, VisitPlanReceipt, VisitPlanToday,
  AssessmentCancelRequest, AssessmentCloseRequest, AssessmentDeferralRequest,
  AssessmentEvent, AssessmentEventCreateRequest, AssessmentEventsToday,
  AssessmentInstance, AssessmentInstanceMutationRequest, AssessmentItemResponseRequest,
  AssessmentStartRequest,
} from "./types";
import { parseAccountSessionPlan } from "./sessionPlan.ts";
import { ApiError, apiNetworkError, decodeJsonApiResponse } from "./apiResponse";
import {
  createDeviceCapabilityStore,
  type DeviceCapabilityRecord,
} from "./security/deviceCapability";
import {
  createPatientBindingStore,
  type PatientBindingRecord,
} from "./security/patientBinding";
import {
  attachHintFor,
  classifyAttachOutcome,
  type AttachDisposition,
  type AttachHint,
} from "./patient/bindingAttachPolicy";
import { csrfHeader } from "./security/csrf";
import {
  parseResearchDictionary,
  parseResearchMeta,
  parseResearchPage,
  researchDatasetPath,
  type ResearchDataClassification,
  type ResearchDictionaryRow,
  type ResearchMeta,
  type ResearchPage,
} from "./console/research/researchDataContract";
import {
  buildAutopilotResumeRequest,
  buildAutopilotStartRequest,
  buildAutopilotTakeoverRequest,
  parseAutopilotStatusReceipt,
  type AutopilotStartReceipt,
} from "./autopilot/startControl";
import {
  parseProviderReadiness,
  type ProviderReadiness,
} from "./autopilot/providerReadiness";
import {
  parsePatientSessionList,
  parseStartedVisitSession,
  parseVisitPlanList,
  parseVisitPlanReceipt,
  parseVisitPlanToday,
  reconcileStartedVisitPlan,
  startedVisitSessionId,
} from "./visitPlans";
import { parseSessionRuntimeState } from "./sessionRuntime";
import type { SessionAbortReasonCode } from "./sessionAbort";
import {
  parseSessionCloseoutRecord,
  parseSessionOutcomeSummary,
  type SessionCloseoutOutcomeSummary,
  type SessionCloseoutRecord,
  type SessionCloseoutSaveRequest,
} from "./console/sessionCloseout";
import { parseScaleProtocolReadiness } from "./console/scaleProtocol";
import {
  parseQuestionnaireCatalog,
  parseQuestionnaireRecord,
  parseQuestionnaireRecordList,
  type QuestionnaireCatalogEntry,
  type QuestionnairePhaseLabel,
  type QuestionnaireRecord,
  type QuestionnaireValueWrite,
} from "./console/questionnaires";
import {
  assertFormalAssessmentMutationReady,
  parseAssessmentEvent,
  parseAssessmentEventList,
  parseAssessmentEventsToday,
  type FormalAssessmentMutationReadiness,
} from "./console/formalAssessment";
import {
  parsePatientWithdrawalReceipt,
  parseWithdrawnAudioGovernanceRows,
} from "./console/subjectWithdrawal";
import {
  parseAIQualityDashboard,
  type AIQualityDashboardContract,
  type QualityDataClassification,
} from "./console/quality/qualityDashboardContract";
import {
  isFrozenReleasePayload,
  parseAIQualityRelease,
  type AIQualityReleaseContract,
} from "./console/quality/qualityReleaseContract";
import { qualityDashboardRequestPath } from "./console/quality/qualityDashboardRequestPolicy";
import { parseSessionAiUsage, type SessionAiUsageContract } from "./console/sessionAiUsageContract";
import type { CursorMsg } from "./sync/messages";
import {
  parsePatientPauseReceipt,
  type PatientPauseReceipt,
} from "./patient/patientPause";

export { ApiError } from "./apiResponse";

export type InteractionPresentationDraft = {
  idempotency_key: string;
  interaction: Extract<
    InteractionAppendRequest,
    { event_type: "cue_selected" | "feedback_selected" }
  >;
  // fbSeq is a server clock on the atomic route; callers cannot manufacture it.
  cursor: Omit<CursorMsg, "type" | "wseq" | "fbSeq">;
};

export type InteractionPresentationRequest = Omit<InteractionPresentationDraft, "cursor"> & {
  cursor: InteractionPresentationDraft["cursor"] & {
    wseq: number;
    expected_revision: number;
  };
};

export interface InteractionPresentationReceipt {
  interaction: InteractionEvent;
  cursor: Omit<CursorMsg, "type" | "wseq"> & { wseq: number };
  seq: number;
  wseq: number;
  runtimeRevision: number;
  idempotent: boolean;
}

export interface TechnicalPauseDraft {
  idempotency_key: string;
  error_code: string;
  attempt_id?: number;
}

export interface TechnicalPauseRequest extends TechnicalPauseDraft {
  expected_revision: number;
  expected_live_wseq: number;
}

export interface TechnicalPauseReceipt {
  interaction: InteractionEvent;
  runtime: SessionRuntimeState;
  cursor: Record<string, unknown> & { wseq: number };
  seq: number;
  wseq: number;
  runtimeRevision: number;
  idempotent: boolean;
}

// PIN 从不落存储，只在 POST /device/pair 的一次调用栈中出现。短时 capability 与
// 随机非 PII 设备 id 都限定在当前标签页 sessionStorage，关标签页即消失。
const deviceStore = createDeviceCapabilityStore(localStorage, sessionStorage);
export const DEVICE_PAIR_REQUIRED_EVENT = "nmu:device-pair-required";
export const DEVICE_CAPABILITY_UPDATED_EVENT = "nmu:device-capability-updated";
export const ACCOUNT_REAUTH_REQUIRED_EVENT = "nmu:account-reauth-required";
export const PATIENT_BINDING_UPDATED_EVENT = "nmu:patient-binding-updated";

// 受试者绑定长期存 localStorage(它只能换本人场次能力,不是路由凭据);
// 短时场次能力仍只在 sessionStorage,老规则不变。
const patientBindingStore = createPatientBindingStore(localStorage);
export const getPatientBinding = (): PatientBindingRecord | null =>
  patientBindingStore.get();
export const clearPatientBinding = (): void => {
  patientBindingStore.clear();
  queueMicrotask(() => window.dispatchEvent(new Event(PATIENT_BINDING_UPDATED_EVENT)));
};

export const getDeviceCapability = (): DeviceCapabilityRecord | null => deviceStore.get();
export const getRecoveryDeviceCapability = (sessionId: string): DeviceCapabilityRecord | null =>
  deviceStore.getRecovery(sessionId);
export const removeRecoveryDeviceCapabilityIfMatches = (record: DeviceCapabilityRecord): boolean =>
  // Recovery cleanup is deliberately silent: an old-session slot must never
  // blank, stop, or re-pair the currently active patient UI.
  deviceStore.removeRecoveryIfMatches(record);
export const clearDeviceCapability = (): void => {
  deviceStore.clear();
  queueMicrotask(() => window.dispatchEvent(new Event(DEVICE_CAPABILITY_UPDATED_EVENT)));
};

export interface DeviceCredentialSelection {
  headers: Record<string, string>;
  source: "active" | "recovery" | null;
  record: DeviceCapabilityRecord | null;
}

export function selectDeviceCredential(
  sessionId?: string,
  allowRecovery = false,
): DeviceCredentialSelection {
  const active = deviceStore.get();
  if (active && (!sessionId || active.sessionId === sessionId)) {
    return {
      headers: { "X-Device-Capability": active.capability },
      source: "active",
      record: active,
    };
  }
  if (allowRecovery && sessionId) {
    const recovery = deviceStore.getRecovery(sessionId);
    if (recovery) {
      return {
        headers: { "X-Device-Capability": recovery.capability },
        source: "recovery",
        record: recovery,
      };
    }
  }
  return { headers: {}, source: null, record: null };
}

export function deviceCapabilityHeader(): Record<string, string> {
  return selectDeviceCredential().headers;
}

function errorCodeFromText(text: string): string | null {
  if (!text) return null;
  try {
    const value = JSON.parse(text) as unknown;
    if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
    const topCode = (value as { code?: unknown }).code;
    if (typeof topCode === "string") return topCode;
    const detail = (value as { detail?: unknown }).detail;
    if (detail !== null && typeof detail === "object" && !Array.isArray(detail)) {
      const nestedCode = (detail as { code?: unknown }).code;
      return typeof nestedCode === "string" ? nestedCode : null;
    }
  } catch { /* proxy/non-JSON body has no stable code */ }
  return null;
}

export function handleDeviceAuthorizationFailure(
  status: number,
  text: string,
  credential: DeviceCredentialSelection = selectDeviceCredential(),
): boolean {
  const code = errorCodeFromText(text);
  // A payload/session mismatch is a request bug, not evidence that the token
  // itself is dead.  In particular it must not discard a recovery slot that may
  // still be needed for another exact audio ACK.
  if (status === 409 && code === "device_session_mismatch") return false;
  const sessionChanged = status === 409 && code === "device_session_changed";
  const recoveryTransition = (status === 401 || status === 409)
    && code === "device_capability_recovery_only";
  if (status !== 401 && !sessionChanged && !recoveryTransition) return false;
  if (credential.source === "recovery" && credential.record) {
    // Recovery-only/changed/mismatched routes can still leave this token valid
    // for exact persisted ACKs.  Remove only this exact token on a stable terminal
    // capability error; never remove a newly stored replacement by session id.
    const terminal = status === 401 && (
      code === "device_capability_invalid"
      || code === "device_capability_expired"
      || code === "device_capability_revoked"
    );
    // Recovery-slot churn belongs to an old outbox and must not blank or stop the
    // currently authorized active-session UI.
    if (terminal) deviceStore.removeRecoveryIfMatches(credential.record);
    return false;
  }
  if (credential.source === null) {
    // A no-header request can finish after a successful pair.  Its 401 must not
    // reopen the prompt or disturb the newly installed active credential.
    if (deviceStore.get()) return false;
    queueMicrotask(() => {
      if (!deviceStore.get()) window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
    });
    return true;
  }
  let changed = false;
  if (credential.source === "active" && credential.record) {
    if (code === "device_capability_recovery_only" || code === "device_session_changed") {
      try { changed = deviceStore.demoteIfMatches(credential.record); }
      catch { changed = deviceStore.clearIfMatches(credential.record); }
    } else if (status === 401) {
      if (code === "device_capability_revoked") {
        deviceStore.removeRecoveryIfMatches(credential.record);
      }
      changed = deviceStore.clearIfMatches(credential.record);
    }
  }
  // A late failure from token A is harmless once token B is active.  Only the
  // request that actually removed/demoted its exact credential may blank the
  // authorized mirror or ask for pairing again.
  if (!changed) return false;
  queueMicrotask(() => {
    window.dispatchEvent(new Event(DEVICE_CAPABILITY_UPDATED_EVENT));
    if (!deviceStore.get()) window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
  });
  return true;
}

async function pairDevice(pin: string): Promise<DeviceCapabilityRecord> {
  const suppliedPin = pin.trim();
  if (!suppliedPin) throw new ApiError(422, "请输入设备 PIN");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), DEFAULT_REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch("/device/pair", {
      method: "POST",
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Console-Pin": suppliedPin,
      },
      body: JSON.stringify({ deviceId: deviceStore.getOrCreateDeviceId() }),
    });
    const text = await res.text();
    const parsed = decodeJsonApiResponse({
      status: res.status,
      ok: res.ok,
      statusText: res.statusText,
      text,
    });
    const record = deviceStore.save(parsed);
    queueMicrotask(() => window.dispatchEvent(new Event(DEVICE_CAPABILITY_UPDATED_EVENT)));
    return record;
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(408, "设备配对超时，请检查连接后重试");
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
  }
}

// 配对响应的宽形状:全局 PIN 只回场次能力三元组;受试者配对码额外带 binding,
// 且本人此刻没有 live 场次时只回 binding。
export type PairWithCodeResult =
  | { kind: "session"; record: DeviceCapabilityRecord }
  | { kind: "binding" };

async function pairDeviceWithCode(pin: string): Promise<PairWithCodeResult> {
  const suppliedPin = pin.trim();
  if (!suppliedPin) throw new ApiError(422, "请输入配对码");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), DEFAULT_REQUEST_TIMEOUT_MS);
  try {
    const deviceId = deviceStore.getOrCreateDeviceId();
    const res = await fetch("/device/pair", {
      method: "POST",
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-Console-Pin": suppliedPin,
      },
      body: JSON.stringify({ deviceId }),
    });
    const text = await res.text();
    const parsed = decodeJsonApiResponse({
      status: res.status,
      ok: res.ok,
      statusText: res.statusText,
      text,
    }) as Record<string, unknown>;
    const binding = parsed.binding;
    const bindingSaved = typeof binding === "string";
    if (bindingSaved) {
      patientBindingStore.save({ binding, deviceId });
    }
    if (parsed.capability !== undefined) {
      const record = deviceStore.save({
        capability: parsed.capability,
        sessionId: parsed.sessionId,
        expiresAt: parsed.expiresAt,
      });
      queueMicrotask(() => {
        window.dispatchEvent(new Event(DEVICE_CAPABILITY_UPDATED_EVENT));
        if (bindingSaved) window.dispatchEvent(new Event(PATIENT_BINDING_UPDATED_EVENT));
      });
      return { kind: "session", record };
    }
    if (!bindingSaved) throw new ApiError(502, "服务器配对响应缺少凭据");
    queueMicrotask(() => window.dispatchEvent(new Event(PATIENT_BINDING_UPDATED_EVENT)));
    return { kind: "binding" };
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(408, "设备配对超时，请检查连接后重试");
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
  }
}

const ATTACH_REQUEST_TIMEOUT_MS = 8_000;

// 自动跟场:绑定令牌换当前本人 live 场次能力。全部失败形态按纯策略层分派;
// 老人端调用方只看处置结果,永不弹错误。
export interface AttachResult {
  disposition: AttachDisposition;
  hint: AttachHint;
}

async function attachPatientDevice(): Promise<AttachResult> {
  const bindingRecord = patientBindingStore.get();
  if (!bindingRecord) return { disposition: "drop_binding", hint: null };
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), ATTACH_REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch("/device/attach", {
      method: "POST",
      signal: controller.signal,
      credentials: "omit",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deviceId: bindingRecord.deviceId,
        binding: bindingRecord.binding,
      }),
    });
    const text = await res.text();
    const code = errorCodeFromText(text);
    const disposition = classifyAttachOutcome(res.status, code);
    const hint = attachHintFor(res.status, code);
    if (disposition === "attached") {
      try {
        deviceStore.save(JSON.parse(text) as unknown);
      } catch {
        // 响应损坏不等于绑定已死:保留绑定,下一轮再试。
        return { disposition: "quiet_retry", hint: null };
      }
      queueMicrotask(() => window.dispatchEvent(new Event(DEVICE_CAPABILITY_UPDATED_EVENT)));
      return { disposition: "attached", hint: null };
    }
    if (disposition === "drop_binding") {
      patientBindingStore.clear();
      queueMicrotask(() => {
        window.dispatchEvent(new Event(PATIENT_BINDING_UPDATED_EVENT));
        // 绑定死了又没有能力:回到人工配对入口,别让老人端悄悄卡死。
        if (!deviceStore.get()) window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
      });
    }
    return { disposition, hint };
  } catch {
    return { disposition: "quiet_retry", hint: null };
  } finally {
    window.clearTimeout(timeout);
  }
}

const DEFAULT_REQUEST_TIMEOUT_MS = 12_000;
// 研究取数一页最多 1000 行，还要在服务端过一遍去标识断言，比普通读慢。
const RESEARCH_READ_TIMEOUT_MS = 45_000;

interface RequestOptions {
  silent401?: boolean;
  device?: boolean;
  deviceSessionId?: string;
  allowRecovery?: boolean;
  noStore?: boolean;
  signal?: AbortSignal;
  // best-effort 治理上报专用:失败不得触发配对/重认证 UI(开放本地模式下
  // 删除回执的 401 是预期结果,绝不能在老人屏弹 PIN 弹窗)。
  silentDeviceAuthFailure?: boolean;
}

function payloadSessionId(payload: object): string | undefined {
  const record = payload as { sessionId?: unknown; session_id?: unknown };
  const camel = typeof record.sessionId === "string" ? record.sessionId : undefined;
  const snake = typeof record.session_id === "string" ? record.session_id : undefined;
  return camel && snake && camel !== snake ? undefined : camel ?? snake;
}

// silent401:登录/自查这类"401 是预期结果"的调用不广播重认证事件。
async function req<T>(method: string, path: string, body?: unknown,
                     timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, opts?: RequestOptions): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(opts?.signal?.reason);
  if (opts?.signal?.aborted) abortFromCaller();
  else opts?.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const credential = opts?.device
      ? selectDeviceCredential(opts.deviceSessionId, opts.allowRecovery)
      : null;
    // Headerless device requests are intentional in the explicitly open local
    // development mode.  Let the server decide: protected deployments return a
    // stable 401 and trigger pairing; open mode can complete without a token.
    const res = await fetch(path, {
      method,
      signal: controller.signal,
      // Patient-device transport never sends the research account cookie.  This
      // prevents a logged-in same-origin console from silently replacing pairing.
      credentials: opts?.device ? "omit" : "same-origin",
      cache: opts?.device || opts?.noStore ? "no-store" : "default",
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(credential?.headers ?? {}),
        ...csrfHeader(method),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    if (!res.ok && opts?.device && !opts?.silentDeviceAuthFailure) {
      handleDeviceAuthorizationFailure(res.status, text, credential ?? undefined);
    }
    else if (!res.ok && res.status === 401 && !opts?.silent401) {
      window.dispatchEvent(new Event(ACCOUNT_REAUTH_REQUIRED_EVENT));
    }
    return decodeJsonApiResponse({
      status: res.status,
      ok: res.ok,
      statusText: res.statusText,
      text,
      retryAfter: res.headers.get("Retry-After"),
    }) as T;
  } catch (error) {
    if (timedOut) throw new ApiError(408, "请求超时，请检查本机服务连接后重试");
    if (opts?.signal?.aborted) throw opts.signal.reason instanceof Error
      ? opts.signal.reason : new DOMException("请求已取消", "AbortError");
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
    opts?.signal?.removeEventListener("abort", abortFromCaller);
  }
}

// CSV 走裸 fetch 而不是 <a download>：顶层导航会把控制台带走，而且失败时浏览器
// 会把错误响应当文件存下来。这样非 2xx 仍走与 JSON 读同一条错误解码路径。
async function fetchResearchCsv(path: string): Promise<Blob> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), RESEARCH_READ_TIMEOUT_MS);
  try {
    const res = await fetch(path, {
      method: "GET", signal: controller.signal,
      credentials: "same-origin", cache: "no-store",
    });
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new Event(ACCOUNT_REAUTH_REQUIRED_EVENT));
      const text = await res.text();
      decodeJsonApiResponse({
        status: res.status, ok: false, statusText: res.statusText, text,
        retryAfter: res.headers.get("Retry-After"),
      });
    }
    return await res.blob();
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(408, "导出超时，请把每页行数调小后重试");
    throw apiNetworkError(error);
  } finally {
    window.clearTimeout(timeout);
  }
}

interface TurnConfirmationRequest {
  confirmed_response_text: string;
  expected_revision: number;
  idempotency_key: string;
}

export function parseTurnConfirmationReceipt(
  value: TurnEvent,
  turnId: number,
  request: TurnConfirmationRequest,
): TurnEvent {
  if (value === null || typeof value !== "object"
      || value.id !== turnId
      || !Number.isInteger(value.confirmation_revision)
      || value.confirmation_revision !== request.expected_revision + 1
      || value.confirmed_response_text !== request.confirmed_response_text.trim()) {
    throw new ApiError(
      502,
      "服务器的确认结果与本次提交不一致，这次修改没有生效——请直接重试，不会重复保存",
      value,
      "noncanonical-json",
    );
  }
  return value;
}

function assessmentReceiptExpectation(event: AssessmentEvent) {
  return {
    patientId: event.patient_id,
    eventId: event.event_id,
    definitionDigests: Object.fromEntries(
      event.instances.map((instance) => [instance.category_key, instance.definition_digest]),
    ),
  };
}

function requireAssessmentInstanceTarget(
  event: AssessmentEvent,
  instance: AssessmentInstance,
): void {
  const exact = event.instances.find((candidate) => candidate.instance_id === instance.instance_id);
  if (!exact || exact.event_id !== event.event_id || exact.patient_id !== event.patient_id
      || exact.category_key !== instance.category_key
      || exact.definition_digest !== instance.definition_digest
      || exact.revision !== instance.revision) {
    throw new Error("正式评估实例与当前事件快照不一致，已阻止写入");
  }
}

export const api = {
  health: () => req<{ status: string; service: string }>("GET", "/health"),
  pairDevice,
  pairDeviceWithCode,
  attachPatientDevice,

  // 账号认证(公网部署)。cookie 走同源自动携带;登录/登出/自查/配置。
  // authMe/authLogin 的 401 是预期结果(未登录/密码错),silent401 免触发重认证事件。
  authConfig: () => req<AuthConfig>("GET", "/auth/config"),
  authMe: () => req<AuthIdentity>("GET", "/auth/me", undefined, DEFAULT_REQUEST_TIMEOUT_MS, { silent401: true }),
  authLogin: (username: string, password: string) =>
    req<AuthIdentity>("POST", "/auth/login", { username, password }, DEFAULT_REQUEST_TIMEOUT_MS, { silent401: true }),
  authLogout: () => req<{ ok: boolean }>("POST", "/auth/logout", undefined, DEFAULT_REQUEST_TIMEOUT_MS, { silent401: true }),
  providerReadiness: async (): Promise<ProviderReadiness> =>
    parseProviderReadiness(await req<unknown>(
      "GET", "/ai/provider-readiness", undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    )),
  probeProviderReadiness: async (): Promise<ProviderReadiness> =>
    parseProviderReadiness(await req<unknown>(
      "POST", "/ai/provider-readiness/probe", undefined,
      75_000, { noStore: true },
    )),

  // 患者 / 场次
  createPatient: (p: Patient) => req<Patient>("POST", "/patients", p),
  updatePatient: (id: string, patch: PatientProfilePatch) =>
    req<Patient>("PATCH", `/patients/${encodeURIComponent(id)}`, patch),
  getPatient: (id: string) => req<Patient>("GET", `/patients/${encodeURIComponent(id)}`),
  listPatients: () => req<PatientSummary[]>(
    "GET", "/patients", undefined, DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
  ),
  getAIQualityMetrics: async (
    classification: QualityDataClassification,
    signal?: AbortSignal,
  ): Promise<AIQualityDashboardContract | AIQualityReleaseContract> => {
    const body = await req<unknown>(
      "GET", qualityDashboardRequestPath(classification), undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
    );
    // 研究分区在配了冻结发布之后返回的是另一份契约，字段集合与 v2 聚合
    // 有意不同（精确计数全部拿掉）。按 schema_version 分派，不要试图让
    // 一个解析器同时吃两种形状——那些为它开的 if 迟早会被误用到 v2 上。
    return isFrozenReleasePayload(body)
      ? parseAIQualityRelease(body)
      : parseAIQualityDashboard(body, classification);
  },
  patientSessions: async (id: string): Promise<Session[]> =>
    parsePatientSessionList(await req<unknown>(
      "GET", `/patients/${encodeURIComponent(id)}/sessions`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), id),

  // 只读研究数据面。全部 GET，零写入口；投影与去标识导出包同一口径。
  researchMeta: async (signal?: AbortSignal): Promise<ResearchMeta> =>
    parseResearchMeta(await req<unknown>(
      "GET", "/research/v1/meta", undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
    )),
  researchDictionary: async (signal?: AbortSignal): Promise<ResearchDictionaryRow[]> =>
    parseResearchDictionary(await req<unknown>(
      "GET", "/research/v1/dictionary", undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
    )),
  researchDataset: async (request: {
    dataset: string;
    classification: ResearchDataClassification;
    cursor?: string | null;
    limit?: number | null;
    signal?: AbortSignal;
  }): Promise<ResearchPage> => parseResearchPage(await req<unknown>(
    "GET", researchDatasetPath(request), undefined,
    RESEARCH_READ_TIMEOUT_MS, { noStore: true, signal: request.signal },
  ), request.dataset, request.classification),
  researchCsv: (request: {
    dataset: string;
    classification: ResearchDataClassification;
    cursor?: string | null;
    limit?: number | null;
  }): Promise<Blob> =>
    fetchResearchCsv(researchDatasetPath({ ...request, csv: true })),
  researchDictionaryCsv: (): Promise<Blob> =>
    fetchResearchCsv("/research/v1/dictionary.csv"),
  cloudProcessingPolicy: () => req<CloudProcessingPolicy>("GET", "/cloud-processing/policy"),
  setPatientCloudProcessing: (
    id: string,
    allowed: boolean,
    expected: Patient,
    policy: CloudProcessingPolicy | null = null,
  ) => req<Patient>("PATCH", `/patients/${encodeURIComponent(id)}/cloud-processing`, {
    allowed,
    expected: {
      allowed: expected.cloud_processing_allowed ?? null,
      provider_id: expected.cloud_processing_provider_id ?? null,
      notice_version: expected.cloud_processing_notice_version ?? null,
      consented_at: expected.cloud_processing_consented_at ?? null,
      revoked_at: expected.cloud_processing_revoked_at ?? null,
      withdrawal_status: expected.withdrawal_status ?? null,
      governance_revision: expected.governance_revision,
    },
    policy_provider_id: allowed ? policy?.provider_id ?? null : null,
    policy_notice_version: allowed ? policy?.notice_version ?? null : null,
  }),
  withdrawPatient: async (
    id: string,
    body: {
      idempotency_key: string;
      expected_governance_revision: number;
      reason_code: WithdrawalReasonCode;
    },
  ) => parsePatientWithdrawalReceipt(await req<unknown>(
      "POST", `/patients/${encodeURIComponent(id)}/withdrawal`, body,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), id),
  patientWithdrawal: async (id: string) => parsePatientWithdrawalReceipt(
    await req<unknown>(
      "GET", `/patients/${encodeURIComponent(id)}/withdrawal`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), id,
  ),
  withdrawnAudioGovernance: async (patientId?: string) => {
    const query = patientId
      ? `?${new URLSearchParams({ patient_id: patientId })}`
      : "";
    return parseWithdrawnAudioGovernanceRows(await req<unknown>(
      "GET", `/governance/withdrawn-audio${query}`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ));
  },
  deleteWithdrawnAudio: (rawAudioId: string, sessionId: string) => {
    const query = new URLSearchParams({
      source: "withdrawal",
      session_id: sessionId,
    });
    return req<{ raw_audio_id: string; status: string; bytes_deleted: boolean }>(
      "DELETE", `/audio/${encodeURIComponent(rawAudioId)}?${query}`,
    );
  },

  // 测试前训练安排只接受严格服务端收据；不缓存姓名，也不由浏览器生成 plan/session id。
  createVisitPlan: async (body: VisitPlanCreateRequest): Promise<VisitPlanReceipt> =>
    parseVisitPlanReceipt(await req<unknown>("POST", "/visit-plans", {
      idempotency_key: body.idempotency_key,
      patient_id: body.patient_id,
      scheduled_date: body.scheduled_date,
      scheduled_time: body.scheduled_time ?? null,
      queue_order: body.queue_order ?? null,
      session_sitting_no: body.session_sitting_no ?? 1,
      week_no: body.week_no,
      phase_type: body.phase_type,
      event_line: body.event_line,
      ...(body.autopilot_profile_version_id === undefined
        ? {}
        : { autopilot_profile_version_id: body.autopilot_profile_version_id }),
    }), { patientId: body.patient_id }),
  listTodayVisitPlans: async (): Promise<VisitPlanToday> =>
    parseVisitPlanToday(await req<unknown>(
      "GET", "/visit-plans/today", undefined, DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    )),
  listPatientVisitPlans: async (patientId: string): Promise<VisitPlanReceipt[]> => {
    const query = new URLSearchParams({ patient_id: patientId });
    return parseVisitPlanList(await req<unknown>(
      "GET", `/visit-plans?${query}`, undefined, DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), patientId);
  },
  approveVisitPlan: async (
    planId: string,
    body: VisitPlanMutationRequest,
  ): Promise<VisitPlanReceipt> => parseVisitPlanReceipt(await req<unknown>(
    "POST", `/visit-plans/${encodeURIComponent(planId)}/approve`,
    { idempotency_key: body.idempotency_key, expected_revision: body.expected_revision },
  ), { planId, status: "approved" }),
  startVisitPlan: async (
    approvedPlan: VisitPlanReceipt,
    body: VisitPlanMutationRequest,
  ): Promise<VisitPlanReceipt> => {
    const planId = approvedPlan.plan_id;
    const receipt = parseVisitPlanReceipt(await req<unknown>(
      "POST", `/visit-plans/${encodeURIComponent(planId)}/start`,
      { idempotency_key: body.idempotency_key, expected_revision: body.expected_revision },
    ), { planId, patientId: approvedPlan.patient_id, status: "started" });
    // 启动请求不含 session_id；只从严格 started receipt 接受服务端场次。
    startedVisitSessionId(receipt);
    return reconcileStartedVisitPlan(approvedPlan, receipt);
  },
  cancelVisitPlan: async (
    planId: string,
    body: VisitPlanCancelRequest,
  ): Promise<VisitPlanReceipt> => parseVisitPlanReceipt(await req<unknown>(
    "POST", `/visit-plans/${encodeURIComponent(planId)}/cancel`,
    {
      idempotency_key: body.idempotency_key,
      expected_revision: body.expected_revision,
      reason_code: body.reason_code,
    },
  ), { planId, status: "cancelled" }),

  // 研究审计账本(只读)。按受试者/场次过滤;verify 校验哈希链是否被篡改。
  listAudit: (params: { patientId?: string; sessionId?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.patientId) q.set("patient_id", params.patientId);
    if (params.sessionId) q.set("session_id", params.sessionId);
    if (params.limit) q.set("limit", String(params.limit));
    const qs = q.toString();
    return req<AuditEntry[]>("GET", `/audit${qs ? `?${qs}` : ""}`);
  },
  auditVerify: () => req<AuditVerify>("GET", "/audit/verify"),
  getTrainSession: (sid: string) => req<Session>("GET", `/sessions/${encodeURIComponent(sid)}`),
  getStartedVisitSession: async (receipt: VisitPlanReceipt): Promise<Session> => {
    const sid = startedVisitSessionId(receipt);
    return parseStartedVisitSession(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), receipt);
  },
  sessionJournal: (sid: string, signal?: AbortSignal) => req<{
    session: Session; items: ItemEvent[]; turns: TurnEvent[]; audios: AudioAsset[]; abnormal: AbnormalEvent[];
    attempts: import("./types").AttemptEvent[]; interactions: import("./types").InteractionEvent[];
    audio_receipts: AudioCaptureReceipt[];
    // 未经运行时校验的原始负载；调用方必须用 sessionAiEvidenceContract 的严格 parser
    // 校验后才能展示，缺字段/畸形不得静默当空数组。
    tts_serves: unknown;
    rapport_utterances: unknown;
    confirmation_revisions: unknown;
  }>(
    "GET", `/sessions/${encodeURIComponent(sid)}/journal`, undefined,
    DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
  ),
  rapportReplyCreate: (sid: string, body: {
    sectionKey: string; questionIdx: number; mode: "auto" | "bank" | "script";
    replyId?: string; rawAudioId?: string;
  }) => req<{
    utteranceId: number; text: string;
    source: "script" | "bank" | "llm" | "fallback";
    replyId: string | null; degradedReason: string | null; idempotent: boolean;
    round: number; maxRounds: number; final: boolean; invitesMore: boolean;
  }>("POST", `/sessions/${encodeURIComponent(sid)}/rapport/replies`, body),
  audioReceipts: (sid: string, afterSeq = 0, limit = 500) => {
    const q = new URLSearchParams({ after_seq: String(afterSeq), limit: String(limit) });
    return req<{
      session_id: string; after_seq: number; last_seq: number; receipts: AudioCaptureReceipt[];
    }>("GET", `/sessions/${encodeURIComponent(sid)}/audio-receipts?${q}`);
  },
  getSessionRuntime: async (sid: string, signal?: AbortSignal): Promise<SessionRuntimeState> =>
    parseSessionRuntimeState(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/runtime`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
    ), sid),
  getSessionAiUsage: async (sid: string, signal?: AbortSignal): Promise<SessionAiUsageContract> =>
    parseSessionAiUsage(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/ai-usage`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true, signal },
    ), sid),
  pauseSession: async (sid: string): Promise<SessionRuntimeState> =>
    parseSessionRuntimeState(await req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/pause`,
    ), sid),
  patientPause: async (sid: string, idempotencyKey: string): Promise<PatientPauseReceipt> =>
    parsePatientPauseReceipt(await req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/patient-pause`,
      { idempotency_key: idempotencyKey }, DEFAULT_REQUEST_TIMEOUT_MS,
      { device: true, deviceSessionId: sid, noStore: true },
    ), sid),
  resumeSession: async (sid: string): Promise<SessionRuntimeState> =>
    parseSessionRuntimeState(await req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/resume`,
    ), sid),
  abortSession: async (
    sid: string,
    operation: {
      reason_code: SessionAbortReasonCode;
      expected_revision: number;
      idempotency_key: string;
    },
  ): Promise<SessionRuntimeState> => parseSessionRuntimeState(await req<unknown>(
    "POST", `/sessions/${encodeURIComponent(sid)}/abort`,
    operation,
  ), sid),
  finishIntervention: async (sid: string): Promise<SessionRuntimeState> =>
    parseSessionRuntimeState(await req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/finish-intervention`,
    ), sid),
  getSessionOutcomeSummary: async (sid: string): Promise<SessionCloseoutOutcomeSummary> =>
    parseSessionOutcomeSummary(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/outcome-summary`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), sid),
  getSessionCloseout: async (sid: string): Promise<SessionCloseoutRecord> =>
    parseSessionCloseoutRecord(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/closeout`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), sid),
  saveSessionCloseout: async (
    sid: string,
    body: SessionCloseoutSaveRequest,
  ): Promise<SessionCloseoutRecord> => parseSessionCloseoutRecord(await req<unknown>(
    "PUT", `/sessions/${encodeURIComponent(sid)}/closeout`, body,
  ), sid),
  completeSession: async (sid: string): Promise<SessionRuntimeState> =>
    parseSessionRuntimeState(await req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/complete`,
    ), sid),
  // 具名研究者显式启动模拟 P0a。响应在进入控制台状态前缩成不含题目、答案、
  // 设备凭据的最小收据；患者设备命令只能走独立 capability 路由获取。
  startAutopilotP0a: async (sid: string): Promise<AutopilotStartReceipt> =>
    parseAutopilotStatusReceipt(await req<unknown>(
      "POST",
      `/sessions/${encodeURIComponent(sid)}/autopilot/start`,
      buildAutopilotStartRequest(sid),
    )),
  autopilotStatus: async (sid: string): Promise<AutopilotStartReceipt> =>
    parseAutopilotStatusReceipt(await req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/autopilot/status`,
    )),
  takeoverAutopilot: async (
    sid: string,
    stateRevision: number,
  ): Promise<AutopilotStartReceipt> =>
    parseAutopilotStatusReceipt(await req<unknown>(
      "POST",
      `/sessions/${encodeURIComponent(sid)}/autopilot/takeover`,
      buildAutopilotTakeoverRequest(sid, stateRevision),
    )),
  resumeAutopilot: async (
    sid: string,
    stateRevision: number,
  ): Promise<AutopilotStartReceipt> =>
    parseAutopilotStatusReceipt(await req<unknown>(
      "POST",
      `/sessions/${encodeURIComponent(sid)}/autopilot/resume`,
      buildAutopilotResumeRequest(sid, stateRevision),
    )),
  recordingAuthorization: (sid: string, opts?: { device?: boolean }) =>
    req<{ allowed: boolean; runtime_status: SessionRuntimeStatus; is_simulation: boolean }>(
      "POST", `/sessions/${encodeURIComponent(sid)}/recording-authorization`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, opts?.device ? { ...opts, deviceSessionId: sid } : opts),

  // 内容 / 计划
  // 不带周次 = 第 2 周就绪探针语义;训练台必须带场次周次,否则第 3~8 周
  // 场次会撞「题库版本不一致」整屏 fail-closed(2026-08-25 实测)。
  itemBank: (week?: number) => req<ItemBankInfo>(
    "GET", week ? `/content/item-bank?week=${week}` : "/content/item-bank"),
  scaleProtocol: async () => parseScaleProtocolReadiness(await req<unknown>(
    "GET", "/content/scale-protocol", undefined,
    DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
  )),
  // 量表电子记录(原型道):定义含逐题词,只经这些认证接口下发,永不进前端构建产物。
  listQuestionnaireDefinitions: async (): Promise<QuestionnaireCatalogEntry[]> =>
    parseQuestionnaireCatalog(await req<unknown>(
      "GET", "/questionnaires/definitions", undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    )),
  listQuestionnaireRecords: async (patientId: string): Promise<QuestionnaireRecord[]> =>
    parseQuestionnaireRecordList(await req<unknown>(
      "GET", `/patients/${encodeURIComponent(patientId)}/questionnaire-records`,
      undefined, DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), { patientId }),
  createQuestionnaireRecord: async (
    patientId: string,
    body: { questionnaire_id: string; phase_label: QuestionnairePhaseLabel; note?: string },
  ): Promise<QuestionnaireRecord> =>
    parseQuestionnaireRecord(await req<unknown>(
      "POST", `/patients/${encodeURIComponent(patientId)}/questionnaire-records`, body,
    ), { patientId }),
  putQuestionnaireValues: async (
    record: QuestionnaireRecord,
    values: QuestionnaireValueWrite[],
  ): Promise<QuestionnaireRecord> =>
    parseQuestionnaireRecord(await req<unknown>(
      "PUT", `/questionnaire-records/${encodeURIComponent(record.record_id)}/values`,
      { values },
    ), { patientId: record.patient_id, recordId: record.record_id }),
  // AI 初评要等云端 LLM 一轮往返,超时对齐 probeProviderReadiness。
  generateQuestionnaireAiDraft: async (
    record: QuestionnaireRecord,
  ): Promise<QuestionnaireRecord> =>
    parseQuestionnaireRecord(await req<unknown>(
      "POST", `/questionnaire-records/${encodeURIComponent(record.record_id)}/ai-draft`,
      undefined, 75_000,
    ), { patientId: record.patient_id, recordId: record.record_id }),
  lockQuestionnaireRecord: async (
    record: QuestionnaireRecord,
  ): Promise<QuestionnaireRecord> =>
    parseQuestionnaireRecord(await req<unknown>(
      "POST", `/questionnaire-records/${encodeURIComponent(record.record_id)}/lock`,
    ), { patientId: record.patient_id, recordId: record.record_id }),
  listPatientAssessmentEvents: async (patientId: string): Promise<AssessmentEvent[]> =>
    parseAssessmentEventList(await req<unknown>(
      "GET", `/patients/${encodeURIComponent(patientId)}/assessment-events`, undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    ), { patientId }),
  getAssessmentEvent: async (
    eventId: string,
    patientId: string,
  ): Promise<AssessmentEvent> => parseAssessmentEvent(await req<unknown>(
    "GET", `/assessment-events/${encodeURIComponent(eventId)}`, undefined,
    DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
  ), { patientId, eventId }),
  listTodayAssessmentEvents: async (): Promise<AssessmentEventsToday> =>
    parseAssessmentEventsToday(await req<unknown>(
      "GET", "/assessment-events/today", undefined,
      DEFAULT_REQUEST_TIMEOUT_MS, { noStore: true },
    )),

  // 写契约先于任何 UI 入口存在，但每一个方法都在 fetch 前验证
  // v4 definitions/platform/workflow policy 事实。当前 v3 就绪响应因缺少这些事实会本地拒绝。
  createAssessmentEvent: async (
    patientId: string,
    body: AssessmentEventCreateRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "create");
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/patients/${encodeURIComponent(patientId)}/assessment-events`, {
        timepoint: body.timepoint,
        scheduled_date: body.scheduled_date,
        idempotency_key: body.idempotency_key,
      },
    ), { patientId, timepoint: body.timepoint });
  },
  startAssessmentEvent: async (
    event: AssessmentEvent,
    body: AssessmentStartRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "start");
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/assessment-events/${encodeURIComponent(event.event_id)}/start`, {
        expected_event_revision: body.expected_event_revision,
        idempotency_key: body.idempotency_key,
      },
    ), assessmentReceiptExpectation(event));
  },
  cancelAssessmentEvent: async (
    event: AssessmentEvent,
    body: AssessmentCancelRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "cancel");
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/assessment-events/${encodeURIComponent(event.event_id)}/cancel`, {
        expected_event_revision: body.expected_event_revision,
        idempotency_key: body.idempotency_key,
        reason_code: body.reason_code,
      },
    ), assessmentReceiptExpectation(event));
  },
  submitAssessmentItemResponse: async (
    event: AssessmentEvent,
    instance: AssessmentInstance,
    itemKey: string,
    body: AssessmentItemResponseRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "response");
    requireAssessmentInstanceTarget(event, instance);
    const response = {
      ...(Object.prototype.hasOwnProperty.call(body.response, "value")
        ? { value: body.response.value }
        : {}),
      ...(body.response.authorized_artifact_digest === undefined
        ? {}
        : { authorized_artifact_digest: body.response.authorized_artifact_digest }),
    };
    return parseAssessmentEvent(await req<unknown>(
      "PUT",
      `/assessment-instances/${encodeURIComponent(instance.instance_id)}/responses/${encodeURIComponent(itemKey)}`,
      {
        response,
        expected_event_revision: body.expected_event_revision,
        expected_instance_revision: body.expected_instance_revision,
        expected_item_revision: body.expected_item_revision,
        idempotency_key: body.idempotency_key,
      },
    ), assessmentReceiptExpectation(event));
  },
  issueAssessmentRecordingAuthorization: async (
    event: AssessmentEvent,
    instance: AssessmentInstance,
    itemKey: string,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<{ authorizedArtifactDigest: string; itemRevision: number }> => {
    assertFormalAssessmentMutationReady(readiness, "response");
    requireAssessmentInstanceTarget(event, instance);
    const raw = await req<unknown>(
      "POST",
      `/assessment-instances/${encodeURIComponent(instance.instance_id)}/recording-authorizations`,
      { item_key: itemKey },
    );
    const row = raw as Record<string, unknown>;
    const digest = row?.authorized_artifact_digest;
    const revision = row?.item_revision;
    if (typeof digest !== "string" || !/^sha256:[0-9a-f]{64}$/.test(digest)
        || typeof revision !== "number" || !Number.isInteger(revision) || revision < 1
        || row?.item_key !== itemKey) {
      throw new ApiError(0, "服务端录音授权回执格式无效");
    }
    return { authorizedArtifactDigest: digest, itemRevision: revision };
  },
  completeAssessmentInstance: async (
    event: AssessmentEvent,
    instance: AssessmentInstance,
    body: AssessmentInstanceMutationRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "complete");
    requireAssessmentInstanceTarget(event, instance);
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/assessment-instances/${encodeURIComponent(instance.instance_id)}/complete`, {
        expected_event_revision: body.expected_event_revision,
        expected_instance_revision: body.expected_instance_revision,
        idempotency_key: body.idempotency_key,
      },
    ), assessmentReceiptExpectation(event));
  },
  approveAssessmentDeferral: async (
    event: AssessmentEvent,
    instance: AssessmentInstance,
    body: AssessmentDeferralRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "approve_defer");
    requireAssessmentInstanceTarget(event, instance);
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/assessment-instances/${encodeURIComponent(instance.instance_id)}/approve-defer`, {
        expected_event_revision: body.expected_event_revision,
        expected_instance_revision: body.expected_instance_revision,
        idempotency_key: body.idempotency_key,
        reason_code: body.reason_code,
        deferred_until: body.deferred_until,
      },
    ), assessmentReceiptExpectation(event));
  },
  closeAssessmentEvent: async (
    event: AssessmentEvent,
    body: AssessmentCloseRequest,
    readiness: FormalAssessmentMutationReadiness,
  ): Promise<AssessmentEvent> => {
    assertFormalAssessmentMutationReady(readiness, "close");
    return parseAssessmentEvent(await req<unknown>(
      "POST", `/assessment-events/${encodeURIComponent(event.event_id)}/close`, {
        expected_event_revision: body.expected_event_revision,
        idempotency_key: body.idempotency_key,
        report_status: body.report_status,
        fatigue_observed: body.fatigue_observed,
        distress_or_discomfort_observed: body.distress_or_discomfort_observed,
        participant_declined_to_continue: body.participant_declined_to_continue,
        staff_assistance_occurred: body.staff_assistance_occurred,
        environment_interruption_occurred: body.environment_interruption_occurred,
        device_or_network_interruption_occurred: body.device_or_network_interruption_occurred,
        note: body.note,
      },
    ), assessmentReceiptExpectation(event));
  },
  // 老人端只取服务端当前游标已授权的一条线索/反馈/关系建立话术。
  // 返回 unknown 是刻意的：信任边界由 patient/presentationContent 严格解析，
  // 不把网络 JSON 盲转成可呈现内容。
  patientPresentation: (sid: string) => req<unknown>(
    "GET", `/sessions/${encodeURIComponent(sid)}/patient-presentation`,
    undefined, DEFAULT_REQUEST_TIMEOUT_MS, { device: true, deviceSessionId: sid },
  ),
  patientSessionPlan: (sid: string, weekNo: number, eventLine: string) => {
    const q = new URLSearchParams({ week_no: String(weekNo), event_line: eventLine });
    return req<unknown>(
      "GET", `/sessions/${encodeURIComponent(sid)}/patient-plan?${q}`,
      undefined, DEFAULT_REQUEST_TIMEOUT_MS,
      { device: true, deviceSessionId: sid },
    );
  },
  sessionPlan: (sid: string, weekNo: number, eventLine: string, maxItems?: number,
                opts?: { device?: boolean }) => {
    const q = new URLSearchParams({ week_no: String(weekNo), event_line: eventLine });
    if (maxItems != null) q.set("max_items", String(maxItems));
    return req<unknown>("GET", `/sessions/${encodeURIComponent(sid)}/plan?${q}`,
      undefined, DEFAULT_REQUEST_TIMEOUT_MS,
      opts?.device ? { ...opts, deviceSessionId: sid } : opts)
      .then(parseAccountSessionPlan);
  },

  // 逐环节采集
  createItem: (sid: string, body: { item_id: string; task_type: string; item_set_type?: string; image_id?: string | null }) =>
    req<ItemEvent>("POST", `/sessions/${encodeURIComponent(sid)}/items`, body),
  createTurn: (itemEventId: number, body: {
    turn_seq: number; response_role: string; raw_audio_id: string;
  }) => req<TurnEvent>("POST", `/items/${itemEventId}/turns`, body),
  processAttempt: (sid: string, body: AttemptProcessRequest) =>
    req<AttemptProcessResult>("POST", `/sessions/${encodeURIComponent(sid)}/attempts/process`, body, 60_000),
  appendInteraction: (sid: string, body: InteractionAppendRequest) =>
    req<InteractionEvent>("POST", `/sessions/${encodeURIComponent(sid)}/interactions`, body),
  commitInteractionPresentation: (
    sid: string,
    body: InteractionPresentationRequest,
    signal?: AbortSignal,
  ) =>
    // This route crosses a safety-critical presentation boundary.  Keep the
    // transport result unknown so the exact request-bound receipt parser at the
    // sole call site must prove every field before a bedside cursor is broadcast.
    req<unknown>(
      "POST", `/sessions/${encodeURIComponent(sid)}/interaction-presentations`, body,
      DEFAULT_REQUEST_TIMEOUT_MS, { signal }),
  commitTechnicalPause: async (
    sid: string,
    body: TechnicalPauseRequest,
    signal?: AbortSignal,
  ): Promise<TechnicalPauseReceipt> => {
    const receipt = await req<Omit<TechnicalPauseReceipt, "runtime"> & { runtime: unknown }>(
      "POST", `/sessions/${encodeURIComponent(sid)}/technical-pause`, body,
      DEFAULT_REQUEST_TIMEOUT_MS, { signal },
    );
    return {
      ...receipt,
      runtime: parseSessionRuntimeState(receipt.runtime, sid),
    };
  },
  confirmTurn: async (turnId: number, body: TurnConfirmationRequest) =>
    parseTurnConfirmationReceipt(
      await req<TurnEvent>("PATCH", `/turns/${turnId}/confirm`, body),
      turnId,
      body,
    ),
  lockTurn: (turnId: number, body: { element_value: number }) =>
    req<TurnEvent>("PATCH", `/turns/${turnId}/lock`, body),

  // 跨设备实时状态(内网双设备;同机双窗时 BroadcastChannel 仍是快路径)
  getLiveState: (timeoutMs = 5_000) =>
    req<LiveStateResponse>("GET", "/live/state", undefined, timeoutMs, { device: true }),
  // 研究者端专用投影；与患者端读取路由分离，旧后端缺此端点时明确进入不可用状态。
  getConsoleState: (timeoutMs = 5_000) =>
    req<LiveStateResponse>("GET", "/live/console-state", undefined, timeoutMs),
  putLiveState: (
    kind: "session" | "cursor" | "rapportStep" | "audioSaved" | "patientRec",
    payload: object,
    signal?: AbortSignal,
  ) =>
    req<{ seq: number; wseq?: number; audioReceipt?: { serverSeq: number; rawAudioId: string; idempotent: boolean } }>(
      "PUT", "/live/state", { kind, payload }, DEFAULT_REQUEST_TIMEOUT_MS,
      {
        device: kind === "audioSaved" || kind === "patientRec",
        deviceSessionId: payloadSessionId(payload),
        allowRecovery: kind === "audioSaved",
        signal,
      }),
  putAudioSaved: (payload: object) => req<{
    seq: number;
    audioReceipt: { serverSeq: number; rawAudioId: string; idempotent: boolean };
  }>("PUT", "/live/state", { kind: "audioSaved", payload }, DEFAULT_REQUEST_TIMEOUT_MS,
    { device: true, deviceSessionId: payloadSessionId(payload), allowRecovery: true }),
  // 本地副本删除回执(收据 144):payload=410 detail 原样回显。best-effort,
  // 调用方吞错;与 audioSaved 同走 allowRecovery(410 常到达于场次离开 LiveState 后)。
  putAudioDisposalConfirmed: (payload: object) => req<{
    code: string; rawAudioId: string; duplicate: boolean;
  }>("PUT", "/live/state", { kind: "audioDisposalConfirmed", payload },
    3_500,
    { device: true, deviceSessionId: payloadSessionId(payload),
      allowRecovery: true, silentDeviceAuthFailure: true }),
  // 老人端最小在场上报；服务端校验 session 与 screen，且只用服务器时间判在线。
  patientHeartbeat: (body: PatientHeartbeatRequest) =>
    req<PatientHeartbeatResponse>("POST", "/live/patient-heartbeat", body,
      DEFAULT_REQUEST_TIMEOUT_MS, { device: true, deviceSessionId: body.session_id }),

  // 前后测量表(scale_result 容器)
  createScale: (patientId: string, body: { phase_type: string; scale_name: string; subscale?: string | null; score?: number | null; assessor_id?: string | null }) =>
    req<ScaleResult>("POST", `/patients/${encodeURIComponent(patientId)}/scales`, body),
  listScales: (patientId: string) => req<ScaleResult[]>("GET", `/patients/${encodeURIComponent(patientId)}/scales`),

  // 异常 / 评分 / 导出
  recordAbnormal: (sid: string, body: { item_event_id?: number | null; abnormal_type?: string | null; intervention_type?: string | null; affects_scoring_validity?: boolean; note?: string | null }) =>
    req<AbnormalEvent>("POST", `/sessions/${encodeURIComponent(sid)}/abnormal`, body),
  sessionScores: (sid: string) => req<ScoreReconstruction>("GET", `/sessions/${encodeURIComponent(sid)}/scores`),
  exportSession: (sid: string, idempotencyKey: string, deidentify = true) =>
    req<ExportResult>("POST", `/sessions/${encodeURIComponent(sid)}/export?deidentify=${deidentify}`,
      { idempotency_key: idempotencyKey }, 60_000),
  exportBatch: (batchId: string) =>
    req<ExportResult>("GET", `/exports/${encodeURIComponent(batchId)}`),

  // 音频闸门
  createAudio: (body: { raw_audio_id: string; turn_key: string; session_id?: string | null; is_reliability_sample?: boolean; contains_direct_identifier?: boolean }) =>
    req<{ raw_audio_id: string; registered: true }>("POST", "/audio", body, DEFAULT_REQUEST_TIMEOUT_MS, {
      device: true,
      deviceSessionId: body.session_id ?? undefined,
      allowRecovery: true,
    }),
  audioExport: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/export`),
  audioChecksum: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/checksum`),
  audioReliabilityReview: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/reliability-review`),
  getAudio: (id: string) => req<AudioAsset>("GET", `/audio/${encodeURIComponent(id)}`),
  // 音频原件先落自有服务器；老人端录完即传，checksum 闸门据此真校验。
  // 若随后调用云 ASR，后端会把该音频另行交第三方处理。
  uploadAudioBlob: async (id: string, blob: Blob, sessionId: string) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    try {
      const credential = selectDeviceCredential(sessionId, true);
      const res = await fetch(`/audio/${encodeURIComponent(id)}/blob`, {
        method: "PUT", body: blob, signal: controller.signal,
        credentials: "omit",
        cache: "no-store",
        headers: {
          "Content-Type": blob.type || "audio/webm",
          ...credential.headers,
          ...csrfHeader("PUT"),
        },
      });
      const text = await res.text();
      if (!res.ok) handleDeviceAuthorizationFailure(res.status, text, credential);
      return decodeJsonApiResponse({ status: res.status, ok: res.ok, statusText: res.statusText, text }) as {
        raw_audio_id: string; bytes: number; checksum: string; format: string;
      };
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError(408, "音频上传超时，本机副本已保留，请检查连接后重试");
      throw apiNetworkError(error);
    } finally {
      window.clearTimeout(timeout);
    }
  },
  // 原始声纹 GET 在启用 PIN 时受保护；原生 <audio src> 无法携带自定义请求头，
  // 因此先通过受控 fetch 取回 Blob，再用临时 object URL 回放。
  getAudioBlob: async (id: string): Promise<Blob> => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12_000);
    try {
      const res = await fetch(`/audio/${encodeURIComponent(id)}/blob`, {
        method: "GET", signal: controller.signal, credentials: "same-origin",
      });
      if (!res.ok) {
        if (res.status === 401) window.dispatchEvent(new Event(ACCOUNT_REAUTH_REQUIRED_EVENT));
        const text = await res.text();
        decodeJsonApiResponse({ status: res.status, ok: false, statusText: res.statusText, text });
      }
      return await res.blob();
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError(408, "录音回放载入超时，请检查本机服务连接后重试");
      throw apiNetworkError(error);
    } finally {
      window.clearTimeout(timeout);
    }
  },
  // 操作端收尾屏的删除都是人工发起,审计口径应为 manual(非到期 auto)。
  deleteAudio: (id: string, sessionId: string, source: "manual" | "withdrawal" = "manual") => {
    const query = new URLSearchParams({ source, session_id: sessionId });
    return req<{ raw_audio_id: string; status: string; deleted_by: string }>(
      "DELETE", `/audio/${encodeURIComponent(id)}?${query}`,
    );
  },
};
