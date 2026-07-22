import type {
  TechnicalPauseDraft,
  TechnicalPauseRequest,
} from "../api.ts";
import type { SessionRuntimeState } from "../types.ts";

const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$/;
const ERROR_CODE = /^[a-z0-9._-]{1,64}$/;

export function buildExactTechnicalPauseRequest(
  draft: TechnicalPauseDraft,
  runtime: SessionRuntimeState,
  expectedSessionId: string,
): TechnicalPauseRequest {
  const wseq = runtime.cursor?.wseq;
  if (!IDEMPOTENCY_KEY.test(draft.idempotency_key)) {
    throw new Error("原子技术暂停幂等键无效");
  }
  if (!ERROR_CODE.test(draft.error_code)) {
    throw new Error("原子技术暂停错误代码无效");
  }
  if (runtime.sessionId !== expectedSessionId) {
    throw new Error("原子技术暂停快照不属于当前场次");
  }
  if (runtime.status !== "active") {
    throw new Error(`场次状态为 ${runtime.status}，拒绝构造新技术暂停事实`);
  }
  if (!Number.isSafeInteger(runtime.revision) || runtime.revision < 0
      || !Number.isSafeInteger(wseq) || (wseq as number) < 0) {
    throw new Error("当前场次缺少可用于原子技术暂停的 revision/wseq 真值");
  }
  if (draft.attempt_id !== undefined
      && (!Number.isSafeInteger(draft.attempt_id) || draft.attempt_id < 1)) {
    throw new Error("原子技术暂停 attempt 引用无效");
  }
  return {
    ...draft,
    expected_revision: runtime.revision,
    expected_live_wseq: wseq as number,
  };
}

export function exactTechnicalPauseRequestMatchesDraft(
  exact: TechnicalPauseRequest,
  draft: TechnicalPauseDraft,
): boolean {
  const {
    expected_revision: _revision,
    expected_live_wseq: _wseq,
    ...requestDraft
  } = exact;
  return JSON.stringify(requestDraft) === JSON.stringify(draft);
}

export function technicalPauseDispositionIsUncertain(error: unknown): boolean {
  if (error === null || typeof error !== "object") return true;
  const status = (error as { status?: unknown }).status;
  return typeof status !== "number" || status === 0 || status === 408 || status >= 500;
}

function structuredErrorCode(error: unknown): string | null {
  if (error === null || typeof error !== "object") return null;
  const detail = (error as { detailData?: unknown }).detailData;
  if (detail !== null && typeof detail === "object" && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === "string") return code;
  }
  return null;
}

export async function reconcilePendingTechnicalPauseForTakeover(
  runtimeStatus: string | undefined,
  pending: TechnicalPauseRequest | null,
  replay: (request: TechnicalPauseRequest) => Promise<unknown>,
): Promise<{
  ready: boolean;
  pending: TechnicalPauseRequest | null;
  error?: unknown;
}> {
  if (runtimeStatus !== "paused") return { ready: false, pending };
  if (!pending) return { ready: true, pending: null };
  try {
    await replay(pending);
    return { ready: true, pending: null };
  } catch (error) {
    // This code is returned only after the server found the exact durable
    // receipt for this key/hash and proved a later state replaced it. Evidence
    // therefore exists even though its old paused cursor must not be replayed.
    if (structuredErrorCode(error) === "technical_pause_replay_superseded") {
      return { ready: true, pending: null };
    }
    return { ready: false, pending, error };
  }
}
