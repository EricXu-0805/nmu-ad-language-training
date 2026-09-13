// 自动带练开麦前的「清旧账」:outbox 里若还留着**别的场次**没传完的录音,老人端会
// 拒绝开新麦(fail-closed,7 月的规则)。2026-09-13 演示就卡在这里:9/6 那场的录音
// 永远清不掉。服务端已对「同一受试者、同一台平板、没字节事实」的旧场次录音槽给
// 410 作废(收据 255),但自动带练模式本来不会去碰这些条目——这里把它们按录音器
// 同一条链(登记 → 上传 → audioSaved → 2xx 完成 / 410 作废)走一遍,能清多少清多少;
// 清不掉的照旧留着让执行器拒绝开麦,绝不猜、绝不删。
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
  let probeAfterConflict = false;
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
        probeAfterConflict = true;
      }
    }
    if (!probeAfterConflict && current.phase === "registered") {
      const localChecksum = await sha256Blob(blob);
      try {
        const receipt = await deps.uploadAudioBlob(rawAudioId, blob, current.sessionId);
        validateAudioUploadReceipt(receipt, { rawAudioId, bytes: blob.size, checksum: localChecksum });
        current = advanceAudioOutbox(current, "uploaded", { checksum: localChecksum });
        await deps.putOutbox(current);
        checksum = localChecksum;
      } catch (error) {
        if (!isConflict(error)) return { rawAudioId, outcome: "kept", reason: "upload-failed" };
        probeAfterConflict = true;
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

/** 只清**别的场次**的条目;当前场次的由自动带练自己的恢复逻辑按命令核对。 */
export async function drainForeignOutboxEntries(
  entries: readonly AudioOutboxEntry[],
  currentSessionId: string,
  deps: ForeignOutboxDrainDeps,
): Promise<ForeignOutboxDrainResult[]> {
  const results: ForeignOutboxDrainResult[] = [];
  for (const entry of entries) {
    if (entry.sessionId === currentSessionId) continue;
    results.push(await drainForeignOutboxEntry(entry, deps));
  }
  return results;
}
