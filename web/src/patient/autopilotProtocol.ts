// 老人端自动驾驶 HTTP 信任边界。这里故意不接受字段别名或向后兼容：
// 服务端若意外下发题库答案、未来提示或旧协议字段，必须在开麦/朗读前失败关闭。

export const AUTOPILOT_TTS_PURPOSES = [
  "question", "cue", "feedback", "tell_answer",
] as const;
export type AutopilotTtsPurpose = typeof AUTOPILOT_TTS_PURPOSES[number];

export const AUTOPILOT_MIME_TYPES = [
  "audio/webm",
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/mp4",
] as const;
export type AutopilotMimeType = typeof AUTOPILOT_MIME_TYPES[number];

export const AUTOPILOT_STOP_REASONS = [
  "silence", "user_done", "max_duration", "stream_end",
] as const;
export type AutopilotStopReason = typeof AUTOPILOT_STOP_REASONS[number];

export const AUTOPILOT_ERROR_CODES = [
  "audio_playback_failed",
  "tts_cancelled",
  "microphone_denied",
  "microphone_unavailable",
  "recording_start_failed",
  "recording_runtime_failed",
  "recording_upload_failed",
  "device_command_timeout",
  "device_runtime_failed",
] as const;
export type AutopilotErrorCode = typeof AUTOPILOT_ERROR_CODES[number];

export const AUTOPILOT_RESPONSE_PATHS = ["unknown", "close", "silence"] as const;
export type AutopilotResponsePath = typeof AUTOPILOT_RESPONSE_PATHS[number];

export interface DeviceTtsPayload {
  speech_key: string;
  speech_text: string;
  purpose: AutopilotTtsPurpose;
  // 服务端冻结分支的证据，只在一级 cue/feedback 出现。客户端不据此选分支，
  // 话术永远来自 speech_text；这里带上它只是为了让契约可核对。
  response_path?: AutopilotResponsePath;
}

export interface DeviceRecordPayload {
  raw_audio_id: string;
  turn_ref: string;
  max_duration_seconds: number;
  contains_direct_identifier: boolean;
  presentation_speech_key: string;
  presentation_speech_text: string;
  presentation_purpose: "question" | "cue";
}

interface NextCommandBase {
  schema_version: 1;
  command_key: string;
  command_seq: number;
  state: "pending" | "started";
  command_revision: number;
  control_generation: number;
  runner_generation: number;
  item_ref: string;
  turn_seq: number;
  attempt_seq: number;
  prompt_level: 0 | 1 | 2 | 3;
}

export type NextCommandProjection =
  | (NextCommandBase & { kind: "tts"; payload: DeviceTtsPayload })
  | (NextCommandBase & { kind: "record"; payload: DeviceRecordPayload });

interface AckBase {
  idempotency_key: string;
  control_generation: number;
  runner_generation: number;
  command_revision: number;
  device_event_seq: number;
  client_observed_ms?: number;
}

export type AutopilotAck =
  | (AckBase & {
    ack_type: "tts_started";
    media_duration_ms?: number;
  })
  | (AckBase & {
    ack_type: "tts_ended";
    media_ended: true;
    media_duration_ms?: number;
  })
  | (AckBase & {
    ack_type: "tts_failed";
    error_code: AutopilotErrorCode;
  })
  | (AckBase & {
    ack_type: "record_started";
    mime_type: AutopilotMimeType;
    sample_rate_hz?: number;
    channels?: 1 | 2;
  })
  | (AckBase & {
    ack_type: "record_stopped";
    stop_reason: AutopilotStopReason;
    raw_audio_id: string;
    receipt_server_seq: number;
    checksum: string;
    byte_count: number;
    duration_seconds: number;
  })
  | (AckBase & {
    ack_type: "record_failed";
    error_code: AutopilotErrorCode;
  });

export type AutopilotAckFacts =
  | Omit<Extract<AutopilotAck, { ack_type: "tts_started" }>, keyof AckBase>
  | Omit<Extract<AutopilotAck, { ack_type: "tts_ended" }>, keyof AckBase>
  | Omit<Extract<AutopilotAck, { ack_type: "tts_failed" }>, keyof AckBase>
  | Omit<Extract<AutopilotAck, { ack_type: "record_started" }>, keyof AckBase>
  | Omit<Extract<AutopilotAck, { ack_type: "record_stopped" }>, keyof AckBase>
  | Omit<Extract<AutopilotAck, { ack_type: "record_failed" }>, keyof AckBase>;

type UnknownRecord = Record<string, unknown>;

const COMMAND_KEYS = [
  "schema_version", "command_key", "command_seq", "kind", "state",
  "command_revision", "control_generation", "runner_generation", "item_ref",
  "turn_seq", "attempt_seq", "prompt_level", "payload",
] as const;
const TTS_PAYLOAD_KEYS = ["speech_key", "speech_text", "purpose"] as const;
const TTS_PAYLOAD_OPTIONAL_KEYS = ["response_path"] as const;
const RECORD_PAYLOAD_KEYS = [
  "raw_audio_id", "turn_ref", "max_duration_seconds", "contains_direct_identifier",
  "presentation_speech_key", "presentation_speech_text", "presentation_purpose",
] as const;
const ACK_COMMON_KEYS = [
  "idempotency_key", "ack_type", "control_generation", "runner_generation",
  "command_revision", "device_event_seq",
] as const;

const COMMAND_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const SPEECH_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const RAW_AUDIO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
const ITEM_REF = /^itm-[0-9]{4}$/;
const TURN_REF = /^(itm-[0-9]{4})#([1-9][0-9]*)$/;
const CHECKSUM = /^[0-9a-f]{64}$/;
const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function hasExactKeys(
  value: UnknownRecord,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const allowed = new Set([...required, ...optional]);
  return required.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => allowed.has(key));
}

function oneOf<const T extends readonly (string | number)[]>(
  value: unknown,
  allowed: T,
): value is T[number] {
  return allowed.some((candidate) => candidate === value);
}

function safeInteger(value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === "number" && Number.isSafeInteger(value)
    && value >= min && value <= max;
}

function finiteNumber(value: unknown, min: number, max: number): value is number {
  return typeof value === "number" && Number.isFinite(value)
    && value >= min && value <= max;
}

function boundedText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && !CONTROL_CHARACTER.test(value);
}

function commandBaseIsValid(row: UnknownRecord): boolean {
  return row.schema_version === 1
    && COMMAND_KEY.test(typeof row.command_key === "string" ? row.command_key : "")
    && safeInteger(row.command_seq, 1)
    && oneOf(row.state, ["pending", "started"] as const)
    && safeInteger(row.command_revision, 0)
    && safeInteger(row.control_generation, 1)
    && safeInteger(row.runner_generation, 1)
    && typeof row.item_ref === "string" && ITEM_REF.test(row.item_ref) && row.item_ref !== "itm-0000"
    && safeInteger(row.turn_seq, 1)
    && safeInteger(row.attempt_seq, 1)
    && oneOf(row.prompt_level, [0, 1, 2, 3] as const);
}

function parseTtsPayload(value: unknown, row: UnknownRecord): DeviceTtsPayload | null {
  const payload = record(value);
  if (!payload || !hasExactKeys(payload, TTS_PAYLOAD_KEYS, TTS_PAYLOAD_OPTIONAL_KEYS)
      || !SPEECH_KEY.test(typeof payload.speech_key === "string" ? payload.speech_key : "")
      || !boundedText(payload.speech_text, 2_000)
      || !oneOf(payload.purpose, AUTOPILOT_TTS_PURPOSES)) return null;

  // 与服务端冻结语义保持同一封闭值域。这里不判断文本“像不像答案”，
  // 只允许服务端显式授权的当前 purpose；额外 target/answer 字段已被 exact keys 拒绝。
  // 与服务端 _validate_tts_semantics 同一张 (prompt_level, attempt_seq) 矩阵。
  // 只绑 prompt_level 不够：例如 cue(2,2) 的等级合法而尝试序号不合法，冻结语义里
  // 根本不存在这一格，放过去等于承认一条服务端拒绝签发的话术。
  const FROZEN_STAGES: Record<AutopilotTtsPurpose, readonly (readonly [number, number])[]> = {
    question: [[0, 1]],
    cue: [[1, 2], [2, 3]],
    feedback: [[0, 1], [1, 2], [2, 3]],
    tell_answer: [[3, 3]],
  };
  const stageAllowed = FROZEN_STAGES[payload.purpose as AutopilotTtsPurpose]
    .some(([level, attempt]) => row.prompt_level === level && row.attempt_seq === attempt);
  if (!stageAllowed) return null;

  // response_path 是服务端冻结分支的证据，不是客户端选分支的依据。服务端只在
  // 一级 cue(1,2) 与一级 feedback(1,2) 上冻结它，其余环节根本没有这个字段。
  // 所以这里核对的是"该有的必须有、不该有的必须没有"，两边都不放过：
  // 缺失会让分支证据凭空消失，多出来则说明投影与冻结语义已经对不上。
  const firstLevelBranch = (payload.purpose === "cue" || payload.purpose === "feedback")
    && row.prompt_level === 1 && row.attempt_seq === 2;
  const carriesResponsePath = Object.hasOwn(payload, "response_path");
  if (firstLevelBranch !== carriesResponsePath) return null;
  // null 不在闭集里，被这一句一并拒掉。
  if (carriesResponsePath
      && !oneOf(payload.response_path, AUTOPILOT_RESPONSE_PATHS)) return null;
  return payload as unknown as DeviceTtsPayload;
}

function parseRecordPayload(value: unknown, row: UnknownRecord): DeviceRecordPayload | null {
  const payload = record(value);
  if (!payload || !hasExactKeys(payload, RECORD_PAYLOAD_KEYS)
      || !RAW_AUDIO_ID.test(typeof payload.raw_audio_id === "string" ? payload.raw_audio_id : "")
      || typeof payload.turn_ref !== "string"
      || !safeInteger(payload.max_duration_seconds, 1, 300)
      || typeof payload.contains_direct_identifier !== "boolean"
      || !SPEECH_KEY.test(typeof payload.presentation_speech_key === "string"
        ? payload.presentation_speech_key : "")
      || !boundedText(payload.presentation_speech_text, 2_000)
      || !oneOf(payload.presentation_purpose, ["question", "cue"] as const)) return null;
  const match = TURN_REF.exec(payload.turn_ref);
  if (!match || match[1] !== row.item_ref || Number(match[2]) !== row.turn_seq) return null;
  if (payload.presentation_purpose === "question"
      && (row.prompt_level !== 0 || row.attempt_seq !== 1)) return null;
  if (payload.presentation_purpose === "cue"
      && !oneOf(row.prompt_level, [1, 2] as const)) return null;
  return payload as unknown as DeviceRecordPayload;
}

/** Strictly decode the current-only command projection before any media effect. */
export function parseNextCommandProjection(value: unknown): NextCommandProjection {
  const row = record(value);
  if (!row || !hasExactKeys(row, COMMAND_KEYS) || !commandBaseIsValid(row)) {
    throw new Error("自动驾驶命令响应不符合最小投影契约");
  }
  if (row.kind === "tts") {
    const payload = parseTtsPayload(row.payload, row);
    if (payload) return { ...row, kind: "tts", payload } as unknown as NextCommandProjection;
  } else if (row.kind === "record") {
    const payload = parseRecordPayload(row.payload, row);
    if (payload) return { ...row, kind: "record", payload } as unknown as NextCommandProjection;
  }
  throw new Error("自动驾驶命令响应不符合当前命令类型");
}

function parseAckCommon(row: UnknownRecord): boolean {
  return COMMAND_KEY.test(typeof row.idempotency_key === "string" ? row.idempotency_key : "")
    && safeInteger(row.control_generation, 1)
    && safeInteger(row.runner_generation, 1)
    && safeInteger(row.command_revision, 0)
    && safeInteger(row.device_event_seq, 1)
    && (!Object.hasOwn(row, "client_observed_ms")
      || safeInteger(row.client_observed_ms, 0));
}

/** Strictly validate the closed, event-specific device ACK union. */
export function parseAutopilotAck(value: unknown): AutopilotAck {
  const row = record(value);
  if (!row || !parseAckCommon(row) || typeof row.ack_type !== "string") {
    throw new Error("自动驾驶设备回执结构非法");
  }
  const optionalClientTime = ["client_observed_ms"] as const;
  const exact = (required: readonly string[], optional: readonly string[] = []) =>
    hasExactKeys(row, [...ACK_COMMON_KEYS, ...required], [...optionalClientTime, ...optional]);

  switch (row.ack_type) {
    case "tts_started":
      if (exact([], ["media_duration_ms"])
          && (!Object.hasOwn(row, "media_duration_ms")
            || safeInteger(row.media_duration_ms, 0, 21_600_000))) {
        return row as unknown as AutopilotAck;
      }
      break;
    case "tts_ended":
      if (exact(["media_ended"], ["media_duration_ms"])
          && row.media_ended === true
          && (!Object.hasOwn(row, "media_duration_ms")
            || safeInteger(row.media_duration_ms, 0, 21_600_000))) {
        return row as unknown as AutopilotAck;
      }
      break;
    case "tts_failed":
    case "record_failed":
      if (exact(["error_code"]) && oneOf(row.error_code, AUTOPILOT_ERROR_CODES)) {
        return row as unknown as AutopilotAck;
      }
      break;
    case "record_started":
      if (exact(["mime_type"], ["sample_rate_hz", "channels"])
          && oneOf(row.mime_type, AUTOPILOT_MIME_TYPES)
          && (!Object.hasOwn(row, "sample_rate_hz")
            || safeInteger(row.sample_rate_hz, 8_000, 192_000))
          && (!Object.hasOwn(row, "channels") || oneOf(row.channels, [1, 2] as const))) {
        return row as unknown as AutopilotAck;
      }
      break;
    case "record_stopped":
      if (exact([
        "stop_reason", "raw_audio_id", "receipt_server_seq", "checksum",
        "byte_count", "duration_seconds",
      ])
          && oneOf(row.stop_reason, AUTOPILOT_STOP_REASONS)
          && RAW_AUDIO_ID.test(typeof row.raw_audio_id === "string" ? row.raw_audio_id : "")
          && safeInteger(row.receipt_server_seq, 1)
          && typeof row.checksum === "string" && CHECKSUM.test(row.checksum)
          && safeInteger(row.byte_count, 1)
          && finiteNumber(row.duration_seconds, 0, 21_600)) {
        return row as unknown as AutopilotAck;
      }
      break;
  }
  throw new Error("自动驾驶设备回执事件载荷非法");
}

/**
 * Construct one ACK from the exact current command. device_event_seq is always
 * the next local per-device value; the reducer still rejects gaps/reordering.
 */
export function buildAutopilotAck(
  command: NextCommandProjection,
  lastDeviceEventSeq: number,
  idempotencyKey: string,
  facts: AutopilotAckFacts,
  clientObservedMs?: number,
): AutopilotAck {
  if (!safeInteger(lastDeviceEventSeq, 0)) throw new Error("设备事件序号非法");
  const candidate: UnknownRecord = {
    idempotency_key: idempotencyKey,
    control_generation: command.control_generation,
    runner_generation: command.runner_generation,
    command_revision: command.command_revision,
    device_event_seq: lastDeviceEventSeq + 1,
    ...facts,
  };
  if (clientObservedMs !== undefined) candidate.client_observed_ms = clientObservedMs;
  return parseAutopilotAck(candidate);
}
