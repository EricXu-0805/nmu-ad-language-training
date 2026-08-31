// 跨端同步消息 —— 操作端为【唯一写者】,消息只带索引/级别指针,绝不带题目文本。
// 老人端凭 (itemIdx,turnIdx,cueLevel) 自己去版本锁定的 plan/bundle 查逐字文本,保证单一内容源。
import type { SessionRuntimeStatus } from "../types";

export type RecState = "idle" | "armed" | "recording" | "stopped";
// 第1周一问分两拍:ask=机器人问句,reply=老人答完后机器人说的那句(只能来自冻结脚本)。
export type RapportBeat = "ask" | "reply";
export type CueLevel = 0 | 1 | 2 | 3;
export type PatientScreen = "idle" | "present" | "record" | "thanks" | "paused" | "done";
export type FeedbackKey =
  | "self" | "cued1_unknown" | "cued1_close" | "cued1_silence"
  | "cued2" | "namefix_l" | "namefix_r";
export const PATIENT_REC_FAILURE_CODES = [
  "microphone_start_timeout",
  "microphone_permission_denied",
  "microphone_not_found",
  "microphone_start_failed",
  "recording_authorization_failed",
] as const;
export type PatientRecFailureCode = typeof PATIENT_REC_FAILURE_CODES[number];

interface PatientRecBase {
  type: "patientRec";
  turnKey: string;
  sessionId: string;
}

export type PatientRecFailureMsg = PatientRecBase & {
  active: false;
  failureCode: PatientRecFailureCode;
  failureId: string;
};

export type PatientRecMsg =
  | (PatientRecBase & { active: true; failureCode?: never; failureId?: never })
  | (PatientRecBase & { active: false; failureCode?: never; failureId?: never })
  | PatientRecFailureMsg;

// wseq:写者(操作端)单调写序号。老人端双源(bus 秒推 + 轮询快照)竞态时,
// 凭它丢弃迟到的旧快照,防止画面被顶回上一题/上一级线索(对认知障碍老人是最糟的跳变)。
export type SyncMsg =
  | { type: "session"; sessionId: string; weekNo: number; eventLine: string; mode: "task" | "rapport"; itemBankVersionId: string; paused?: boolean; runtimeStatus?: SessionRuntimeStatus; wseq?: number }
  // 仅用于同机/同源老人端的减权安全信号：不持久化、不带业务游标，也绝不代表
  // 服务端已经暂停。操作端先靠它立刻停媒体，再由 /pause 的权威投影收口。
  | { type: "safetyStop"; sessionId: string }
  // 老人端自己按下暂停。和 safetyStop 分开，因为它必须在每个标签页
  // 丢弃尚未完成的录音，不得沿用人工暂停的保存语义。幂等键不是凭据。
  | { type: "patientPauseStop"; sessionId: string; idempotencyKey: string }
  // recSeq:每次 arm 递增。armed→armed 重发(老人自停后再示意)靠它触发老人端 effect;无它则依赖值不变、麦克风永不重开。
  // selfStart:操作端按录音资格(recording_allowed)判定后下发——老人端只有收到 true 才显示
  // "点这里,开始回答"自助开录按钮。缺省/false 一律不显示(fail-closed:合规闸门不被老人端绕过)。
  // fbKey/fbItemId/fbSeq:自动驾驶反馈——不载文本,只指向题库/协议固定话术,老人端本地查表回填(fbSeq 变化才重读)。
  | { type: "cursor"; sessionId: string; screen: PatientScreen; itemIdx: number; turnIdx: number; responseRole: string; cueLevel: CueLevel; recording: RecState; recSeq?: number; rawAudioId?: string; selfStart?: boolean; fbKey?: FeedbackKey; fbItemId?: string; fbSeq?: number; wseq?: number }
  | { type: "rapportStep"; sessionId: string; sectionKey: string; questionIdx: number; beat?: RapportBeat; replyId?: string; recording: RecState; recSeq?: number; rawAudioId?: string; assentGate?: boolean; containsDirectIdentifier?: boolean; paused?: boolean; wseq?: number }
  // sessionId:操作端凭它丢弃跨场次的迟到/残留回报(live state 里 audioSaved 存到下次握手才清)。
  | { type: "audioSaved"; rawAudioId: string; durationSeconds: number; byteCount: number; checksum: string; turnKey: string; sessionId: string; containsDirectIdentifier: boolean }
  // 老人端麦克风真值上报(自助开录时操作端唯一的感知渠道;也用于示意录音的开麦确认)。
  // 这是"上报"不是"显示状态"——老人端仍只读游标,写者规则不变(audioSaved/patientRec 两类上报除外)。
  | PatientRecMsg;

export type SessionMsg = Extract<SyncMsg, { type: "session" }>;
export type CursorMsg = Extract<SyncMsg, { type: "cursor" }>;
export type RapportMsg = Extract<SyncMsg, { type: "rapportStep" }>;
export type AudioSavedMsg = Extract<SyncMsg, { type: "audioSaved" }>;
export type SafetyStopMsg = Extract<SyncMsg, { type: "safetyStop" }>;
export type PatientPauseStopMsg = Extract<SyncMsg, { type: "patientPauseStop" }>;

type SyncType = Exclude<SyncMsg["type"], "safetyStop" | "patientPauseStop">;
type UnknownRecord = Record<string, unknown>;

const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;
const SAFE_AUDIO_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
const SAFE_FAILURE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const PATIENT_PAUSE_IDEMPOTENCY_KEY = /^patient_pause:[0-9a-f]{32}$/;
const SESSION_STATUSES = [
  "active", "paused", "intervention_completed", "completed", "aborted", "failed",
] as const satisfies readonly SessionRuntimeStatus[];
const SCREENS = ["idle", "present", "record", "thanks", "paused", "done"] as const;
const REC_STATES = ["idle", "armed", "recording", "stopped"] as const;
const RAPPORT_BEATS = ["ask", "reply"] as const;
const FEEDBACK_KEYS = [
  "self", "cued1_unknown", "cued1_close", "cued1_silence",
  "cued2", "namefix_l", "namefix_r",
] as const;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function exactKeys(value: UnknownRecord, required: readonly string[], optional: readonly string[],
                   withType: boolean): boolean {
  const requiredKeys = withType ? ["type", ...required] : required;
  const allowed = new Set([...requiredKeys, ...optional]);
  return requiredKeys.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => allowed.has(key));
}

function oneOf<const T extends readonly (string | number)[]>(
  value: unknown, allowed: T,
): value is T[number] {
  return allowed.some((candidate) => candidate === value);
}

function boundedText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && !CONTROL_CHARACTER.test(value);
}

function safeAudioId(value: unknown): value is string {
  return typeof value === "string" && SAFE_AUDIO_ID.test(value);
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function turnKey(value: unknown): value is string {
  if (!boundedText(value, 200)) return false;
  if (value.startsWith("关系建立·")) return value.length > "关系建立·".length;
  const separator = value.lastIndexOf("#");
  return separator > 0 && /^[1-9][0-9]*$/.test(value.slice(separator + 1));
}

function addOptionalInteger(target: Record<string, unknown>, source: UnknownRecord,
                            key: string): boolean {
  if (!Object.hasOwn(source, key)) return true;
  if (!nonNegativeInteger(source[key])) return false;
  target[key] = source[key];
  return true;
}

function parseSession(value: unknown, withType: boolean): SessionMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row,
    ["sessionId", "weekNo", "eventLine", "mode", "itemBankVersionId"],
    ["paused", "runtimeStatus", "wseq"], withType)
    || (withType && row.type !== "session")
    || !boundedText(row.sessionId, 128)
    || !Number.isSafeInteger(row.weekNo) || (row.weekNo as number) < 1 || (row.weekNo as number) > 8
    || !boundedText(row.eventLine, 64)
    || !oneOf(row.mode, ["task", "rapport"] as const)
    || !boundedText(row.itemBankVersionId, 128)) return null;
  const result: SessionMsg = {
    type: "session", sessionId: row.sessionId, weekNo: row.weekNo as number,
    eventLine: row.eventLine, mode: row.mode, itemBankVersionId: row.itemBankVersionId,
  };
  if (Object.hasOwn(row, "paused")) {
    if (typeof row.paused !== "boolean") return null;
    result.paused = row.paused;
  }
  if (Object.hasOwn(row, "runtimeStatus")) {
    if (!oneOf(row.runtimeStatus, SESSION_STATUSES)) return null;
    result.runtimeStatus = row.runtimeStatus;
  }
  if (!addOptionalInteger(result, row, "wseq")) return null;
  return result;
}

function parseSafetyStop(value: unknown, withType: boolean): SafetyStopMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row, ["sessionId"], [], withType)
      || (withType && row.type !== "safetyStop")
      || !boundedText(row.sessionId, 128)) return null;
  return { type: "safetyStop", sessionId: row.sessionId };
}

function parsePatientPauseStop(
  value: unknown,
  withType: boolean,
): PatientPauseStopMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row, ["sessionId", "idempotencyKey"], [], withType)
      || (withType && row.type !== "patientPauseStop")
      || !boundedText(row.sessionId, 128)
      || typeof row.idempotencyKey !== "string"
      || !PATIENT_PAUSE_IDEMPOTENCY_KEY.test(row.idempotencyKey)) return null;
  return {
    type: "patientPauseStop",
    sessionId: row.sessionId,
    idempotencyKey: row.idempotencyKey,
  };
}

function parseCursor(value: unknown, withType: boolean): CursorMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row,
    ["sessionId", "screen", "itemIdx", "turnIdx", "responseRole", "cueLevel", "recording"],
    ["recSeq", "rawAudioId", "selfStart", "fbKey", "fbItemId", "fbSeq", "wseq"], withType)
    || (withType && row.type !== "cursor")
    || !boundedText(row.sessionId, 128)
    || !oneOf(row.screen, SCREENS)
    || !nonNegativeInteger(row.itemIdx)
    || !nonNegativeInteger(row.turnIdx)
    || !boundedText(row.responseRole, 64)
    || !oneOf(row.cueLevel, [0, 1, 2, 3] as const)
    || !oneOf(row.recording, REC_STATES)) return null;
  const result: CursorMsg = {
    type: "cursor", sessionId: row.sessionId, screen: row.screen,
    itemIdx: row.itemIdx, turnIdx: row.turnIdx, responseRole: row.responseRole,
    cueLevel: row.cueLevel, recording: row.recording,
  };
  for (const key of ["recSeq", "fbSeq", "wseq"] as const) {
    if (!addOptionalInteger(result, row, key)) return null;
  }
  if (Object.hasOwn(row, "rawAudioId")) {
    if (!safeAudioId(row.rawAudioId)) return null;
    result.rawAudioId = row.rawAudioId;
  }
  if (Object.hasOwn(row, "selfStart")) {
    if (typeof row.selfStart !== "boolean") return null;
    result.selfStart = row.selfStart;
  }
  if (Object.hasOwn(row, "fbKey")) {
    if (!oneOf(row.fbKey, FEEDBACK_KEYS)) return null;
    result.fbKey = row.fbKey;
  }
  if (Object.hasOwn(row, "fbItemId")) {
    if (!boundedText(row.fbItemId, 160)) return null;
    result.fbItemId = row.fbItemId;
  }
  return result;
}

function parseRapport(value: unknown, withType: boolean): RapportMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row,
    ["sessionId", "sectionKey", "questionIdx", "recording"],
    ["beat", "replyId", "recSeq", "rawAudioId", "assentGate", "containsDirectIdentifier", "paused", "wseq"], withType)
    || (withType && row.type !== "rapportStep")
    || !boundedText(row.sessionId, 128)
    || !boundedText(row.sectionKey, 100)
    || !nonNegativeInteger(row.questionIdx)
    || !oneOf(row.recording, REC_STATES)) return null;
  const result: RapportMsg = {
    type: "rapportStep", sessionId: row.sessionId, sectionKey: row.sectionKey,
    questionIdx: row.questionIdx, recording: row.recording,
  };
  if (Object.hasOwn(row, "beat")) {
    if (!oneOf(row.beat, RAPPORT_BEATS)) return null;
    result.beat = row.beat;
  }
  if (Object.hasOwn(row, "replyId")) {
    if (typeof row.replyId !== "string" || !/^[a-z][a-z0-9]{0,15}$/.test(row.replyId)) return null;
    result.replyId = row.replyId;
  }
  for (const key of ["recSeq", "wseq"] as const) {
    if (!addOptionalInteger(result, row, key)) return null;
  }
  if (Object.hasOwn(row, "rawAudioId")) {
    if (!safeAudioId(row.rawAudioId)) return null;
    result.rawAudioId = row.rawAudioId;
  }
  for (const key of ["assentGate", "containsDirectIdentifier", "paused"] as const) {
    if (!Object.hasOwn(row, key)) continue;
    if (typeof row[key] !== "boolean") return null;
    result[key] = row[key];
  }
  return result;
}

function parseAudioSaved(value: unknown, withType: boolean): AudioSavedMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row,
    ["rawAudioId", "durationSeconds", "byteCount", "checksum", "turnKey", "sessionId", "containsDirectIdentifier"],
    [], withType)
    || (withType && row.type !== "audioSaved")
    || !safeAudioId(row.rawAudioId)
    || typeof row.durationSeconds !== "number" || !Number.isFinite(row.durationSeconds)
    || row.durationSeconds < 0 || row.durationSeconds > 21_600
    || typeof row.byteCount !== "number" || !Number.isSafeInteger(row.byteCount)
    || row.byteCount <= 0 || row.byteCount > 64 * 1024 * 1024
    || typeof row.checksum !== "string" || !/^[0-9a-f]{64}$/.test(row.checksum)
    || !turnKey(row.turnKey)
    || !boundedText(row.sessionId, 128)
    || typeof row.containsDirectIdentifier !== "boolean") return null;
  const result: AudioSavedMsg = {
    type: "audioSaved", rawAudioId: row.rawAudioId, durationSeconds: row.durationSeconds,
    byteCount: row.byteCount, checksum: row.checksum,
    turnKey: row.turnKey, sessionId: row.sessionId,
    containsDirectIdentifier: row.containsDirectIdentifier,
  };
  return result;
}

function parsePatientRec(value: unknown, withType: boolean): PatientRecMsg | null {
  const row = record(value);
  if (!row || !exactKeys(row, ["active", "turnKey", "sessionId"], ["failureCode", "failureId"], withType)
    || (withType && row.type !== "patientRec")
    || typeof row.active !== "boolean"
    || !turnKey(row.turnKey)
    || !boundedText(row.sessionId, 128)) return null;
  const hasFailureCode = Object.hasOwn(row, "failureCode");
  const hasFailureId = Object.hasOwn(row, "failureId");
  if (hasFailureCode !== hasFailureId || (row.active && hasFailureCode)) return null;
  if (hasFailureCode) {
    if (!oneOf(row.failureCode, PATIENT_REC_FAILURE_CODES)
        || typeof row.failureId !== "string" || !SAFE_FAILURE_ID.test(row.failureId)) return null;
    return {
      type: "patientRec", active: false, turnKey: row.turnKey, sessionId: row.sessionId,
      failureCode: row.failureCode, failureId: row.failureId,
    };
  }
  return row.active
    ? { type: "patientRec", active: true, turnKey: row.turnKey, sessionId: row.sessionId }
    : { type: "patientRec", active: false, turnKey: row.turnKey, sessionId: row.sessionId };
}

export function isPatientRecFailure(message: PatientRecMsg | null): message is PatientRecFailureMsg {
  return message?.active === false && typeof message.failureCode === "string"
    && typeof message.failureId === "string";
}

/** Strict boundary for BroadcastChannel/localStorage values. Invalid or extra fields fail closed. */
export function parseSyncMsg(value: unknown): SyncMsg | null {
  const row = record(value);
  if (!row || typeof row.type !== "string") return null;
  switch (row.type) {
    case "session": return parseSession(row, true);
    case "safetyStop": return parseSafetyStop(row, true);
    case "patientPauseStop": return parsePatientPauseStop(row, true);
    case "cursor": return parseCursor(row, true);
    case "rapportStep": return parseRapport(row, true);
    case "audioSaved": return parseAudioSaved(row, true);
    case "patientRec": return parsePatientRec(row, true);
    default: return null;
  }
}

export function parseSyncPayload(type: "session", value: unknown): SessionMsg | null;
export function parseSyncPayload(type: "cursor", value: unknown): CursorMsg | null;
export function parseSyncPayload(type: "rapportStep", value: unknown): RapportMsg | null;
export function parseSyncPayload(type: "audioSaved", value: unknown): AudioSavedMsg | null;
export function parseSyncPayload(type: "patientRec", value: unknown): PatientRecMsg | null;
/** Strict boundary for the server's payload-only live-state projections. */
export function parseSyncPayload(type: SyncType, value: unknown): SyncMsg | null {
  switch (type) {
    case "session": return parseSession(value, false);
    case "cursor": return parseCursor(value, false);
    case "rapportStep": return parseRapport(value, false);
    case "audioSaved": return parseAudioSaved(value, false);
    case "patientRec": return parsePatientRec(value, false);
  }
}

// 单机一条流的窗内事件(非跨窗总线):操作端 ⇄ App 路由层叠层宿主。
// 状态源在 ConsoleShell;红线守卫禁止 console/** import 老人端源码,故经事件解耦。
export const PATIENT_VIEW_EVENT = "nmu:patient-view";           // detail: { open: boolean }
export const PATIENT_VIEW_EXIT_EVENT = "nmu:patient-view-exit"; // 宿主按住返回 → 操作端收
export const PATIENT_VIEW_REC_EVENT = "nmu:patient-view-rec";   // detail: { active } 录音真值→宿主退出钮
export const CONSOLE_NOTE_EVENT = "nmu:console-note";           // detail: { count } 叠层期间暂存的提示数
// detail: { sessionId } 老人端一次明确点击激活(浏览器 user activation)+朗读开+连接就绪后,
// 对本窗操作端发一次"可尝试自动启动"信号。只是本机触发条件:不是研究授权、不是服务器
// 真值、更不是"阿里已使用"的证据;操作端收到后仍走全套门禁与服务端重验,绝不绕过。
export const PATIENT_ACTIVATION_EVENT = "nmu:patient-activation";
