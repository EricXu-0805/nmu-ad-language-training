import {
  parseAutopilotAck,
  parseNextCommandProjection,
  type AutopilotAck,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";

const OWNER_KEY = /^[0-9a-f]{64}$/;
const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;
const ENVELOPE_KEYS = new Set([
  "schemaVersion", "ownerKey", "sessionId", "commandKey", "command", "ack", "createdAtMs",
]);
const CHECKPOINT_V1_KEYS = new Set([
  "schemaVersion", "ownerKey", "sessionId", "lastDeviceEventSeq", "updatedAtMs",
]);
const CHECKPOINT_V2_KEYS = new Set([
  "schemaVersion", "ownerKey", "sessionId", "lastDeviceEventSeq", "updatedAtMs", "latch",
]);
const LATCH_KEYS = new Set(["commandKey", "command", "ack"]);

export interface AutopilotAckEnvelope {
  schemaVersion: 1;
  /** SHA-256 capability fingerprint. The bearer capability is never persisted here. */
  ownerKey: string;
  sessionId: string;
  commandKey: string;
  command: NextCommandProjection;
  ack: AutopilotAck;
  createdAtMs: number;
}

export type AutopilotTerminalFailureAck =
  Extract<AutopilotAck, { ack_type: "tts_failed" | "record_failed" }>;

/** Exact evidence for a completed tts_failed/record_failed ACK that a refresh must not forget. */
export interface AutopilotTerminalFailureLatch {
  commandKey: string;
  command: NextCommandProjection;
  ack: AutopilotTerminalFailureAck;
}

export interface AutopilotAckCheckpointV1 {
  schemaVersion: 1;
  ownerKey: string;
  sessionId: string;
  lastDeviceEventSeq: number;
  updatedAtMs: number;
}

export interface AutopilotAckCheckpointV2 {
  schemaVersion: 2;
  ownerKey: string;
  sessionId: string;
  lastDeviceEventSeq: number;
  updatedAtMs: number;
  latch: AutopilotTerminalFailureLatch | null;
}

export type AutopilotAckCheckpoint = AutopilotAckCheckpointV1 | AutopilotAckCheckpointV2;

function exactKeys(value: Record<string, unknown>, allowed: Set<string>): boolean {
  const keys = Object.keys(value);
  return keys.length === allowed.size && keys.every((key) => allowed.has(key));
}

function safeSessionId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 128
    && !CONTROL_CHARACTER.test(value);
}

function ackMatchesCommand(ack: AutopilotAck, command: NextCommandProjection): boolean {
  if (ack.control_generation !== command.control_generation
      || ack.runner_generation !== command.runner_generation
      || ack.command_revision !== command.command_revision) return false;
  if (command.kind === "tts") {
    return ack.ack_type === "tts_started"
      || ack.ack_type === "tts_ended"
      || ack.ack_type === "tts_failed";
  }
  return (ack.ack_type === "record_started"
      || ack.ack_type === "record_stopped"
      || ack.ack_type === "record_failed")
    && (ack.ack_type !== "record_stopped"
      || ack.raw_audio_id === command.payload.raw_audio_id);
}

/** IndexedDB is attacker-influencable; every recovered ACK is revalidated exactly. */
export function parseAutopilotAckEnvelope(value: unknown): AutopilotAckEnvelope {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("自动驾驶 ACK outbox 条目不是对象");
  }
  const row = value as Record<string, unknown>;
  if (!exactKeys(row, ENVELOPE_KEYS) || row.schemaVersion !== 1
      || typeof row.ownerKey !== "string" || !OWNER_KEY.test(row.ownerKey)
      || !safeSessionId(row.sessionId)
      || typeof row.commandKey !== "string"
      || !Number.isSafeInteger(row.createdAtMs) || (row.createdAtMs as number) <= 0) {
    throw new Error("自动驾驶 ACK outbox 元数据非法");
  }
  const command = parseNextCommandProjection(row.command);
  const ack = parseAutopilotAck(row.ack);
  if (row.commandKey !== command.command_key || !ackMatchesCommand(ack, command)) {
    throw new Error("自动驾驶 ACK 与冻结命令不一致");
  }
  return {
    schemaVersion: 1,
    ownerKey: row.ownerKey,
    sessionId: row.sessionId,
    commandKey: row.commandKey,
    command,
    ack,
    createdAtMs: row.createdAtMs as number,
  };
}

export function createAutopilotAckEnvelope(input: {
  ownerKey: string;
  sessionId: string;
  command: NextCommandProjection;
  ack: AutopilotAck;
  nowMs?: number;
}): AutopilotAckEnvelope {
  return parseAutopilotAckEnvelope({
    schemaVersion: 1,
    ownerKey: input.ownerKey,
    sessionId: input.sessionId,
    commandKey: input.command.command_key,
    command: input.command,
    ack: input.ack,
    createdAtMs: input.nowMs ?? Date.now(),
  });
}

/** latch 的 tts_failed/record_failed 限定复用既有 ackMatchesCommand 的代际/revision 校验。 */
function parseTerminalFailureLatch(value: unknown): AutopilotTerminalFailureLatch {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("自动驾驶终态失败 latch 不是对象");
  }
  const row = value as Record<string, unknown>;
  if (!exactKeys(row, LATCH_KEYS) || typeof row.commandKey !== "string") {
    throw new Error("自动驾驶终态失败 latch 元数据非法");
  }
  const command = parseNextCommandProjection(row.command);
  const ack = parseAutopilotAck(row.ack);
  if (ack.ack_type !== "tts_failed" && ack.ack_type !== "record_failed") {
    throw new Error("自动驾驶终态失败 latch 与冻结命令不一致");
  }
  if (row.commandKey !== command.command_key || !ackMatchesCommand(ack, command)) {
    throw new Error("自动驾驶终态失败 latch 与冻结命令不一致");
  }
  return { commandKey: row.commandKey, command, ack };
}

/** schemaVersion 1（无 latch）必须继续可读；新写入一律走 2。 */
export function parseAutopilotAckCheckpoint(value: unknown): AutopilotAckCheckpoint {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("自动驾驶 ACK checkpoint 不是对象");
  }
  const row = value as Record<string, unknown>;
  const commonValid = typeof row.ownerKey === "string" && OWNER_KEY.test(row.ownerKey)
    && safeSessionId(row.sessionId)
    && Number.isSafeInteger(row.lastDeviceEventSeq) && (row.lastDeviceEventSeq as number) >= 0
    && Number.isSafeInteger(row.updatedAtMs) && (row.updatedAtMs as number) > 0;
  if (row.schemaVersion === 1) {
    if (!commonValid || !exactKeys(row, CHECKPOINT_V1_KEYS)) {
      throw new Error("自动驾驶 ACK checkpoint 非法");
    }
    return {
      schemaVersion: 1,
      ownerKey: row.ownerKey as string,
      sessionId: row.sessionId as string,
      lastDeviceEventSeq: row.lastDeviceEventSeq as number,
      updatedAtMs: row.updatedAtMs as number,
    };
  }
  if (row.schemaVersion === 2) {
    if (!commonValid || !exactKeys(row, CHECKPOINT_V2_KEYS)) {
      throw new Error("自动驾驶 ACK checkpoint 非法");
    }
    const latch = row.latch === null ? null : parseTerminalFailureLatch(row.latch);
    if (latch && latch.ack.device_event_seq !== row.lastDeviceEventSeq) {
      throw new Error("自动驾驶终态失败 latch 与 checkpoint 序号不一致");
    }
    return {
      schemaVersion: 2,
      ownerKey: row.ownerKey as string,
      sessionId: row.sessionId as string,
      lastDeviceEventSeq: row.lastDeviceEventSeq as number,
      updatedAtMs: row.updatedAtMs as number,
      latch,
    };
  }
  throw new Error("自动驾驶 ACK checkpoint 非法");
}

export function createAutopilotAckCheckpoint(input: {
  ownerKey: string;
  sessionId: string;
  lastDeviceEventSeq: number;
  latch?: AutopilotTerminalFailureLatch | null;
  nowMs?: number;
}): AutopilotAckCheckpoint {
  return parseAutopilotAckCheckpoint({
    schemaVersion: 2,
    ownerKey: input.ownerKey,
    sessionId: input.sessionId,
    lastDeviceEventSeq: input.lastDeviceEventSeq,
    updatedAtMs: input.nowMs ?? Date.now(),
    latch: input.latch ?? null,
  });
}

/** 无论 checkpoint 是 v1 迁移读还是 v2，统一取 latch（v1 恒为 null）。 */
export function checkpointTerminalFailureLatch(
  checkpoint: AutopilotAckCheckpoint | null,
): AutopilotTerminalFailureLatch | null {
  return checkpoint && checkpoint.schemaVersion === 2 ? checkpoint.latch : null;
}

/** completeAutopilotAck 的原子决策核心：失败类 ACK 落 latch，其余一律清。 */
export function nextAutopilotAckLatch(
  entry: AutopilotAckEnvelope,
): AutopilotTerminalFailureLatch | null {
  if (entry.ack.ack_type !== "tts_failed" && entry.ack.ack_type !== "record_failed") return null;
  return { commandKey: entry.command.command_key, command: entry.command, ack: entry.ack };
}

export function nextAutopilotAckCheckpoint(
  entry: AutopilotAckEnvelope,
  nowMs?: number,
): AutopilotAckCheckpoint {
  return createAutopilotAckCheckpoint({
    ownerKey: entry.ownerKey,
    sessionId: entry.sessionId,
    lastDeviceEventSeq: entry.ack.device_event_seq,
    latch: nextAutopilotAckLatch(entry),
    nowMs,
  });
}

export function sameAutopilotAckEnvelope(
  leftValue: unknown,
  rightValue: unknown,
): boolean {
  const left = parseAutopilotAckEnvelope(leftValue);
  const right = parseAutopilotAckEnvelope(rightValue);
  const canonical = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(canonical);
    if (value !== null && typeof value === "object") {
      return Object.fromEntries(Object.entries(value as Record<string, unknown>)
        .sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey))
        .map(([key, item]) => [key, canonical(item)]));
    }
    return value;
  };
  return JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
}

export async function fingerprintAutopilotCapability(input: {
  sessionId: string;
  capability: string;
}): Promise<string> {
  if (!safeSessionId(input.sessionId) || typeof input.capability !== "string"
      || input.capability.length < 32 || input.capability.length > 256
      || !globalThis.crypto?.subtle) {
    throw new Error("当前设备无法安全建立 ACK 所有权摘要");
  }
  const bytes = new TextEncoder().encode(
    `nmu-p0a-ack-owner-v1\u0000${input.sessionId}\u0000${input.capability}`,
  );
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0")).join("");
}
