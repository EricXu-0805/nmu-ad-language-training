// 开麦前尝试恢复其他场次的持久录音，沿用登记 → 上传 → audioSaved 的精确回执链。
// 恢复失败的副本继续保留；执行器核对本机字节与归属后，可允许不同场次使用新 ID。
// 删除仍须权威保存收据或精确 410 处置，上传拒绝本身从不授权清理。
import { ApiError } from "../apiResponse.ts";
import type { AudioOutboxEntry } from "./audioOutbox.ts";
import { advanceAudioOutbox } from "./audioOutbox.ts";
import {
  completeAudioOutboxAfterServerAck, sha256Blob, validateAudioUploadReceipt,
} from "./audioUploadReceipt.ts";
import {
  buildAudioSavedPayload, finalizeAudioSavedRequest, type AudioTerminalDisposition,
} from "./audioTerminalDisposition.ts";

export interface ForeignOutboxDrainDeps {
  readBlob(rawAudioId: string): Promise<Blob | undefined>;
  createAudio(entry: AudioOutboxEntry): Promise<unknown>;
  uploadAudioBlob(rawAudioId: string, blob: Blob, sessionId: string): Promise<unknown>;
  putOutbox(entry: AudioOutboxEntry): Promise<void>;
  putAudioSaved(saved: object): Promise<unknown>;
  completeOutbox(rawAudioId: string): Promise<void>;
  discardTerminalOutbox(entry: AudioOutboxEntry, disposition: AudioTerminalDisposition): Promise<void>;
  reportLocalCopyDisposal(disposition: AudioTerminalDisposition): Promise<void>;
}

export type ForeignOutboxDrainResult = {
  rawAudioId: string;
  outcome: "server-saved" | "terminal-discarded" | "kept";
  reason?: string;
};

function isConflict(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

// 同一条录音同时只清一次:开麦前的软预算到点后这次清理还在后台继续跑,下一次
// 开麦(研究者点「继续」)再来清同一条时接上它,而不是并发第二次上传。
const inFlight = new Map<string, Promise<ForeignOutboxDrainResult>>();

/** 单条:与 useVoxRecorder.commitPending 同序;任何拿不准的错误都保留条目。 */
export function drainForeignOutboxEntry(
  entry: AudioOutboxEntry,
  deps: ForeignOutboxDrainDeps,
): Promise<ForeignOutboxDrainResult> {
  const running = inFlight.get(entry.rawAudioId);
  if (running) return running;
  const task = drainOne(entry, deps).finally(() => { inFlight.delete(entry.rawAudioId); });
  inFlight.set(entry.rawAudioId, task);
  return task;
}

async function drainOne(
  entry: AudioOutboxEntry,
  deps: ForeignOutboxDrainDeps,
): Promise<ForeignOutboxDrainResult> {
  const rawAudioId = entry.rawAudioId;
  let current = entry;
  let checksum = current.checksum ?? null;
  let probeMetadata = false;
  try {
    const blob = await deps.readBlob(rawAudioId);
    if (!blob) return { rawAudioId, outcome: "kept", reason: "missing-blob" };
    if (blob.size !== entry.blobBytes || (blob.type || "audio/webm") !== entry.mimeType) {
      return { rawAudioId, outcome: "kept", reason: "blob-mismatch" };
    }
    if (current.phase === "captured") {
      try {
        await deps.createAudio(current);
        current = advanceAudioOutbox(current, "registered");
        await deps.putOutbox(current);
      } catch (error) {
        if (!isConflict(error)) return { rawAudioId, outcome: "kept", reason: "register-failed" };
        probeMetadata = true;
      }
    }
    if (!probeMetadata && current.phase === "registered") {
      const localChecksum = await sha256Blob(blob);
      try {
        const receipt = await deps.uploadAudioBlob(rawAudioId, blob, current.sessionId);
        validateAudioUploadReceipt(receipt, { rawAudioId, bytes: blob.size, checksum: localChecksum });
        current = advanceAudioOutbox(current, "uploaded", { checksum: localChecksum });
        await deps.putOutbox(current);
        checksum = localChecksum;
      } catch (error) {
        // The upload endpoint conceals assets bound to an older session with 404.
        // Ask the existing metadata endpoint for an exact outcome; neither 404
        // nor 409 authorizes local deletion without its validated receipt/410.
        if (!isConflict(error) && !(error instanceof ApiError && error.status === 404)) {
          return { rawAudioId, outcome: "kept", reason: "upload-failed" };
        }
        probeMetadata = true;
        checksum = localChecksum;
      }
    }
    if (!checksum) checksum = await sha256Blob(blob);
    const saved = buildAudioSavedPayload(current, checksum);
    const outcome = await finalizeAudioSavedRequest(
      { entry: current, blob, saved, broadcastSaved: false },
      {
        putAudioSaved: (payload) => deps.putAudioSaved(payload),
        // 2xx 不够,只有服务端账本收据(audioReceipt)才允许删本机副本——与录音器同一条规则。
        completeServerSaved: (response) => completeAudioOutboxAfterServerAck(
          response, rawAudioId, () => deps.completeOutbox(rawAudioId)).then(() => undefined),
        discardTerminalOutbox: (disposition) => deps.discardTerminalOutbox(current, disposition),
        broadcastAudioSaved: () => { /* 别的场次的收据不广播到当前控制台 */ },
        reportLocalCopyDisposal: (disposition) => deps.reportLocalCopyDisposal(disposition),
      },
    );
    return { rawAudioId, outcome: outcome.kind };
  } catch (error) {
    return { rawAudioId, outcome: "kept", reason: error instanceof ApiError ? `http-${error.status}` : "error" };
  }
}

/**
 * 只清**别的场次**的条目;当前场次的由自动带练自己的恢复逻辑按命令核对。
 *
 * attemptedThisSession(rawAudioId → 当时的 currentSessionId):同一场里每条只试一次,
 * 不论结局(409 拒收、404 后 audioSaved 被拒、网络失败)都不再重试,后台还在跑的那次也不再等。
 * 2026-11-26 生产 4 条 409 每题重传,开录 ~1.7 s → ~5 s;2026-11-27 一条上传 404、audioSaved 被拒,
 * 只认 409 的记忆漏掉它,前 9 题开录各等满软预算 8 s。换到别的场次或刷新页面后照常重试;
 * 本机副本的去留规则不变(只认收据或精确 410)。
 */
export async function drainForeignOutboxEntries(
  entries: readonly AudioOutboxEntry[],
  currentSessionId: string,
  deps: ForeignOutboxDrainDeps,
  attemptedThisSession?: Map<string, string>,
): Promise<ForeignOutboxDrainResult[]> {
  const results: ForeignOutboxDrainResult[] = [];
  for (const entry of entries) {
    if (entry.sessionId === currentSessionId) continue;
    if (attemptedThisSession?.get(entry.rawAudioId) === currentSessionId) {
      results.push({ rawAudioId: entry.rawAudioId, outcome: "kept", reason: "attempted-this-session" });
      continue;
    }
    attemptedThisSession?.set(entry.rawAudioId, currentSessionId);
    results.push(await drainForeignOutboxEntry(entry, deps));
  }
  return results;
}
