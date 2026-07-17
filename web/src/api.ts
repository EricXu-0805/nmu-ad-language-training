// 强类型 API 客户端——逐个后端路由一个函数。相对路径:dev 走 Vite 代理、生产由 FastAPI 同源托管。
// 全本地、无外部请求。任何非 2xx 抛 ApiError,带后端 detail,供 UI 明确报错(不静默失败)。
import type {
  AbnormalEvent, AudioAsset, ExportResult, ItemBankInfo, ItemEvent,
  LiveStateResponse, Patient, PatientHeartbeatRequest, PatientHeartbeatResponse,
  PatientSummary, ScaleResult, ScoreReconstruction, Session, SessionPlan, SessionRuntimeState, TurnEvent,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(`[${status}] ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// 操作端 PIN(内网双设备模式,后端设 CONSOLE_PIN 才启用)。两端各输一次,存本机。
const PIN_KEY = "nmu:pin";
export const getPin = (): string | null => { try { return localStorage.getItem(PIN_KEY); } catch { return null; } };
export const PIN_REQUIRED_EVENT = "nmu:pin-required";
export const PIN_UPDATED_EVENT = "nmu:pin-updated";
export const setPin = (p: string): void => {
  localStorage.setItem(PIN_KEY, p);
  window.dispatchEvent(new Event(PIN_UPDATED_EVENT));
};

function pinHeader(): Record<string, string> {
  const p = getPin();
  return p ? { "X-Console-Pin": p } : {};
}

const DEFAULT_REQUEST_TIMEOUT_MS = 12_000;

async function req<T>(method: string, path: string, body?: unknown, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, {
      method,
      signal: controller.signal,
      headers: {
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...pinHeader(),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new Event(PIN_REQUIRED_EVENT));
      const detail = data && typeof data === "object" && "detail" in data ? String(data.detail) : text;
      throw new ApiError(res.status, detail);
    }
    return data as T;
  } catch (error) {
    if (controller.signal.aborted) throw new ApiError(408, "请求超时，请检查本机服务连接后重试");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export const api = {
  health: () => req<{ status: string; service: string }>("GET", "/health"),

  // 患者 / 场次
  createPatient: (p: Patient) => req<Patient>("POST", "/patients", p),
  getPatient: (id: string) => req<Patient>("GET", `/patients/${encodeURIComponent(id)}`),
  listPatients: () => req<PatientSummary[]>("GET", "/patients"),
  patientSessions: (id: string) => req<Session[]>("GET", `/patients/${encodeURIComponent(id)}/sessions`),
  createSession: (s: Session) => req<Session>("POST", "/sessions", s),
  getTrainSession: (sid: string) => req<Session>("GET", `/sessions/${encodeURIComponent(sid)}`),
  sessionJournal: (sid: string) => req<{
    session: Session; items: ItemEvent[]; turns: TurnEvent[]; audios: AudioAsset[]; abnormal: AbnormalEvent[];
  }>("GET", `/sessions/${encodeURIComponent(sid)}/journal`),
  getSessionRuntime: (sid: string) =>
    req<SessionRuntimeState>("GET", `/sessions/${encodeURIComponent(sid)}/runtime`),
  pauseSession: (sid: string) =>
    req<SessionRuntimeState>("POST", `/sessions/${encodeURIComponent(sid)}/pause`),
  resumeSession: (sid: string) =>
    req<SessionRuntimeState>("POST", `/sessions/${encodeURIComponent(sid)}/resume`),

  // 内容 / 计划
  itemBank: () => req<ItemBankInfo>("GET", "/content/item-bank"),
  sessionPlan: (sid: string, weekNo: number, eventLine: string, maxItems?: number) => {
    const q = new URLSearchParams({ week_no: String(weekNo), event_line: eventLine });
    if (maxItems != null) q.set("max_items", String(maxItems));
    return req<SessionPlan>("GET", `/sessions/${encodeURIComponent(sid)}/plan?${q}`);
  },

  // 逐环节采集
  createItem: (sid: string, body: { item_id: string; task_type: string; item_set_type?: string; image_id?: string | null }) =>
    req<ItemEvent>("POST", `/sessions/${encodeURIComponent(sid)}/items`, body),
  createTurn: (itemEventId: number, body: {
    turn_seq: number; response_role?: string | null; raw_audio_id?: string | null;
    asr_text?: string | null; asr_confidence?: number | null; prompt_level?: number | null; duration_seconds?: number | null;
  }) => req<TurnEvent>("POST", `/items/${itemEventId}/turns`, body),
  confirmTurn: (turnId: number, confirmed_response_text: string) =>
    req<TurnEvent>("PATCH", `/turns/${turnId}/confirm`, { confirmed_response_text }),
  aiJudgeTurn: (turnId: number) => req<TurnEvent>("POST", `/turns/${turnId}/ai-judge`),
  // 自动驾驶逐轮判类:只读,不建 turn、不写库、永不锁分
  judgeClassify: (item_id: string, response_role: string, text: string | null) =>
    req<{ answer_type: string | null; ai_score: number | null; needs_review: boolean; judge_mode: string; contains_target: boolean }>(
      "POST", "/judge/classify", { item_id, response_role, text }, 30_000),
  lockTurn: (turnId: number, body: { reviewer_id: string; element_value: number; reviewed_score?: number | null; prompt_level?: number | null }) =>
    req<TurnEvent>("PATCH", `/turns/${turnId}/lock`, body),

  // 跨设备实时状态(内网双设备;同机双窗时 BroadcastChannel 仍是快路径)
  getLiveState: (timeoutMs = 5_000) =>
    req<LiveStateResponse>("GET", "/live/state", undefined, timeoutMs),
  // 研究者端专用投影；与患者端读取路由分离，旧后端缺此端点时明确进入不可用状态。
  getConsoleState: (timeoutMs = 5_000) =>
    req<LiveStateResponse>("GET", "/live/console-state", undefined, timeoutMs),
  putLiveState: (kind: "session" | "cursor" | "rapportStep" | "audioSaved" | "patientRec", payload: object) =>
    req<{ seq: number; wseq?: number }>("PUT", "/live/state", { kind, payload }),
  // 老人端最小在场上报；服务端校验 session 与 screen，且只用服务器时间判在线。
  patientHeartbeat: (body: PatientHeartbeatRequest) =>
    req<PatientHeartbeatResponse>("POST", "/live/patient-heartbeat", body),

  // 前后测量表(scale_result 容器)
  createScale: (patientId: string, body: { phase_type: string; scale_name: string; subscale?: string | null; score?: number | null; assessor_id?: string | null }) =>
    req<ScaleResult>("POST", `/patients/${encodeURIComponent(patientId)}/scales`, body),
  listScales: (patientId: string) => req<ScaleResult[]>("GET", `/patients/${encodeURIComponent(patientId)}/scales`),

  // 异常 / 评分 / 导出
  recordAbnormal: (sid: string, body: { item_event_id?: number | null; abnormal_type?: string | null; intervention_type?: string | null; affects_scoring_validity?: boolean; note?: string | null }) =>
    req<AbnormalEvent>("POST", `/sessions/${encodeURIComponent(sid)}/abnormal`, body),
  sessionScores: (sid: string) => req<ScoreReconstruction>("GET", `/sessions/${encodeURIComponent(sid)}/scores`),
  exportSession: (sid: string, deidentify = true) =>
    req<ExportResult>("POST", `/sessions/${encodeURIComponent(sid)}/export?deidentify=${deidentify}`, undefined, 60_000),

  // 音频闸门
  createAudio: (body: { raw_audio_id: string; turn_key: string; session_id?: string | null; is_reliability_sample?: boolean; contains_direct_identifier?: boolean }) =>
    req<AudioAsset>("POST", "/audio", body),
  audioExport: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/export`),
  audioChecksum: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/checksum`),
  audioReliabilityReview: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/reliability-review`),
  getAudio: (id: string) => req<AudioAsset>("GET", `/audio/${encodeURIComponent(id)}`),
  // 音频字节落库(本机磁盘,不上云);老人端录完即传,checksum 闸门据此真校验。
  uploadAudioBlob: async (id: string, blob: Blob) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    try {
      const res = await fetch(`/audio/${encodeURIComponent(id)}/blob`, {
        method: "PUT", body: blob, signal: controller.signal,
        headers: { "Content-Type": blob.type || "audio/webm", ...pinHeader() },
      });
      if (!res.ok) {
        if (res.status === 401) window.dispatchEvent(new Event(PIN_REQUIRED_EVENT));
        throw new ApiError(res.status, await res.text());
      }
      return res.json() as Promise<{ raw_audio_id: string; bytes: number; checksum: string; format: string }>;
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError(408, "音频上传超时，本机副本已保留，请检查连接后重试");
      throw error;
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
        method: "GET", signal: controller.signal, headers: pinHeader(),
      });
      if (!res.ok) {
        if (res.status === 401) window.dispatchEvent(new Event(PIN_REQUIRED_EVENT));
        throw new ApiError(res.status, await res.text());
      }
      return await res.blob();
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError(408, "录音回放载入超时，请检查本机服务连接后重试");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  },
  // 本地 ASR(M0=Null 引擎:degraded=true → 人工转写)
  asrTranscribe: (id: string) =>
    req<{ raw_audio_id: string; asr_text: string | null; asr_confidence: number | null; engine_version: string; degraded: boolean }>(
      "POST", `/asr/transcribe/${encodeURIComponent(id)}`, undefined, 60_000),
  // 操作端收尾屏的删除都是人工发起,审计口径应为 manual(非到期 auto)。
  deleteAudio: (id: string, source: "manual" | "withdrawal" = "manual") =>
    req<{ raw_audio_id: string; status: string; deleted_by: string }>("DELETE", `/audio/${encodeURIComponent(id)}?source=${source}`),
};
