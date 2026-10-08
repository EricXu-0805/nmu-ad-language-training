export const WARMUP_VERSION = "wk1-warmup-v1" as const;
export const WARMUP_TOPICS = ["weather", "food", "sleep", "activity"] as const;
export type WarmupTopic = typeof WARMUP_TOPICS[number];
export interface Week1Warmup {
  versionId: typeof WARMUP_VERSION;
  topic: WarmupTopic;
  beat: "ask" | "reply" | "done";
  seq: number;
}
export interface Week1WarmupContent {
  versionId: typeof WARMUP_VERSION;
  topics: { id: WarmupTopic; label: string; ask: string; reply: string }[];
}
export type WarmupAction = "start" | "replay" | "reply" | "end" | "continue";
export interface WarmupRequest {
  action: WarmupAction;
  topic?: WarmupTopic;
  expectedWseq: number;
  expectedRevision: number;
  idempotencyKey: string;
}
function exact(value: unknown, keys: string[]): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key)));
}
export function parseWeek1Warmup(value: unknown): Week1Warmup | null {
  if (!exact(value, ["versionId", "topic", "beat", "seq"])
    || value.versionId !== WARMUP_VERSION || !WARMUP_TOPICS.includes(value.topic as WarmupTopic)
    || !["ask", "reply", "done"].includes(value.beat as string)
    || !Number.isSafeInteger(value.seq) || Number(value.seq) < 1) return null;
  return value as unknown as Week1Warmup;
}
export function warmupIdentity(value: Week1Warmup | undefined): string | null {
  return value ? `warmup:${value.versionId}:${value.topic}:${value.beat}:${value.seq}` : null;
}
export function warmupTtsContext(sessionId: string, value: Week1Warmup): string | null {
  return value.beat === "done" ? null
    : `session:${sessionId}|plane:warmup|version:${value.versionId}|topic:${value.topic}|beat:${value.beat}|seq:${value.seq}`;
}
export function parseWeek1WarmupContent(value: unknown): Week1WarmupContent {
  const text = (s: unknown) => typeof s === "string" && s.length > 0 && s.length <= 300 && !s.includes("\r") && !s.includes("\n") && !s.includes("\0");
  if (!exact(value, ["versionId", "topics"]) || value.versionId !== WARMUP_VERSION
    || !Array.isArray(value.topics) || value.topics.length !== WARMUP_TOPICS.length
    || value.topics.some((row) => !exact(row, ["id", "label", "ask", "reply"])
      || !WARMUP_TOPICS.includes(row.id as WarmupTopic) || !text(row.label) || !text(row.ask) || !text(row.reply))
    || new Set(value.topics.map((row) => row.id)).size !== WARMUP_TOPICS.length) {
    throw new Error("训练前互动内容尚未通过校验");
  }
  return value as unknown as Week1WarmupContent;
}

export interface WarmupTargetStep {
  sectionKey: string; questionIdx: number; beat?: "ask" | "reply"; utteranceId?: number;
  recSeq?: number; paused?: boolean; wseq?: number; warmup?: Week1Warmup;
}
/** Freeze the complete position visible when a manual decision is made. */
export function warmupTargetIdentity(sessionId: string, revision: number, step: WarmupTargetStep | null | undefined): string {
  return JSON.stringify({ sessionId, revision, sectionKey: step?.sectionKey ?? null,
    questionIdx: step?.questionIdx ?? null, beat: step?.beat ?? "ask", utteranceId: step?.utteranceId ?? null,
    recSeq: step?.recSeq ?? null, paused: step?.paused ?? false, wseq: step?.wseq ?? 0,
    warmup: warmupIdentity(step?.warmup) });
}
