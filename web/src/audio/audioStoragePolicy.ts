const SAFE_AUDIO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
const MARKER_KEYS = new Set([
  "schemaVersion", "rawAudioId", "detectedAtMs", "sourceVersion", "reason",
]);

export interface LegacyAudioOrphan {
  schemaVersion: 1;
  rawAudioId: string;
  detectedAtMs: number;
  sourceVersion: number;
  reason: "missing_outbox_metadata";
}

export type AutomaticCacheDeleteDecision =
  | "delete"
  | "preserve-request"
  | "preserve-outbox"
  | "preserve-legacy";

export function isSafeAudioStorageId(value: unknown): value is string {
  return typeof value === "string" && SAFE_AUDIO_ID.test(value);
}

function stringIds(keys: readonly IDBValidKey[]): string[] {
  return keys.filter(isSafeAudioStorageId);
}

/** IDs whose bytes exist but have no recoverable outbox metadata. */
export function findLegacyBlobIds(
  blobKeys: readonly IDBValidKey[],
  outboxKeys: readonly IDBValidKey[],
  markedKeys: readonly IDBValidKey[] = [],
): string[] {
  const tracked = new Set([...stringIds(outboxKeys), ...stringIds(markedKeys)]);
  return stringIds(blobKeys).filter((rawAudioId) => !tracked.has(rawAudioId)).sort();
}

export function createLegacyAudioOrphan(
  rawAudioId: string,
  sourceVersion: number,
  nowMs = Date.now(),
): LegacyAudioOrphan {
  return parseLegacyAudioOrphan({
    schemaVersion: 1,
    rawAudioId,
    detectedAtMs: nowMs,
    sourceVersion,
    reason: "missing_outbox_metadata",
  });
}

/** IndexedDB is untrusted; persisted disposition markers are strict data too. */
export function parseLegacyAudioOrphan(value: unknown): LegacyAudioOrphan {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("旧版录音处置标记无效");
  }
  const row = value as Partial<LegacyAudioOrphan>;
  if (Object.keys(row).some((key) => !MARKER_KEYS.has(key))
      || row.schemaVersion !== 1
      || typeof row.rawAudioId !== "string" || !SAFE_AUDIO_ID.test(row.rawAudioId)
      || typeof row.detectedAtMs !== "number" || !Number.isFinite(row.detectedAtMs) || row.detectedAtMs <= 0
      || !Number.isSafeInteger(row.sourceVersion) || (row.sourceVersion ?? -1) < 1
      || row.reason !== "missing_outbox_metadata") {
    throw new Error("旧版录音处置标记无效");
  }
  return row as LegacyAudioOrphan;
}

export function automaticCacheDeleteDecision(input: {
  explicitlyPreserved: boolean;
  hasOutbox: boolean;
  legacyMarked: boolean;
}): AutomaticCacheDeleteDecision {
  if (input.explicitlyPreserved) return "preserve-request";
  if (input.hasOutbox) return "preserve-outbox";
  if (input.legacyMarked) return "preserve-legacy";
  return "delete";
}
