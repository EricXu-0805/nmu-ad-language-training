export type AudioOutboxPhase = "captured" | "registered" | "uploaded";

export interface AudioOutboxEntry {
  schemaVersion: 1;
  rawAudioId: string;
  sessionId: string;
  turnKey: string;
  containsDirectIdentifier: boolean;
  durationSeconds: number;
  blobBytes: number;
  mimeType: string;
  phase: AudioOutboxPhase;
  checksum?: string;
  /** Present only for an autopilot capture that has physically stopped. */
  autopilotStopReason?: "silence" | "user_done" | "max_duration" | "stream_end";
  /** Exact server audioSaved ledger receipt; never synthesized on the device. */
  captureReceiptServerSeq?: number;
  createdAtMs: number;
  updatedAtMs: number;
}

const SAFE_AUDIO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
const SAFE_CHECKSUM = /^[0-9a-f]{64}$/;
const HAS_CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;
const ENTRY_KEYS = new Set([
  "schemaVersion", "rawAudioId", "sessionId", "turnKey", "containsDirectIdentifier",
  "durationSeconds", "blobBytes", "mimeType", "phase", "checksum", "createdAtMs", "updatedAtMs",
  "autopilotStopReason", "captureReceiptServerSeq",
]);
const PHASE_ORDER: Record<AudioOutboxPhase, number> = {
  captured: 0,
  registered: 1,
  uploaded: 2,
};

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** IndexedDB is attacker-influencable; every recovery record is schema checked. */
export function parseAudioOutboxEntry(value: unknown): AudioOutboxEntry {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("录音 outbox 条目不是对象");
  }
  const row = value as Partial<AudioOutboxEntry>;
  const extraKeys = Object.keys(row).filter((key) => !ENTRY_KEYS.has(key));
  if (extraKeys.length) throw new Error("录音 outbox 含未允许字段");
  if (row.schemaVersion !== 1 || !SAFE_AUDIO_ID.test(row.rawAudioId ?? "")) {
    throw new Error("录音 outbox 标识或版本无效");
  }
  if (typeof row.sessionId !== "string" || !row.sessionId || row.sessionId.length > 128
      || HAS_CONTROL_CHARACTER.test(row.sessionId)) {
    throw new Error("录音 outbox 场次无效");
  }
  if (typeof row.turnKey !== "string" || !row.turnKey || row.turnKey.length > 200
      || HAS_CONTROL_CHARACTER.test(row.turnKey)) {
    throw new Error("录音 outbox 环节无效");
  }
  if (typeof row.containsDirectIdentifier !== "boolean"
      || !finiteNumber(row.durationSeconds) || row.durationSeconds < 0 || row.durationSeconds > 21_600
      || !Number.isSafeInteger(row.blobBytes) || (row.blobBytes ?? 0) <= 0
      || typeof row.mimeType !== "string" || !row.mimeType.startsWith("audio/")
      || row.mimeType.length > 200 || HAS_CONTROL_CHARACTER.test(row.mimeType)
      || !(row.phase && Object.hasOwn(PHASE_ORDER, row.phase))
      || !finiteNumber(row.createdAtMs) || row.createdAtMs <= 0
      || !finiteNumber(row.updatedAtMs) || row.updatedAtMs < row.createdAtMs) {
    throw new Error("录音 outbox 元数据无效");
  }
  if (row.phase === "uploaded") {
    if (typeof row.checksum !== "string" || !SAFE_CHECKSUM.test(row.checksum)) {
      throw new Error("已上传 outbox 缺少有效 checksum");
    }
  } else if (row.checksum !== undefined) {
    throw new Error("未上传 outbox 不得预写 checksum");
  }
  if (row.autopilotStopReason !== undefined
      && !["silence", "user_done", "max_duration", "stream_end"].includes(row.autopilotStopReason)) {
    throw new Error("录音 outbox 自动驾驶停止原因非法");
  }
  if (row.captureReceiptServerSeq !== undefined
      && (row.phase !== "uploaded" || !Number.isSafeInteger(row.captureReceiptServerSeq)
        || row.captureReceiptServerSeq <= 0)) {
    throw new Error("录音 outbox 服务器采集收据非法");
  }
  return row as AudioOutboxEntry;
}

export function createAudioOutboxEntry(input: {
  rawAudioId: string;
  sessionId: string;
  turnKey: string;
  containsDirectIdentifier: boolean;
  durationSeconds: number;
  blob: Blob;
  nowMs?: number;
}): AudioOutboxEntry {
  const now = input.nowMs ?? Date.now();
  return parseAudioOutboxEntry({
    schemaVersion: 1,
    rawAudioId: input.rawAudioId,
    sessionId: input.sessionId,
    turnKey: input.turnKey,
    containsDirectIdentifier: input.containsDirectIdentifier,
    durationSeconds: input.durationSeconds,
    blobBytes: input.blob.size,
    mimeType: input.blob.type || "audio/webm",
    phase: "captured",
    createdAtMs: now,
    updatedAtMs: now,
  });
}

/** Only forward, single-step transitions are legal; replaying the same phase is idempotent. */
export function advanceAudioOutbox(
  current: AudioOutboxEntry,
  phase: AudioOutboxPhase,
  opts: { checksum?: string; nowMs?: number } = {},
): AudioOutboxEntry {
  const row = parseAudioOutboxEntry(current);
  const from = PHASE_ORDER[row.phase];
  const to = PHASE_ORDER[phase];
  if (to < from || to > from + 1) throw new Error("录音 outbox 阶段不得回退或跳级");
  const checksum = phase === "uploaded" ? opts.checksum : row.checksum;
  return parseAudioOutboxEntry({
    ...row,
    phase,
    ...(checksum ? { checksum } : {}),
    updatedAtMs: Math.max(row.updatedAtMs, opts.nowMs ?? Date.now()),
  });
}

/** Attach the stop fact before first persistence so refresh recovery never guesses it. */
export function attachAutopilotStopReason(
  current: AudioOutboxEntry,
  stopReason: NonNullable<AudioOutboxEntry["autopilotStopReason"]>,
): AudioOutboxEntry {
  const row = parseAudioOutboxEntry(current);
  if (row.autopilotStopReason !== undefined && row.autopilotStopReason !== stopReason) {
    throw new Error("录音 outbox 停止原因不得改写");
  }
  return parseAudioOutboxEntry({ ...row, autopilotStopReason: stopReason });
}

/** Persist only the exact audioSaved receipt returned by the server ledger. */
export function attachAudioCaptureReceipt(
  current: AudioOutboxEntry,
  receiptServerSeq: number,
  nowMs = Date.now(),
): AudioOutboxEntry {
  const row = parseAudioOutboxEntry(current);
  if (row.phase !== "uploaded" || !Number.isSafeInteger(receiptServerSeq)
      || receiptServerSeq <= 0) {
    throw new Error("仅已上传录音可绑定服务器采集收据");
  }
  if (row.captureReceiptServerSeq !== undefined
      && row.captureReceiptServerSeq !== receiptServerSeq) {
    throw new Error("录音 outbox 服务器采集收据不得改写");
  }
  return parseAudioOutboxEntry({
    ...row,
    captureReceiptServerSeq: receiptServerSeq,
    updatedAtMs: Math.max(row.updatedAtMs, nowMs),
  });
}
