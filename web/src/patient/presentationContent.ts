// 老人端当前呈现的网络信任边界。严格拒绝多字段响应，防止后端日后
// 误把整份题库/答案塞进这个过渡接口而前端无感接受。
import type { RapportBeat } from "../sync/messages";

export const PATIENT_FEEDBACK_KEYS = [
  "self", "cued1_unknown", "cued1_close", "cued1_silence",
  "cued2", "namefix_l", "namefix_r",
] as const;
export type PatientFeedbackKey = typeof PATIENT_FEEDBACK_KEYS[number];

export interface PatientTaskPresentation {
  schema_version: 1;
  mode: "task";
  session_id: string;
  item_bank_version_id: string;
  item_idx: number;
  turn_idx: number;
  item_ref: string;
  turn_seq: number;
  response_role: string;
  cue_level: 0 | 1 | 2 | 3;
  cue_text: string | null;
  feedback_key: PatientFeedbackKey | null;
  feedback_item_ref: string | null;
  feedback_seq: number | null;
  feedback_text: string | null;
  wseq: number | null;
}

export interface PatientRapportPresentation {
  schema_version: 1;
  mode: "rapport";
  session_id: string;
  script_version_id: string;
  section_key: string;
  question_idx: number;
  beat: RapportBeat;
  speaker: "机器人" | "研究者";
  text: string | null;
  wseq: number | null;
}

export type PatientPresentation = PatientTaskPresentation | PatientRapportPresentation;

export interface TaskPresentationExpectation {
  mode: "task";
  sessionId: string;
  itemBankVersionId: string;
  itemIdx: number;
  turnIdx: number;
  itemRef: string;
  turnSeq: number;
  responseRole: string;
  cueLevel: 0 | 1 | 2 | 3;
  feedbackKey?: PatientFeedbackKey;
  feedbackItemRef?: string;
  feedbackSeq?: number;
  wseq?: number;
}

export interface RapportPresentationExpectation {
  mode: "rapport";
  sessionId: string;
  sectionKey: string;
  questionIdx: number;
  beat: RapportBeat;
  wseq?: number;
}

export type PatientPresentationExpectation =
  | TaskPresentationExpectation
  | RapportPresentationExpectation;

type UnknownRecord = Record<string, unknown>;

const TASK_KEYS = [
  "schema_version", "mode", "session_id", "item_bank_version_id",
  "item_idx", "turn_idx", "item_ref", "turn_seq", "response_role",
  "cue_level", "cue_text", "feedback_key", "feedback_item_ref",
  "feedback_seq", "feedback_text", "wseq",
] as const;
const RAPPORT_KEYS = [
  "schema_version", "mode", "session_id", "script_version_id",
  "section_key", "question_idx", "beat", "speaker", "text", "wseq",
] as const;

function exactRecord(value: unknown, keys: readonly string[]): UnknownRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as UnknownRecord;
  const actual = Object.keys(row);
  if (actual.length !== keys.length || !keys.every((key) => Object.hasOwn(row, key))) return null;
  return row;
}

function boundedText(value: unknown, max = 10_000): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && !value.includes("\u0000") && !value.includes("\r") && !value.includes("\n");
}

function nullableText(value: unknown): value is string | null {
  return value === null || boundedText(value);
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function nullableNonNegativeInteger(value: unknown): value is number | null {
  return value === null || nonNegativeInteger(value);
}

function feedbackKey(value: unknown): value is PatientFeedbackKey {
  return PATIENT_FEEDBACK_KEYS.some((candidate) => candidate === value);
}

function itemRef(value: unknown): value is string {
  return typeof value === "string" && /^itm-[0-9]{4}$/.test(value)
    && value !== "itm-0000";
}

function parseTask(value: unknown): PatientTaskPresentation | null {
  const row = exactRecord(value, TASK_KEYS);
  if (!row || row.schema_version !== 1 || row.mode !== "task"
      || !boundedText(row.session_id, 128)
      || !boundedText(row.item_bank_version_id, 128)
      || !nonNegativeInteger(row.item_idx) || !nonNegativeInteger(row.turn_idx)
      || !itemRef(row.item_ref)
      || !nonNegativeInteger(row.turn_seq) || row.turn_seq < 1
      || !boundedText(row.response_role, 64)
      || ![0, 1, 2, 3].includes(row.cue_level as number)
      || !nullableText(row.cue_text)
      || !(row.feedback_key === null || feedbackKey(row.feedback_key))
      || !(row.feedback_item_ref === null || itemRef(row.feedback_item_ref))
      || !nullableNonNegativeInteger(row.feedback_seq)
      || !nullableText(row.feedback_text)
      || !nullableNonNegativeInteger(row.wseq)) return null;
  const feedbackPresent = [row.feedback_key, row.feedback_item_ref, row.feedback_seq]
    .map((part) => part !== null);
  if (feedbackPresent.some(Boolean) && !feedbackPresent.every(Boolean)) return null;
  if (row.feedback_key === null && row.feedback_text !== null) return null;
  return row as unknown as PatientTaskPresentation;
}

function parseRapport(value: unknown): PatientRapportPresentation | null {
  const row = exactRecord(value, RAPPORT_KEYS);
  if (!row || row.schema_version !== 1 || row.mode !== "rapport"
      || !boundedText(row.session_id, 128)
      || !boundedText(row.script_version_id, 128)
      || !boundedText(row.section_key, 100)
      || !nonNegativeInteger(row.question_idx)
      || !["ask", "reply"].includes(row.beat as string)
      || !["机器人", "研究者"].includes(row.speaker as string)
      || !nullableText(row.text)
      || !nullableNonNegativeInteger(row.wseq)) return null;
  if (row.speaker === "机器人" && row.text === null) return null;
  if (row.speaker !== "机器人" && row.text !== null) return null;
  return row as unknown as PatientRapportPresentation;
}

export function parsePatientPresentation(value: unknown): PatientPresentation {
  const mode = value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord).mode : null;
  const parsed = mode === "task" ? parseTask(value) : mode === "rapport" ? parseRapport(value) : null;
  if (!parsed) throw new Error("老人端呈现响应结构不符合最小投影契约");
  return parsed;
}

export function presentationMatches(
  presentation: PatientPresentation,
  expected: PatientPresentationExpectation,
): boolean {
  if (presentation.mode !== expected.mode || presentation.session_id !== expected.sessionId) return false;
  if (expected.wseq !== undefined && presentation.wseq !== expected.wseq) return false;
  if (presentation.mode === "rapport" && expected.mode === "rapport") {
    return presentation.section_key === expected.sectionKey
      && presentation.question_idx === expected.questionIdx
      && presentation.beat === expected.beat;
  }
  if (presentation.mode !== "task" || expected.mode !== "task") return false;
  return presentation.item_bank_version_id === expected.itemBankVersionId
    && presentation.item_idx === expected.itemIdx
    && presentation.turn_idx === expected.turnIdx
    && presentation.item_ref === expected.itemRef
    && presentation.turn_seq === expected.turnSeq
    && presentation.response_role === expected.responseRole
    && presentation.cue_level === expected.cueLevel
    && presentation.feedback_key === (expected.feedbackKey ?? null)
    && presentation.feedback_item_ref === (expected.feedbackItemRef ?? null)
    && presentation.feedback_seq === (expected.feedbackSeq ?? null);
}

export function presentationExpectationKey(expected: PatientPresentationExpectation | null): string {
  if (expected === null) return "none";
  // 期望对象只由本地强类型游标构造，固定字段顺序只用作 effect key。
  return JSON.stringify(expected);
}
