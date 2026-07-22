// 操作端作业日志：localStorage 只保留当前场次严格绑定的恢复快照，
// GET /sessions/{sid}/journal 永远是已提交 item/turn/audio 和状态的研究真值。
import { useCallback, useEffect, useState } from "react";
import { turnKey } from "../lib/ids.ts";
import { journalForLocalStorage } from "../security/localSensitiveState.ts";
import type {
  AttemptEvent,
  AudioAsset,
  AudioCaptureReceipt,
  InteractionEvent,
  ItemEvent,
  Session,
  TurnEvent,
} from "../types.ts";

const JOURNAL_SCHEMA_VERSION = 2 as const;
const AUDIO_STATUSES = new Set([
  "recorded", "exported", "checksum_verified", "reliability_review_done", "deletable", "deleted",
]);
const TASK_TYPES = new Set(["单要素", "双要素", "多要素", "关系建立"]);

interface JournalItemProvenance {
  source: "server_committed";
  sessionId: string;
  itemId: string;
}

interface JournalTurnProvenance {
  source: "server_committed";
  sessionId: string;
  turnKey: string;
  rawAudioId?: string;
}

interface JournalAudioProvenance {
  source: "server_committed" | "local_pending_audio";
  sessionId: string;
  turnKey: string;
  rawAudioId: string;
}

interface JournalCueProvenance {
  source: "server_committed";
  sessionId: string;
  turnKey: string;
}

export interface JournalItem {
  itemEventId: number;
  taskType: string;
  imageId: string | null;
  provenance: JournalItemProvenance;
}

export interface JournalTurn {
  turnId: number;
  responseRole: string;
  asrSaved: boolean;
  confirmed: boolean;
  aiJudged: boolean;
  locked: boolean;
  elementValue?: number;
  promptLevel?: number;
  rawAudioId?: string;
  asrText?: string;
  confirmedText?: string;
  confirmationRevision?: number;
  sourceAttemptId?: number;
  asrConfidence?: number;
  aiAnswerType?: string;
  aiScore?: number;
  aiNeedsReview?: boolean;
  cueLevel?: number;
  provenance: JournalTurnProvenance;
}

export interface JournalAudio {
  turnKey: string;
  containsDirectIdentifier: boolean;
  isReliabilitySample: boolean;
  lastStatus: string;
  durationSeconds?: number;
  provenance: JournalAudioProvenance;
}

export interface SessionJournal {
  schemaVersion: typeof JOURNAL_SCHEMA_VERSION;
  sessionId: string;
  itemEvents: Record<string, JournalItem>;
  turns: Record<string, JournalTurn>;
  audios: Record<string, JournalAudio>;
  cueLevels: Record<string, number>;
  cueProvenance: Record<string, JournalCueProvenance>;
  cursor: { itemIdx: number; turnIdx: number };
}

export interface ServerSessionJournal {
  session: Session;
  items: ItemEvent[];
  turns: TurnEvent[];
  audios: AudioAsset[];
  interactions?: InteractionEvent[];
  attempts?: AttemptEvent[];
  audio_receipts?: AudioCaptureReceipt[];
}

// 旧后端既无 AudioAsset.turn_key、又可能缺关联 TurnEvent。不能因此把服务端音频丢掉；
// 用内部占位键保留，但绝不把它当成训练环节回放映射。
export const UNBOUND_AUDIO_TURN_KEY_PREFIX = "__unbound_audio__:";
export const isBoundJournalTurnKey = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && !value.startsWith(UNBOUND_AUDIO_TURN_KEY_PREFIX);

export const journalStorageKey = (sid: string): string => `nmu:journal:${sid}`;

export function emptySessionJournal(sid: string): SessionJournal {
  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    sessionId: sid,
    itemEvents: {},
    turns: {},
    audios: {},
    cueLevels: {},
    cueProvenance: {},
    cursor: { itemIdx: 0, turnIdx: 0 },
  };
}

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: JsonRecord, required: readonly string[], optional: readonly string[] = []): boolean {
  const keys = Object.keys(value);
  const allowed = new Set([...required, ...optional]);
  return required.every((field) => Object.prototype.hasOwnProperty.call(value, field))
    && keys.every((field) => allowed.has(field));
}

function isSafeString(value: unknown, { allowEmpty = false, max = 200 } = {}): value is string {
  return typeof value === "string" && value.length <= max && (allowEmpty || value.trim().length > 0);
}

function isSafeRecordKey(value: unknown): value is string {
  return isSafeString(value, { max: 200 })
    && value !== "__proto__"
    && value !== "prototype"
    && value !== "constructor";
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function optionalFinite(value: unknown): boolean {
  return value === undefined || isFiniteNumber(value);
}

function optionalSafeString(value: unknown, max = 200): boolean {
  return value === undefined || isSafeString(value, { allowEmpty: true, max });
}

function validTurnKey(value: unknown): value is string {
  return isSafeString(value, { max: 200 });
}

function canonicalTurnKeyParts(value: string): { itemId: string; turnSeq: number } | null {
  const separator = value.lastIndexOf("#");
  if (separator <= 0 || separator === value.length - 1) return null;
  const itemId = value.slice(0, separator);
  const rawSequence = value.slice(separator + 1);
  if (!/^\d+$/.test(rawSequence)) return null;
  const turnSeq = Number(rawSequence);
  return isSafeString(itemId, { max: 200 }) && isPositiveInteger(turnSeq)
    ? { itemId, turnSeq }
    : null;
}

function validItemProvenance(value: unknown, sid: string, itemId: string): value is JournalItemProvenance {
  return isRecord(value)
    && hasExactKeys(value, ["source", "sessionId", "itemId"])
    && value.source === "server_committed"
    && value.sessionId === sid
    && value.itemId === itemId;
}

function validTurnProvenance(
  value: unknown,
  sid: string,
  key: string,
  rawAudioId: unknown,
): value is JournalTurnProvenance {
  if (!isRecord(value) || !hasExactKeys(value, ["source", "sessionId", "turnKey"], ["rawAudioId"])) return false;
  if (value.source !== "server_committed" || value.sessionId !== sid || value.turnKey !== key) return false;
  if (rawAudioId === undefined) return value.rawAudioId === undefined;
  return value.rawAudioId === rawAudioId;
}

function validAudioProvenance(
  value: unknown,
  sid: string,
  turnK: string,
  rawAudioId: string,
): value is JournalAudioProvenance {
  return isRecord(value)
    && hasExactKeys(value, ["source", "sessionId", "turnKey", "rawAudioId"])
    && (value.source === "server_committed" || value.source === "local_pending_audio")
    && value.sessionId === sid
    && value.turnKey === turnK
    && value.rawAudioId === rawAudioId;
}

function validCueProvenance(value: unknown, sid: string, turnK: string): value is JournalCueProvenance {
  return isRecord(value)
    && hasExactKeys(value, ["source", "sessionId", "turnKey"])
    && value.source === "server_committed"
    && value.sessionId === sid
    && value.turnKey === turnK;
}

function parseItem(value: unknown, sid: string, itemId: string): JournalItem | null {
  if (!isRecord(value) || !hasExactKeys(value, ["itemEventId", "taskType", "imageId", "provenance"])) return null;
  if (!isPositiveInteger(value.itemEventId) || !TASK_TYPES.has(String(value.taskType))) return null;
  if (!(value.imageId === null || isSafeString(value.imageId, { allowEmpty: true, max: 500 }))) return null;
  if (!validItemProvenance(value.provenance, sid, itemId)) return null;
  return value as unknown as JournalItem;
}

function parseTurn(value: unknown, sid: string, key: string): JournalTurn | null {
  const required = ["turnId", "responseRole", "asrSaved", "confirmed", "aiJudged", "locked", "provenance"];
  const optional = [
    "elementValue", "promptLevel", "rawAudioId", "sourceAttemptId", "asrConfidence",
    "aiAnswerType", "aiScore", "aiNeedsReview", "cueLevel", "confirmationRevision",
  ];
  if (canonicalTurnKeyParts(key) === null || !isRecord(value) || !hasExactKeys(value, required, optional)) return null;
  if (!isPositiveInteger(value.turnId) || !isSafeString(value.responseRole, { allowEmpty: true, max: 200 })) return null;
  if ([value.asrSaved, value.confirmed, value.aiJudged, value.locked].some((entry) => typeof entry !== "boolean")) return null;
  if (!optionalFinite(value.elementValue) || !optionalFinite(value.asrConfidence) || !optionalFinite(value.aiScore)) return null;
  if (value.promptLevel !== undefined && (!isNonNegativeInteger(value.promptLevel) || value.promptLevel > 3)) return null;
  if (value.cueLevel !== undefined && (!isNonNegativeInteger(value.cueLevel) || value.cueLevel > 3)) return null;
  if (value.sourceAttemptId !== undefined && !isPositiveInteger(value.sourceAttemptId)) return null;
  if (value.confirmationRevision !== undefined
      && (typeof value.confirmationRevision !== "number"
        || !Number.isInteger(value.confirmationRevision)
        || value.confirmationRevision < 0)) return null;
  if (!optionalSafeString(value.rawAudioId) || !optionalSafeString(value.aiAnswerType)) return null;
  if (value.aiNeedsReview !== undefined && typeof value.aiNeedsReview !== "boolean") return null;
  if (!validTurnProvenance(value.provenance, sid, key, value.rawAudioId)) return null;
  return value as unknown as JournalTurn;
}

function parseAudio(value: unknown, sid: string, rawAudioId: string): JournalAudio | null {
  const required = ["turnKey", "containsDirectIdentifier", "isReliabilitySample", "lastStatus", "provenance"];
  if (!isRecord(value) || !hasExactKeys(value, required, ["durationSeconds"])) return null;
  if (!validTurnKey(value.turnKey) || !AUDIO_STATUSES.has(String(value.lastStatus))) return null;
  if (typeof value.containsDirectIdentifier !== "boolean" || typeof value.isReliabilitySample !== "boolean") return null;
  if (value.durationSeconds !== undefined && (!isFiniteNumber(value.durationSeconds) || value.durationSeconds < 0)) return null;
  if (!validAudioProvenance(value.provenance, sid, value.turnKey, rawAudioId)) return null;
  if ((value.provenance as JournalAudioProvenance).source === "local_pending_audio"
      && (value.lastStatus !== "recorded" || !isBoundJournalTurnKey(value.turnKey))) return null;
  return value as unknown as JournalAudio;
}

function parseRecord<T>(
  value: unknown,
  parseEntry: (entry: unknown, key: string) => T | null,
): Record<string, T> | null {
  if (!isRecord(value)) return null;
  const parsed: Record<string, T> = {};
  for (const [entryKey, entry] of Object.entries(value)) {
    if (!isSafeRecordKey(entryKey)) return null;
    const next = parseEntry(entry, entryKey);
    if (next === null) return null;
    parsed[entryKey] = next;
  }
  return parsed;
}

/**
 * Parse a persisted journal without trusting a TypeScript assertion. Any unknown
 * field, incomplete nested object, or provenance mismatch rejects the complete
 * snapshot; callers must then recover from the server journal.
 */
export function parseStoredSessionJournal(raw: string, requestedSessionId: string): SessionJournal | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(value) || !hasExactKeys(value, [
    "schemaVersion", "sessionId", "itemEvents", "turns", "audios",
    "cueLevels", "cueProvenance", "cursor",
  ])) return null;
  if (value.schemaVersion !== JOURNAL_SCHEMA_VERSION || value.sessionId !== requestedSessionId) return null;

  const items = parseRecord(value.itemEvents, (entry, itemId) => parseItem(entry, requestedSessionId, itemId));
  const turns = parseRecord(value.turns, (entry, key) => parseTurn(entry, requestedSessionId, key));
  const audios = parseRecord(value.audios, (entry, rawAudioId) => parseAudio(entry, requestedSessionId, rawAudioId));
  if (items === null || turns === null || audios === null) return null;
  const itemEventIds = new Set<number>();
  for (const item of Object.values(items)) {
    if (itemEventIds.has(item.itemEventId)) return null;
    itemEventIds.add(item.itemEventId);
  }
  const turnIds = new Set<number>();
  const turnAudioIds = new Set<string>();
  for (const [key, turn] of Object.entries(turns)) {
    const parts = canonicalTurnKeyParts(key);
    if (parts === null || !Object.prototype.hasOwnProperty.call(items, parts.itemId)
        || turnIds.has(turn.turnId)) return null;
    turnIds.add(turn.turnId);
    if (turn.rawAudioId !== undefined) {
      const linkedAudio = audios[turn.rawAudioId];
      if (turnAudioIds.has(turn.rawAudioId)
          || linkedAudio?.turnKey !== key
          || linkedAudio.provenance.source !== "server_committed") return null;
      turnAudioIds.add(turn.rawAudioId);
    }
  }

  if (!isRecord(value.cueLevels) || !isRecord(value.cueProvenance)) return null;
  const cueLevels: Record<string, number> = {};
  const cueProvenance: Record<string, JournalCueProvenance> = {};
  if (Object.keys(value.cueLevels).length !== Object.keys(value.cueProvenance).length) return null;
  for (const [key, level] of Object.entries(value.cueLevels)) {
    if (!validTurnKey(key) || !isNonNegativeInteger(level) || level > 3) return null;
    const provenance = value.cueProvenance[key];
    if (!validCueProvenance(provenance, requestedSessionId, key)) return null;
    cueLevels[key] = level;
    cueProvenance[key] = provenance;
  }
  if (Object.keys(value.cueProvenance).some((key) => !(key in cueLevels))) return null;

  if (!isRecord(value.cursor) || !hasExactKeys(value.cursor, ["itemIdx", "turnIdx"])) return null;
  if (!isNonNegativeInteger(value.cursor.itemIdx) || !isNonNegativeInteger(value.cursor.turnIdx)) return null;

  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    sessionId: requestedSessionId,
    itemEvents: items,
    turns,
    audios,
    cueLevels,
    cueProvenance,
    cursor: { itemIdx: value.cursor.itemIdx, turnIdx: value.cursor.turnIdx },
  };
}

type JournalStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function loadSessionJournal(sid: string, storage: JournalStorage = localStorage): SessionJournal {
  const storageKey = journalStorageKey(sid);
  try {
    const raw = storage.getItem(storageKey);
    if (raw === null) return emptySessionJournal(sid);
    const parsed = parseStoredSessionJournal(raw, sid);
    if (parsed !== null) return parsed;
  } catch {
    // 私隐模式或损坏快照与伪造快照同样 fail closed。
  }
  try { storage.removeItem(storageKey); } catch { /* 不得影响服务端恢复 */ }
  return emptySessionJournal(sid);
}

function store(journal: SessionJournal, expectedSessionId: string, storage: JournalStorage = localStorage): void {
  if (journal.sessionId !== expectedSessionId) {
    try { storage.removeItem(journalStorageKey(expectedSessionId)); } catch { /* fail closed */ }
    throw new Error("场次 journal 与当前受试者不匹配，已拒绝保存");
  }
  // 回答文本只从服务端证据账本恢复；Web Storage 不保留原文。
  const safe = journalForLocalStorage(journal);
  const serialized = JSON.stringify(safe);
  if (parseStoredSessionJournal(serialized, expectedSessionId) === null) {
    try { storage.removeItem(journalStorageKey(expectedSessionId)); } catch { /* fail closed */ }
    throw new Error("场次 journal 结构不安全，已拒绝保存");
  }
  storage.setItem(journalStorageKey(expectedSessionId), serialized);
}

function assertServerJournalScope(remote: ServerSessionJournal, expectedSessionId: string): void {
  if (remote.session?.session_id !== expectedSessionId) throw new Error("服务端返回了其他场次的 journal");
  if (!Array.isArray(remote.items) || !Array.isArray(remote.turns) || !Array.isArray(remote.audios)) {
    throw new Error("服务端 journal 结构不完整");
  }
  const itemIds = new Set<number>();
  for (const item of remote.items) {
    if (item.session_id !== expectedSessionId || !isPositiveInteger(item.id) || itemIds.has(item.id)
        || !isSafeRecordKey(item.item_id)) {
      throw new Error("服务端 item 引用不属于当前场次");
    }
    itemIds.add(item.id);
  }
  const turnIds = new Set<number>();
  for (const turn of remote.turns) {
    if (!isPositiveInteger(turn.id) || turnIds.has(turn.id) || !itemIds.has(turn.item_event_id)) {
      throw new Error("服务端 turn 引用不属于当前场次");
    }
    turnIds.add(turn.id);
  }
  const audioIds = new Set<string>();
  for (const audio of remote.audios) {
    if (!isSafeRecordKey(audio.raw_audio_id) || audioIds.has(audio.raw_audio_id)
        || (audio.session_id != null && audio.session_id !== expectedSessionId)) {
      throw new Error("服务端 audio 引用不属于当前场次");
    }
    audioIds.add(audio.raw_audio_id);
  }
  for (const interaction of remote.interactions ?? []) {
    if (interaction.session_id !== expectedSessionId) throw new Error("服务端 interaction 串入了其他场次");
  }
  for (const attempt of remote.attempts ?? []) {
    if (attempt.session_id !== expectedSessionId) throw new Error("服务端 attempt 串入了其他场次");
  }
  for (const receipt of remote.audio_receipts ?? []) {
    if (receipt.session_id !== expectedSessionId) throw new Error("服务端 audio receipt 串入了其他场次");
  }
}

function cueLevelFromInteraction(interaction: InteractionEvent): number | null {
  if (interaction.event_type !== "cue_selected" || !interaction.item_id || !isPositiveInteger(interaction.turn_seq)) return null;
  let payload: unknown;
  try { payload = JSON.parse(interaction.payload_json); } catch { return null; }
  if (!isRecord(payload)) return null;
  const level = payload.prompt_level;
  return isPositiveInteger(level) && level <= 3 ? level : null;
}

/** Merge a server snapshot without letting local completion claims elevate research truth. */
export function mergeServerJournal(local: SessionJournal, remote: ServerSessionJournal): SessionJournal {
  const sid = remote.session?.session_id;
  if (!isSafeString(sid) || local.sessionId !== sid) throw new Error("场次 journal 合并边界不一致");
  assertServerJournalScope(remote, sid);

  const items: Record<string, JournalItem> = {};
  const itemByDbId = new Map<number, ItemEvent>();
  for (const item of remote.items) {
    itemByDbId.set(item.id, item);
    items[item.item_id] = {
      itemEventId: item.id,
      taskType: item.task_type,
      imageId: item.image_id ?? null,
      provenance: { source: "server_committed", sessionId: sid, itemId: item.item_id },
    };
  }

  const remoteAudioIds = new Set(remote.audios.map((audio) => audio.raw_audio_id));
  const knownServerAudioIds = new Set(remoteAudioIds);
  for (const receipt of remote.audio_receipts ?? []) knownServerAudioIds.add(receipt.raw_audio_id);
  for (const attempt of remote.attempts ?? []) knownServerAudioIds.add(attempt.raw_audio_id);
  for (const turn of remote.turns) if (turn.raw_audio_id) knownServerAudioIds.add(turn.raw_audio_id);

  const turns: Record<string, JournalTurn> = {};
  const remoteTurnKey = new Map<number, string>();
  const turnByAudioId = new Map<string, TurnEvent>();
  for (const turn of remote.turns) {
    if (!Number.isInteger(turn.confirmation_revision)
        || turn.confirmation_revision < 0) {
      throw new Error("服务端 turn 缺少合法的 confirmation_revision，禁止构造 CAS 真值");
    }
    const item = itemByDbId.get(turn.item_event_id);
    if (!item) throw new Error("服务端 turn 缺少当前场次 item");
    const k = turnKey(item.item_id, turn.turn_seq);
    remoteTurnKey.set(turn.id, k);
    if (turn.raw_audio_id) {
      if (turnByAudioId.has(turn.raw_audio_id)) throw new Error("同一服务端音频被多个 turn 引用");
      turnByAudioId.set(turn.raw_audio_id, turn);
    }
    const rawAudioId = turn.raw_audio_id ?? undefined;
    const promptLevel = turn.prompt_level ?? undefined;
    const confirmedText = turn.confirmed_response_text ?? undefined;
    turns[k] = {
      turnId: turn.id,
      responseRole: turn.response_role ?? "",
      asrSaved: true,
      confirmed: Boolean(confirmedText?.trim()),
      aiJudged: turn.ai_answer_type != null || turn.ai_score != null,
      locked: turn.score_locked === true,
      elementValue: turn.element_value ?? undefined,
      promptLevel,
      rawAudioId,
      asrText: turn.asr_text ?? undefined,
      confirmedText,
      confirmationRevision: turn.confirmation_revision,
      sourceAttemptId: turn.source_attempt_id ?? undefined,
      asrConfidence: turn.asr_confidence ?? undefined,
      aiAnswerType: turn.ai_answer_type ?? undefined,
      aiScore: turn.ai_score ?? undefined,
      aiNeedsReview: turn.ai_needs_review ?? undefined,
      cueLevel: promptLevel ?? 0,
      provenance: {
        source: "server_committed",
        sessionId: sid,
        turnKey: k,
        ...(rawAudioId ? { rawAudioId } : {}),
      },
    };
  }

  const audios: Record<string, JournalAudio> = {};
  for (const audio of remote.audios) {
    const linkedTurn = turnByAudioId.get(audio.raw_audio_id);
    const derivedTurnKey = linkedTurn ? remoteTurnKey.get(linkedTurn.id) : undefined;
    const suppliedTurnKey = typeof audio.turn_key === "string" && audio.turn_key.trim()
      ? audio.turn_key.trim()
      : undefined;
    if (derivedTurnKey && suppliedTurnKey && derivedTurnKey !== suppliedTurnKey) {
      throw new Error("服务端 audio 与 turn 的环节引用冲突");
    }
    const k = derivedTurnKey ?? suppliedTurnKey ?? `${UNBOUND_AUDIO_TURN_KEY_PREFIX}${audio.raw_audio_id}`;
    audios[audio.raw_audio_id] = {
      turnKey: k,
      containsDirectIdentifier: audio.contains_direct_identifier,
      isReliabilitySample: audio.is_reliability_sample,
      lastStatus: audio.status,
      durationSeconds: linkedTurn?.duration_seconds ?? undefined,
      provenance: {
        source: "server_committed",
        sessionId: sid,
        turnKey: k,
        rawAudioId: audio.raw_audio_id,
      },
    };
  }

  // 本地已提交/服务端来源的引用若不在新快照中必须丢弃。唯一可保留的是：
  // 当前场次 + 当前 turnKey + 当前 rawAudioId 三者严格绑定，且服务端没有任何已上传证据的录音。
  for (const [rawAudioId, audio] of Object.entries(local.audios)) {
    if (Object.prototype.hasOwnProperty.call(audios, rawAudioId) || knownServerAudioIds.has(rawAudioId)) continue;
    if (audio.provenance.source !== "local_pending_audio"
        || audio.provenance.sessionId !== sid
        || audio.provenance.rawAudioId !== rawAudioId
        || audio.provenance.turnKey !== audio.turnKey
        || audio.lastStatus !== "recorded"
        || !isBoundJournalTurnKey(audio.turnKey)) continue;
    audios[rawAudioId] = audio;
  }

  const cueLevels: Record<string, number> = {};
  const cueProvenance: Record<string, JournalCueProvenance> = {};
  const recordServerCue = (k: string, level: number) => {
    if (!isNonNegativeInteger(level) || level > 3 || level <= (cueLevels[k] ?? 0)) return;
    cueLevels[k] = level;
    cueProvenance[k] = { source: "server_committed", sessionId: sid, turnKey: k };
  };
  for (const [k, turn] of Object.entries(turns)) recordServerCue(k, Math.max(turn.cueLevel ?? 0, turn.promptLevel ?? 0));
  for (const interaction of remote.interactions ?? []) {
    const level = cueLevelFromInteraction(interaction);
    if (level != null && interaction.item_id && interaction.turn_seq != null) {
      recordServerCue(turnKey(interaction.item_id, interaction.turn_seq), level);
    }
  }

  return {
    schemaVersion: JOURNAL_SCHEMA_VERSION,
    sessionId: sid,
    itemEvents: items,
    turns,
    audios,
    cueLevels,
    cueProvenance,
    cursor: local.cursor,
  };
}

function currentJournal(journal: SessionJournal, sessionId: string): SessionJournal {
  return journal.sessionId === sessionId ? journal : loadSessionJournal(sessionId);
}

export function useSessionJournal(sessionId: string) {
  const [journal, setJournal] = useState<SessionJournal>(() => loadSessionJournal(sessionId));

  useEffect(() => { setJournal(loadSessionJournal(sessionId)); }, [sessionId]);

  const persist = useCallback((next: SessionJournal) => {
    if (next.sessionId !== sessionId) {
      try { localStorage.removeItem(journalStorageKey(sessionId)); } catch { /* fail closed */ }
      setJournal(emptySessionJournal(sessionId));
      return;
    }
    store(next, sessionId);
    setJournal(next);
  }, [sessionId]);

  const upsertItem = useCallback((itemId: string, v: Omit<JournalItem, "provenance">) => {
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      const item: JournalItem = {
        ...v,
        provenance: { source: "server_committed", sessionId, itemId },
      };
      const n = { ...j, itemEvents: { ...j.itemEvents, [itemId]: item } };
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  const upsertTurn = useCallback((
    itemId: string,
    turnSeq: number,
    patch: Omit<Partial<JournalTurn>, "provenance"> & Pick<JournalTurn, "turnId" | "responseRole">,
  ) => {
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      const k = turnKey(itemId, turnSeq);
      const prev = j.turns[k] ?? {
        turnId: patch.turnId,
        responseRole: patch.responseRole,
        asrSaved: false,
        confirmed: false,
        aiJudged: false,
        locked: false,
        provenance: { source: "server_committed" as const, sessionId, turnKey: k },
      };
      const value = { ...prev, ...patch };
      const nextTurn: JournalTurn = {
        ...value,
        provenance: {
          source: "server_committed",
          sessionId,
          turnKey: k,
          ...(value.rawAudioId ? { rawAudioId: value.rawAudioId } : {}),
        },
      };
      const n = { ...j, turns: { ...j.turns, [k]: nextTurn } };
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  const upsertAudio = useCallback((rawAudioId: string, v: Omit<JournalAudio, "provenance">) => {
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      // 迟到的本地 audioSaved 不得降级已有服务端状态。
      if (j.audios[rawAudioId]?.provenance.source === "server_committed") return j;
      const audio: JournalAudio = {
        ...v,
        provenance: {
          source: "local_pending_audio",
          sessionId,
          turnKey: v.turnKey,
          rawAudioId,
        },
      };
      const n = { ...j, audios: { ...j.audios, [rawAudioId]: audio } };
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  const recordCueLevel = useCallback((turnK: string, level: number) => {
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      const n = {
        ...j,
        cueLevels: { ...j.cueLevels, [turnK]: level },
        cueProvenance: {
          ...j.cueProvenance,
          [turnK]: { source: "server_committed" as const, sessionId, turnKey: turnK },
        },
      };
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  const setCursor = useCallback((itemIdx: number, turnIdx: number) => {
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      const n = { ...j, cursor: { itemIdx, turnIdx } };
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  const hydrateFromServer = useCallback((remote: ServerSessionJournal) => {
    assertServerJournalScope(remote, sessionId);
    setJournal((previous) => {
      const j = currentJournal(previous, sessionId);
      const n = mergeServerJournal(j, remote);
      store(n, sessionId);
      return n;
    });
  }, [sessionId]);

  return { journal, persist, upsertItem, upsertTurn, upsertAudio, recordCueLevel, setCursor, hydrateFromServer };
}
