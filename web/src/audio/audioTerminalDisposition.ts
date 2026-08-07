import { ApiError } from "../apiResponse.ts";
import { parseAudioOutboxEntry, type AudioOutboxEntry } from "./audioOutbox.ts";
import { sha256Blob } from "./audioUploadReceipt.ts";

const CHECKSUM = /^[0-9a-f]{64}$/;
const DETAIL_KEYS = [
  "code",
  "schemaVersion",
  "action",
  "reason",
  "rawAudioId",
  "sessionId",
  "turnKey",
  "byteCount",
  "checksum",
  "containsDirectIdentifier",
] as const;
const SAVED_KEYS = [
  "rawAudioId",
  "durationSeconds",
  "byteCount",
  "checksum",
  "turnKey",
  "sessionId",
  "containsDirectIdentifier",
] as const;

export interface AudioSavedPayload {
  rawAudioId: string;
  durationSeconds: number;
  byteCount: number;
  checksum: string;
  turnKey: string;
  sessionId: string;
  containsDirectIdentifier: boolean;
}

export interface AudioTerminalDisposition {
  code: "audio_terminal_disposition";
  schemaVersion: 1;
  action: "discard_local_copy";
  reason: "deleted" | "withdrawn";
  rawAudioId: string;
  sessionId: string;
  turnKey: string;
  byteCount: number;
  checksum: string;
  containsDirectIdentifier: boolean;
}

export type AudioSavedFinalizationOutcome =
  | { kind: "server-saved" }
  | { kind: "terminal-discarded"; disposition: AudioTerminalDisposition };

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length
    && actual.every((key, index) => key === wanted[index]);
}

/** Build the one canonical metadata payload used for normal ACK and one 409 outcome probe. */
export function buildAudioSavedPayload(
  entryValue: AudioOutboxEntry,
  checksumValue: string,
): AudioSavedPayload {
  const entry = parseAudioOutboxEntry(entryValue);
  const checksum = checksumValue;
  if (!CHECKSUM.test(checksum)) throw new Error("录音终态核对 checksum 无效");
  if (entry.checksum !== undefined && entry.checksum !== checksum) {
    throw new Error("录音终态核对 checksum 与 outbox 不一致");
  }
  return {
    rawAudioId: entry.rawAudioId,
    durationSeconds: entry.durationSeconds,
    byteCount: entry.blobBytes,
    checksum,
    turnKey: entry.turnKey,
    sessionId: entry.sessionId,
    containsDirectIdentifier: entry.containsDirectIdentifier,
  };
}

/**
 * Accept only the exact governance response attached to an HTTP 410 ApiError.
 * The response is bound to both the canonical outbox snapshot and freshly
 * hashed local bytes. Any ambiguity returns null and preserves the local copy.
 */
export async function parseAudioTerminalDisposition(
  error: unknown,
  expected: {
    entry: AudioOutboxEntry;
    blob: Blob;
    saved: AudioSavedPayload;
  },
): Promise<AudioTerminalDisposition | null> {
  if (!(error instanceof ApiError) || error.status !== 410
      || error.detailEnvelope !== "nested-detail") return null;
  if (error.detailData === null || typeof error.detailData !== "object"
      || Array.isArray(error.detailData)) return null;

  let entry: AudioOutboxEntry;
  try { entry = parseAudioOutboxEntry(expected.entry); }
  catch { return null; }
  const detail = error.detailData as Record<string, unknown>;
  if (!exactKeys(detail, DETAIL_KEYS)) return null;
  if (detail.code !== "audio_terminal_disposition"
      || detail.schemaVersion !== 1
      || detail.action !== "discard_local_copy"
      || (detail.reason !== "deleted" && detail.reason !== "withdrawn")) return null;

  const saved = expected.saved;
  if (!exactKeys(saved as unknown as Record<string, unknown>, SAVED_KEYS)
      || !CHECKSUM.test(saved.checksum)
      || saved.rawAudioId !== entry.rawAudioId
      || saved.sessionId !== entry.sessionId
      || saved.turnKey !== entry.turnKey
      || saved.byteCount !== entry.blobBytes
      || saved.durationSeconds !== entry.durationSeconds
      || saved.containsDirectIdentifier !== entry.containsDirectIdentifier
      || (entry.checksum !== undefined && entry.checksum !== saved.checksum)) return null;
  if (expected.blob.size !== entry.blobBytes
      || (expected.blob.type || "audio/webm") !== entry.mimeType) return null;
  if (detail.rawAudioId !== saved.rawAudioId
      || detail.sessionId !== saved.sessionId
      || detail.turnKey !== saved.turnKey
      || detail.byteCount !== saved.byteCount
      || detail.checksum !== saved.checksum
      || detail.containsDirectIdentifier !== saved.containsDirectIdentifier) return null;

  // The detail checksum is never trusted on its own. This is the first live-byte
  // hash; blobStore performs a second independent re-read/hash before deletion.
  let actualChecksum: string;
  try { actualChecksum = await sha256Blob(expected.blob); }
  catch { return null; }
  if (actualChecksum !== saved.checksum) return null;
  return detail as unknown as AudioTerminalDisposition;
}

/**
 * Central finalizer makes the side-effect boundary testable: only a validated
 * 2xx ledger receipt may broadcast audioSaved; exact terminal disposal never
 * broadcasts or invokes any progress/AI callback.
 */
export async function finalizeAudioSavedRequest(
  input: {
    entry: AudioOutboxEntry;
    blob: Blob;
    saved: AudioSavedPayload;
    broadcastSaved: boolean | (() => boolean);
  },
  deps: {
    putAudioSaved(saved: AudioSavedPayload): Promise<unknown>;
    completeServerSaved(response: unknown): Promise<void>;
    discardTerminalOutbox(disposition: AudioTerminalDisposition): Promise<void>;
    broadcastAudioSaved(saved: AudioSavedPayload): void;
    // 本地删除完成后的治理回执上报(收据 144)。best-effort:失败吞掉不抛——
    // 本地删除已完成是唯一必须成立的事实,回执缺席由治理面板可见化。
    reportLocalCopyDisposal?(disposition: AudioTerminalDisposition): Promise<void>;
  },
): Promise<AudioSavedFinalizationOutcome> {
  let response: unknown;
  try {
    response = await deps.putAudioSaved(input.saved);
  } catch (error) {
    const disposition = await parseAudioTerminalDisposition(error, input);
    if (!disposition) throw error;
    await deps.discardTerminalOutbox(disposition);
    if (deps.reportLocalCopyDisposal) {
      try {
        await deps.reportLocalCopyDisposal(disposition);
      } catch {
        // 删除永不等待回执;上报失败不影响终态结论。
      }
    }
    return { kind: "terminal-discarded", disposition };
  }
  await deps.completeServerSaved(response);
  const shouldBroadcast = typeof input.broadcastSaved === "function"
    ? input.broadcastSaved()
    : input.broadcastSaved;
  if (shouldBroadcast) deps.broadcastAudioSaved(input.saved);
  return { kind: "server-saved" };
}
