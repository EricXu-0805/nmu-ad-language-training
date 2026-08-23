import { parseSyncPayload } from "./sync/messages.ts";
import type {
  SessionRuntimeCursor,
  SessionRuntimeRapportStep,
  SessionRuntimeState,
  SessionRuntimeStatus,
} from "./types.ts";

type UnknownRecord = Record<string, unknown>;

const RUNTIME_KEYS = [
  "sessionId", "status", "revision", "cursor", "rapportStep",
  "pausedAt", "resumedAt", "interventionCompletedAt", "interventionEndedBy",
  "completedAt", "abortedAt", "endedBy", "endReason", "updatedAt",
] as const;
const RUNTIME_EXTRA_KEYS = ["interventionAssessment", "completionAssessment"] as const;
const RUNTIME_STATUSES = new Set<SessionRuntimeStatus>([
  "active", "paused", "intervention_completed", "completed", "aborted", "failed",
]);
const SAFE_SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;
const ISO_DATETIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|([+-])(\d{2}):(\d{2}))?$/;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function exactKeys(
  value: UnknownRecord,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const allowed = new Set([...required, ...optional]);
  return required.every((key) => Object.hasOwn(value, key))
    && Object.keys(value).every((key) => allowed.has(key));
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function boundedText(value: unknown, max: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && value.trim() === value && !CONTROL_CHARACTER.test(value);
}

function validDateTime(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = ISO_DATETIME.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  if (year < 1 || month < 1 || month > 12 || day < 1
      || hour > 23 || minute > 59 || second > 59
      || offsetHour > 14 || offsetMinute > 59
      || (offsetHour === 14 && offsetMinute !== 0)) {
    return false;
  }
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= (days[month - 1] ?? 0);
}

function nullableDateTime(value: unknown): value is string | null {
  return value === null || validDateTime(value);
}

function nullableActor(value: unknown): value is string | null {
  return value === null || boundedText(value, 128);
}

function parseRuntimeCursor(value: unknown, expectedSessionId: string): SessionRuntimeCursor | null {
  if (value === null) return null;
  const row = record(value);
  if (!row) throw new Error("服务端场次运行游标响应格式无效");

  // sourceWseq is a server diagnostic field. Validate it before dropping it;
  // no other internal or future field may silently cross this trust boundary.
  const normalized: UnknownRecord = { ...row };
  if (Object.hasOwn(normalized, "sourceWseq")) {
    if (!nonNegativeInteger(normalized.sourceWseq)) {
      throw new Error("服务端场次运行游标序号无效");
    }
    delete normalized.sourceWseq;
  }
  const parsed = parseSyncPayload("cursor", normalized);
  if (!parsed || parsed.sessionId !== expectedSessionId
      || parsed.recording !== "idle"
      || parsed.screen === "record" || parsed.screen === "paused"
      || Object.hasOwn(normalized, "rawAudioId")
      || (Object.hasOwn(normalized, "selfStart") && parsed.selfStart !== false)) {
    throw new Error("服务端场次运行游标未绑定当前场次或不是安全恢复态");
  }
  const { type: _type, ...cursor } = parsed;
  return cursor;
}

function parseRuntimeRapport(
  value: unknown,
  expectedSessionId: string,
): SessionRuntimeRapportStep | null {
  if (value === null) return null;
  const row = record(value);
  if (!row) throw new Error("服务端关系建立恢复游标响应格式无效");
  const normalized: UnknownRecord = { ...row };
  if (Object.hasOwn(normalized, "sourceWseq")) {
    if (!nonNegativeInteger(normalized.sourceWseq)) {
      throw new Error("服务端关系建立恢复游标序号无效");
    }
    delete normalized.sourceWseq;
  }
  const parsed = parseSyncPayload("rapportStep", normalized);
  if (!parsed || parsed.sessionId !== expectedSessionId
      || parsed.recording !== "idle"
      || Object.hasOwn(normalized, "rawAudioId")) {
    throw new Error("服务端关系建立恢复游标未绑定当前场次或不是安全恢复态");
  }
  const { type: _type, ...rapportStep } = parsed;
  return rapportStep;
}

function parseCompletionIssue(value: unknown): void {
  const row = record(value);
  if (!row || !exactKeys(row, ["code", "detail", "item_id", "turn_seq", "response_role"])
      || !boundedText(row.code, 100) || !boundedText(row.detail, 500)
      || !(row.item_id === null || boundedText(row.item_id, 160))
      || !(row.turn_seq === null || (nonNegativeInteger(row.turn_seq) && row.turn_seq > 0))
      || !(row.response_role === null || boundedText(row.response_role, 100))) {
    throw new Error("服务端场次完成门禁明细响应格式无效");
  }
}

function parseAssessment(
  value: unknown,
  kind: "intervention" | "completion",
): void {
  const countKey = kind === "intervention" ? "completed_attempt_turns" : "locked_turns";
  const row = record(value);
  if (row?.protocol === "rapport") {
    // 第 1 周关系建立成功回执:停在道别、录音收回、全部录音通过核验、零阻断。
    if (!exactKeys(row, [
      "ready", "protocol", "at_farewell", "recording_idle",
      "audio_total", "audio_verified", "issues",
    ])
        || row.ready !== true
        || row.at_farewell !== true
        || row.recording_idle !== true
        || !nonNegativeInteger(row.audio_total)
        || !nonNegativeInteger(row.audio_verified)
        || row.audio_verified !== row.audio_total
        || !Array.isArray(row.issues) || row.issues.length !== 0) {
      throw new Error("服务端关系建立完成门禁响应与成功状态矛盾");
    }
    return;
  }
  const keys = [
    "ready", "expected_turns", "matched_turns", countKey, "audio_evidenced_turns", "issues",
  ];
  if (!row || !exactKeys(row, keys)
      || row.ready !== true
      || !nonNegativeInteger(row.expected_turns) || row.expected_turns <= 0
      || !nonNegativeInteger(row.matched_turns)
      || !nonNegativeInteger(row[countKey])
      || !nonNegativeInteger(row.audio_evidenced_turns)
      || row.matched_turns !== row.expected_turns
      || row[countKey] !== row.expected_turns
      || row.audio_evidenced_turns !== row.expected_turns
      || !Array.isArray(row.issues) || row.issues.length !== 0) {
    throw new Error("服务端场次完成门禁响应与成功状态矛盾");
  }
  row.issues.forEach(parseCompletionIssue);
}

function assertRuntimeFacts(row: UnknownRecord, status: SessionRuntimeStatus, revision: number): void {
  const terminalTime = row.interventionCompletedAt !== null
    || row.completedAt !== null || row.abortedAt !== null;
  if (revision === 0 && status !== "active") {
    throw new Error("服务端场次运行状态与修订号矛盾");
  }
  if ((status === "active" || status === "paused") && terminalTime) {
    throw new Error("服务端场次运行状态与终态事实矛盾");
  }
  if (status === "paused" && row.pausedAt === null) {
    throw new Error("服务端未返回可核对的暂停事实");
  }
  const interventionTerminal = status === "intervention_completed" || status === "completed";
  if (interventionTerminal !== (row.interventionCompletedAt !== null)
      || interventionTerminal !== (row.interventionEndedBy !== null)) {
    throw new Error("服务端床旁干预终态事实不完整");
  }
  if ((status === "completed") !== (row.completedAt !== null)) {
    throw new Error("服务端最终完成状态与时间事实矛盾");
  }
  if ((status === "aborted") !== (row.abortedAt !== null)) {
    throw new Error("服务端中止状态与时间事实矛盾");
  }
  const requiresEndingActor = status === "completed" || status === "aborted" || status === "failed";
  if (requiresEndingActor !== (row.endedBy !== null)
      || requiresEndingActor !== (row.endReason !== null)) {
    throw new Error("服务端场次结束审计事实不完整");
  }
}

/**
 * Strict trust boundary for every server runtime response. The response must
 * be an exact, internally coherent snapshot for the path session, and any
 * recovery cursor must already be microphone-safe.
 */
export function parseSessionRuntimeState(
  value: unknown,
  expectedSessionId: string,
): SessionRuntimeState {
  if (!SAFE_SESSION_ID.test(expectedSessionId)) {
    throw new Error("当前场次标识无效，已拒绝读取运行态");
  }
  const row = record(value);
  if (!row || !exactKeys(row, RUNTIME_KEYS, RUNTIME_EXTRA_KEYS)
      || row.sessionId !== expectedSessionId
      || typeof row.status !== "string" || !RUNTIME_STATUSES.has(row.status as SessionRuntimeStatus)
      || !nonNegativeInteger(row.revision)
      || !nullableDateTime(row.pausedAt)
      || !nullableDateTime(row.resumedAt)
      || !nullableDateTime(row.interventionCompletedAt)
      || !nullableActor(row.interventionEndedBy)
      || !nullableDateTime(row.completedAt)
      || !nullableDateTime(row.abortedAt)
      || !nullableActor(row.endedBy)
      || !(row.endReason === null || boundedText(row.endReason, 500))
      || !nullableDateTime(row.updatedAt)) {
    throw new Error("服务端场次运行态响应与本系统版本不匹配，请刷新后重试");
  }
  const status = row.status as SessionRuntimeStatus;
  const revision = row.revision as number;
  assertRuntimeFacts(row, status, revision);

  if (Object.hasOwn(row, "interventionAssessment")) {
    if (status !== "intervention_completed") {
      throw new Error("床旁干预门禁回执与运行终态不一致");
    }
    parseAssessment(row.interventionAssessment, "intervention");
  }
  if (Object.hasOwn(row, "completionAssessment")) {
    if (status !== "completed") {
      throw new Error("研究完成门禁回执与运行终态不一致");
    }
    parseAssessment(row.completionAssessment, "completion");
  }

  return {
    sessionId: row.sessionId,
    status,
    revision,
    cursor: parseRuntimeCursor(row.cursor, expectedSessionId),
    rapportStep: parseRuntimeRapport(row.rapportStep, expectedSessionId),
    pausedAt: row.pausedAt as string | null,
    resumedAt: row.resumedAt as string | null,
    interventionCompletedAt: row.interventionCompletedAt as string | null,
    interventionEndedBy: row.interventionEndedBy as string | null,
    completedAt: row.completedAt as string | null,
    abortedAt: row.abortedAt as string | null,
    endedBy: row.endedBy as string | null,
    endReason: row.endReason as string | null,
    updatedAt: row.updatedAt as string | null,
  };
}
