export class ApiError extends Error {
  status: number;
  detail: string;
  detailData: unknown;
  detailEnvelope: "direct" | "nested-detail" | "noncanonical-json" | "non-json";

  constructor(
    status: number,
    detail: string,
    detailData?: unknown,
    detailEnvelope: ApiError["detailEnvelope"] = "direct",
  ) {
    super(`[${status}] ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.detailData = detailData ?? detail;
    this.detailEnvelope = detailEnvelope;
  }
}

function plainResponseText(value: string): string {
  return value
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 500);
}

function readableApiDetail(value: unknown, fallback: string): string {
  if (typeof value === "string") {
    const text = plainResponseText(value);
    if (text) return text;
  }
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const message = (value as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message.trim();
    try { return JSON.stringify(value); }
    catch { /* use fallback */ }
  }
  return fallback || "请求失败";
}

export interface ApiResponsePayload {
  status: number;
  ok: boolean;
  statusText?: string;
  text: string;
}

// fetch Response 先读成文本再交这里处理。错误页可能来自 Caddy/代理，不能让
// JSON.parse 的 SyntaxError 越过 ApiError 边界，也不能把整页 HTML 塞进界面。
export function decodeJsonApiResponse({ status, ok, statusText = "", text }: ApiResponsePayload): unknown {
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text) as unknown;
    } catch {
      if (!ok) {
        const fallback = statusText.trim() || `请求失败（HTTP ${status}）`;
        throw new ApiError(status, readableApiDetail(text, fallback), text, "non-json");
      }
      throw new ApiError(status, "服务器返回格式错误，请重试或联系管理员", text, "non-json");
    }
  }

  if (!ok) {
    const objectData = data !== null && typeof data === "object" && !Array.isArray(data)
      ? data as Record<string, unknown>
      : null;
    const hasNestedDetail = objectData !== null && Object.hasOwn(objectData, "detail");
    const canonicalNestedDetail = hasNestedDetail
      && Object.keys(objectData as Record<string, unknown>).length === 1;
    const detailData = hasNestedDetail ? objectData?.detail : data;
    const fallback = plainResponseText(text) || statusText.trim() || `请求失败（HTTP ${status}）`;
    throw new ApiError(
      status,
      readableApiDetail(detailData, fallback),
      detailData,
      canonicalNestedDetail ? "nested-detail" : "noncanonical-json",
    );
  }
  return data;
}

export function apiNetworkError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError(0, "无法连接服务器，请检查网络或服务状态后重试", error);
}
