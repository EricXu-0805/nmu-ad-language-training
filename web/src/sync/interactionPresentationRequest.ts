import type {
  InteractionPresentationDraft,
  InteractionPresentationReceipt,
  InteractionPresentationRequest,
} from "../api.ts";
import type { InteractionEvent } from "../types.ts";
import type { SessionRuntimeState } from "../types.ts";
import { parseSyncPayload } from "./messages.ts";

const IDEMPOTENCY_KEY = /^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$/;
const RECEIPT_KEYS = [
  "interaction", "cursor", "seq", "wseq", "runtimeRevision", "idempotent",
] as const;
const INTERACTION_KEYS = [
  "id", "session_id", "event_seq", "item_id", "turn_seq", "attempt_id",
  "attempt_seq", "event_type", "payload_json", "created_at", "is_simulation",
] as const;
const ISO_DATETIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|([+-])(\d{2}):(\d{2}))?$/;

type UnknownRecord = Record<string, unknown>;

function exactRecord(value: unknown, keys: readonly string[]): UnknownRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as UnknownRecord;
  const actual = Object.keys(row);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(row, key))
    ? row : null;
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function positiveInteger(value: unknown): value is number {
  return nonNegativeInteger(value) && value > 0;
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
      || (offsetHour === 14 && offsetMinute !== 0)) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= (days[month - 1] ?? 0);
}

function exactPrimitiveRecord(actual: UnknownRecord, expected: UnknownRecord): boolean {
  const actualKeys = Object.keys(actual).sort();
  const expectedKeys = Object.keys(expected).sort();
  return actualKeys.length === expectedKeys.length
    && actualKeys.every((key, index) => key === expectedKeys[index]
      && actual[key] === expected[key]);
}

function expectedPayloadJson(
  interaction: InteractionPresentationRequest["interaction"],
): string {
  if (interaction.event_type === "cue_selected") {
    // The backend evidence ledger emits sorted, compact JSON and deliberately
    // retains cue_type=null as part of the closed cue payload contract.
    return JSON.stringify({
      cue_type: interaction.cue_type ?? null,
      prompt_level: interaction.prompt_level,
    });
  }
  return JSON.stringify({ feedback_key: interaction.feedback_key });
}

function parseInteractionEvent(
  value: unknown,
  expectedSessionId: string,
  request: InteractionPresentationRequest,
): InteractionEvent | null {
  const row = exactRecord(value, INTERACTION_KEYS);
  const expectedAttemptId = request.interaction.attempt_id ?? null;
  if (!row
      || !positiveInteger(row.id)
      || row.session_id !== expectedSessionId
      || !positiveInteger(row.event_seq)
      || row.item_id !== request.interaction.item_id
      || row.turn_seq !== request.interaction.turn_seq
      || row.attempt_id !== expectedAttemptId
      || (expectedAttemptId === null
        ? row.attempt_seq !== null
        : !positiveInteger(row.attempt_seq))
      || row.event_type !== request.interaction.event_type
      || row.payload_json !== expectedPayloadJson(request.interaction)
      || !validDateTime(row.created_at)
      || typeof row.is_simulation !== "boolean") return null;
  return {
    id: row.id,
    session_id: row.session_id,
    event_seq: row.event_seq,
    item_id: row.item_id as string,
    turn_seq: row.turn_seq as number,
    attempt_id: row.attempt_id as number | null,
    attempt_seq: row.attempt_seq as number | null,
    event_type: row.event_type as string,
    payload_json: row.payload_json as string,
    created_at: row.created_at,
    is_simulation: row.is_simulation,
  };
}

/**
 * Parse the atomic evidence+presentation receipt as an exact, request-bound
 * record.  A generic 2xx cannot authorize a bedside broadcast: the event,
 * cursor and both server clocks must prove the one immutable request/CAS edge.
 */
export function parseInteractionPresentationReceipt(
  value: unknown,
  expectedSessionId: string,
  request: InteractionPresentationRequest,
): InteractionPresentationReceipt {
  const row = exactRecord(value, RECEIPT_KEYS);
  const priorWseq = request.cursor.wseq;
  const priorRevision = request.cursor.expected_revision;
  if (!row
      || request.cursor.sessionId !== expectedSessionId
      || !nonNegativeInteger(priorWseq)
      || !nonNegativeInteger(priorRevision)
      || !positiveInteger(row.seq)
      || !positiveInteger(row.wseq)
      || (row.wseq as number) <= priorWseq
      || !positiveInteger(row.runtimeRevision)
      || row.runtimeRevision !== priorRevision + 1
      || typeof row.idempotent !== "boolean") {
    throw new Error("服务器原子交互呈现回执与本次请求不一致");
  }

  const interaction = parseInteractionEvent(row.interaction, expectedSessionId, request);
  const cursorRow = row.cursor !== null && typeof row.cursor === "object"
    && !Array.isArray(row.cursor) ? row.cursor as UnknownRecord : null;
  const parsedCursor = parseSyncPayload("cursor", row.cursor);
  if (!interaction || !cursorRow || !parsedCursor
      || parsedCursor.sessionId !== expectedSessionId
      || parsedCursor.wseq !== row.wseq) {
    throw new Error("服务器原子交互呈现回执与本次请求不一致");
  }

  const expectedCursor: UnknownRecord = {};
  for (const [key, candidate] of Object.entries(request.cursor)) {
    if (key === "expected_revision" || key === "wseq"
        || candidate === undefined || candidate === null) continue;
    expectedCursor[key] = candidate;
  }
  expectedCursor.sessionId = expectedSessionId;
  expectedCursor.wseq = row.wseq;
  if (request.interaction.event_type === "feedback_selected") {
    expectedCursor.fbSeq = row.wseq;
  }
  if (!exactPrimitiveRecord(cursorRow, expectedCursor)) {
    throw new Error("服务器原子交互呈现回执与本次请求不一致");
  }

  const { type: _type, ...parsedCursorFields } = parsedCursor;
  const cursor = { ...parsedCursorFields, wseq: row.wseq };
  return {
    interaction,
    cursor,
    seq: row.seq,
    wseq: row.wseq,
    runtimeRevision: row.runtimeRevision,
    idempotent: row.idempotent,
  };
}

export function buildExactInteractionPresentationRequest(
  draft: InteractionPresentationDraft,
  runtime: SessionRuntimeState,
  expectedSessionId: string,
): InteractionPresentationRequest {
  const wseq = runtime.cursor?.wseq;
  if (!IDEMPOTENCY_KEY.test(draft.idempotency_key)) {
    throw new Error("原子呈现幂等键无效");
  }
  if (runtime.sessionId !== expectedSessionId || draft.cursor.sessionId !== expectedSessionId) {
    throw new Error("原子呈现快照不属于当前场次");
  }
  if (runtime.status !== "active") {
    throw new Error(`场次状态为 ${runtime.status}，拒绝构造新的床旁呈现`);
  }
  if (!Number.isSafeInteger(runtime.revision) || runtime.revision < 0
      || !Number.isSafeInteger(wseq) || (wseq as number) < 0) {
    throw new Error("当前场次缺少可用于原子呈现的 revision/wseq 真值");
  }
  if (runtime.cursor?.itemIdx !== draft.cursor.itemIdx
      || runtime.cursor.turnIdx !== draft.cursor.turnIdx) {
    throw new Error("床旁位置已经变化，请先恢复当前环节再呈现提示");
  }
  return {
    ...draft,
    cursor: {
      ...draft.cursor,
      wseq: wseq as number,
      expected_revision: runtime.revision,
    },
  };
}

export function exactRequestMatchesDraft(
  exact: InteractionPresentationRequest,
  draft: InteractionPresentationDraft,
): boolean {
  const { expected_revision: _revision, wseq: _wseq, ...cursor } = exact.cursor;
  return JSON.stringify({
    idempotency_key: exact.idempotency_key,
    interaction: exact.interaction,
    cursor,
  }) === JSON.stringify(draft);
}
