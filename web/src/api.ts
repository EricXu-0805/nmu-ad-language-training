// 强类型 API 客户端——逐个后端路由一个函数。相对路径:dev 走 Vite 代理、生产由 FastAPI 同源托管。
// 全本地、无外部请求。任何非 2xx 抛 ApiError,带后端 detail,供 UI 明确报错(不静默失败)。
import type {
  AbnormalEvent, AudioAsset, ExportResult, ItemBankInfo, ItemEvent,
  Patient, ScaleResult, ScoreReconstruction, Session, SessionPlan, TurnEvent,
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

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? String(data.detail) : text;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  health: () => req<{ status: string; service: string }>("GET", "/health"),

  // 患者 / 场次
  createPatient: (p: Patient) => req<Patient>("POST", "/patients", p),
  getPatient: (id: string) => req<Patient>("GET", `/patients/${encodeURIComponent(id)}`),
  createSession: (s: Session) => req<Session>("POST", "/sessions", s),

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
  lockTurn: (turnId: number, body: { reviewer_id: string; element_value: number; reviewed_score?: number | null; prompt_level?: number | null }) =>
    req<TurnEvent>("PATCH", `/turns/${turnId}/lock`, body),

  // 跨设备实时状态(内网双设备;同机双窗时 BroadcastChannel 仍是快路径)
  getLiveState: () =>
    req<{ seq: number; session: unknown; cursor: unknown; rapportStep: unknown; audioSaved: unknown }>("GET", "/live/state"),
  putLiveState: (kind: "session" | "cursor" | "rapportStep" | "audioSaved", payload: object) =>
    req<{ seq: number }>("PUT", "/live/state", { kind, payload }),

  // 前后测量表(scale_result 容器)
  createScale: (patientId: string, body: { phase_type: string; scale_name: string; subscale?: string | null; score?: number | null; assessor_id?: string | null }) =>
    req<ScaleResult>("POST", `/patients/${encodeURIComponent(patientId)}/scales`, body),
  listScales: (patientId: string) => req<ScaleResult[]>("GET", `/patients/${encodeURIComponent(patientId)}/scales`),

  // 异常 / 评分 / 导出
  recordAbnormal: (sid: string, body: { item_event_id?: number | null; abnormal_type?: string | null; intervention_type?: string | null; affects_scoring_validity?: boolean; note?: string | null }) =>
    req<AbnormalEvent>("POST", `/sessions/${encodeURIComponent(sid)}/abnormal`, body),
  sessionScores: (sid: string) => req<ScoreReconstruction>("GET", `/sessions/${encodeURIComponent(sid)}/scores`),
  exportSession: (sid: string, deidentify = true) =>
    req<ExportResult>("POST", `/sessions/${encodeURIComponent(sid)}/export?deidentify=${deidentify}`),

  // 音频闸门
  createAudio: (body: { raw_audio_id: string; session_id?: string | null; is_reliability_sample?: boolean; contains_direct_identifier?: boolean }) =>
    req<AudioAsset>("POST", "/audio", body),
  audioExport: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/export`),
  audioChecksum: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/checksum`),
  audioReliabilityReview: (id: string) => req<AudioAsset>("POST", `/audio/${encodeURIComponent(id)}/reliability-review`),
  getAudio: (id: string) => req<AudioAsset>("GET", `/audio/${encodeURIComponent(id)}`),
  // 音频字节落库(本机磁盘,不上云);老人端录完即传,checksum 闸门据此真校验。
  uploadAudioBlob: async (id: string, blob: Blob) => {
    const res = await fetch(`/audio/${encodeURIComponent(id)}/blob`, {
      method: "PUT", body: blob,
      headers: { "Content-Type": blob.type || "audio/webm" },
    });
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return res.json() as Promise<{ raw_audio_id: string; bytes: number; checksum: string; format: string }>;
  },
  audioBlobUrl: (id: string) => `/audio/${encodeURIComponent(id)}/blob`,
  // 本地 ASR(M0=Null 引擎:degraded=true → 人工转写)
  asrTranscribe: (id: string) =>
    req<{ raw_audio_id: string; asr_text: string | null; asr_confidence: number | null; engine_version: string; degraded: boolean }>(
      "POST", `/asr/transcribe/${encodeURIComponent(id)}`),
  // 操作端收尾屏的删除都是人工发起,审计口径应为 manual(非到期 auto)。
  deleteAudio: (id: string, source: "manual" | "withdrawal" = "manual") =>
    req<{ raw_audio_id: string; status: string; deleted_by: string }>("DELETE", `/audio/${encodeURIComponent(id)}?source=${source}`),
};
