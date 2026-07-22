import { api, ApiError } from "../api";
import { blobStore } from "./blobStore";
import { serverConfirmsUploadedAudio } from "./audioCachePolicy";
import { AudioSweepPreservationRegistry } from "./audioSweepPreservation";

const activePreservation = new AudioSweepPreservationRegistry();

async function runSweep(): Promise<{ deleted: number; preserved: number }> {
  let keys: string[];
  try { keys = await blobStore.keys(); }
  catch { return { deleted: 0, preserved: 0 }; }
  let deleted = 0;
  let preserved = 0;
  for (const rawAudioId of keys) {
    // 新版可靠 outbox 自己负责续传/清场；旧缓存清扫不得抢先删掉它仍要重传的 blob。
    if (activePreservation.isPreserved(rawAudioId)) {
      preserved += 1;
      continue;
    }
    try {
      const asset = await api.getAudio(rawAudioId);
      if (!serverConfirmsUploadedAudio(rawAudioId, asset)) {
        preserved += 1;
        continue;
      }
      // A concurrent caller may have claimed this ID while the server request
      // was in flight. Recheck immediately before opening the delete transaction.
      if (activePreservation.isPreserved(rawAudioId)) {
        preserved += 1;
        continue;
      }
      // Server proof can become stale relative to another tab staging an outbox.
      // The deletion transaction therefore rechecks both outbox and legacy
      // disposition stores at the last possible moment.
      const outcome = await blobStore.deleteIfUnclaimed(rawAudioId);
      if (outcome === "deleted") deleted += 1;
      else preserved += 1;
    } catch (error) {
      preserved += 1;
      // 认证/服务不可用时停止扫描，不用大量重复请求打扰现场；404 孤儿则继续保留并检查下一条。
      if (!(error instanceof ApiError && error.status === 404)) break;
    }
  }
  return { deleted, preserved };
}

/**
 * Each caller gets its own preserve set. Deletion itself is transactionally
 * guarded, so overlapping sweeps cannot delete a newly staged outbox blob.
 */
export function sweepConfirmedUploadedAudioCache(
  preserveIds: ReadonlySet<string> = new Set(),
): Promise<{ deleted: number; preserved: number }> {
  const claim = activePreservation.claim(preserveIds);
  return runSweep().finally(() => claim.release());
}
