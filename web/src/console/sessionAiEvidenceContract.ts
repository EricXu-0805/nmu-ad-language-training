// 场次 journal 里两条独立的 AI operational 证据:
// - tts_serves: 只证明服务器向请求方实际返回了音频字节(或降级),不证明老人已听到。
// - confirmation_revisions: 只按 turn_id 贴到已有 Turn 的内容无关修订账本(revision/actor/时间)。
// 两者都只有 session 级/turn 级证据,没有 RuntimeCommand 投影,不能拼成跨 item/turn 的全局时间线。
type UnknownRecord = Record<string, unknown>;

const HEX64 = /^[0-9a-f]{64}$/;
const ISO_DATETIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|([+-])(\d{2}):(\d{2}))?$/;
const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;

function asRecord(value: unknown, path: string): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${path} 必须是对象`);
  }
  return value as UnknownRecord;
}

function requireExactKeys(row: UnknownRecord, allowed: ReadonlySet<string>, path: string): void {
  const keys = Object.keys(row);
  if (keys.some((key) => !allowed.has(key)) || [...allowed].some((key) => !Object.hasOwn(row, key))) {
    throw new Error(`${path} 字段集合与预期契约不符，已拒绝显示`);
  }
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function boundedNonEmptyString(value: unknown, max = 200): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max
    && value.trim() === value && !CONTROL_CHARACTER.test(value);
}

// 服务端历史行是无时区 naive datetime,新写入也不保证带时区;两种都必须是真实存在的日历时间，
// 有 offset 时也必须是真实存在的 UTC 偏移(最大 +14:00，且 14 只能对 :00)。
function validServerTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 40) return false;
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
  if (year < 1 || month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) return false;
  if (offsetHour > 14 || offsetMinute > 59 || (offsetHour === 14 && offsetMinute !== 0)) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= (days[month - 1] ?? 0);
}

export type TtsServeSource = "autopilot_command" | "live_speak" | "rapport_utterance";
export type TtsServeResult = "served" | "degraded";

export interface TtsServeEvidenceRecord {
  id: number;
  commandId: number | null;
  utteranceId: number | null;
  source: TtsServeSource;
  engineVersion: string;
  cacheHit: boolean;
  result: TtsServeResult;
  byteCount: number | null;
  textSha256: string;
  isSimulation: boolean;
  createdAt: string;
}

const TTS_SERVE_KEYS = new Set([
  "id", "session_id", "command_id", "utterance_id", "source", "engine_version",
  "cache_hit", "result", "byte_count", "text_sha256", "is_simulation",
  "created_at",
]);

function parseTtsServeEvidenceEntry(
  value: unknown,
  index: number,
  expectedSessionId: string,
  expectedIsSimulation: boolean,
): TtsServeEvidenceRecord {
  const path = `tts_serves[${index}]`;
  const row = asRecord(value, path);
  requireExactKeys(row, TTS_SERVE_KEYS, path);
  if (row.session_id !== expectedSessionId) throw new Error(`${path} 属于其他场次，已拒绝显示`);
  if (!positiveInteger(row.id)) throw new Error(`${path}.id 非法`);
  if (row.command_id !== null && !positiveInteger(row.command_id)) throw new Error(`${path}.command_id 非法`);
  if (row.utterance_id !== null && !positiveInteger(row.utterance_id)) throw new Error(`${path}.utterance_id 非法`);
  if (row.source !== "autopilot_command" && row.source !== "live_speak"
    && row.source !== "rapport_utterance") throw new Error(`${path}.source 非法`);
  if ((row.source === "autopilot_command") !== (row.command_id !== null)) {
    throw new Error(`${path} 的 source 与 command_id 绑定关系不一致`);
  }
  if ((row.source === "rapport_utterance") !== (row.utterance_id !== null)) {
    throw new Error(`${path} 的 source 与 utterance_id 绑定关系不一致`);
  }
  if (!boundedNonEmptyString(row.engine_version)) throw new Error(`${path}.engine_version 非法`);
  if (typeof row.cache_hit !== "boolean") throw new Error(`${path}.cache_hit 非法`);
  if (row.result !== "served" && row.result !== "degraded") throw new Error(`${path}.result 非法`);
  if (row.result === "served") {
    if (!positiveInteger(row.byte_count)) throw new Error(`${path} served 行必须携带已返回字节数`);
  } else {
    if (row.byte_count !== null) throw new Error(`${path} 降级行不应携带 byte_count`);
    // 降级 = 服务端没有返回音频字节；缓存命中必然意味着确实返回了缓存中的字节，两者矛盾。
    if (row.cache_hit === true) throw new Error(`${path} 降级行不应标记 cache_hit`);
  }
  if (typeof row.text_sha256 !== "string" || !HEX64.test(row.text_sha256)) {
    throw new Error(`${path}.text_sha256 必须是 64 位小写 hex`);
  }
  if (typeof row.is_simulation !== "boolean") throw new Error(`${path}.is_simulation 非法`);
  // 必须与已核验的 journal session.is_simulation 精确一致，不是随便一个布尔值。
  if (row.is_simulation !== expectedIsSimulation) {
    throw new Error(`${path}.is_simulation 与本场次不一致，已拒绝显示`);
  }
  if (!validServerTimestamp(row.created_at)) throw new Error(`${path}.created_at 非法`);
  return {
    id: row.id as number,
    commandId: row.command_id as number | null,
    utteranceId: row.utterance_id as number | null,
    source: row.source,
    engineVersion: row.engine_version,
    cacheHit: row.cache_hit,
    result: row.result,
    byteCount: row.byte_count as number | null,
    textSha256: row.text_sha256,
    isSimulation: row.is_simulation,
    createdAt: row.created_at,
  };
}

export function parseTtsServeEvidenceList(
  value: unknown,
  expectedSessionId: string,
  expectedIsSimulation: boolean,
): TtsServeEvidenceRecord[] {
  if (!Array.isArray(value)) throw new Error("tts_serves 必须是数组");
  const records = value.map((entry, index) => (
    parseTtsServeEvidenceEntry(entry, index, expectedSessionId, expectedIsSimulation)
  ));
  // 只按数据库行 id 去重——同一 command_id 完全可能真实重复朗读多次，不能拿它去重。
  const seenIds = new Set<number>();
  for (const record of records) {
    if (seenIds.has(record.id)) throw new Error(`tts_serves 存在重复的行 id #${record.id}，已拒绝显示`);
    seenIds.add(record.id);
  }
  return records;
}

export interface ConfirmationRevisionEntry {
  revision: number;
  actorDisplayId: string;
  changedAt: string;
}

// 历史迁移未回填修订账本:finalRevision===0 且 entries 为空是合法状态,
// 不代表"从未确认"——confirmed_response_text 可能早有值,只是没有可追溯履历。
export interface ConfirmationRevisionTurnRecord {
  turnId: number;
  finalRevision: number;
  entries: ConfirmationRevisionEntry[];
}

const CONFIRMATION_REVISION_KEYS = new Set(["turn_id", "revision", "actor_display_id", "changed_at"]);

export interface TurnFinalRevisionLookup {
  id: number;
  confirmation_revision: number;
}

function nonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

// 远超真实使用场景的业务上限——一个环节被人工反复改论上万次不可能真实发生。
// 没有这道闸，畸形/伪造的 confirmation_revision（例如 1e9）会让下面的连续性
// 校验循环失控放大成拒绝服务；有了这道闸，循环边界永远同时被"业务上限"与
// "调用方实际提交的账本行数"双重夹死，不能被单个巨大数字撑大。
const MAX_TRACKED_REVISIONS_PER_TURN = 100_000;

export function parseConfirmationRevisionsByTurn(
  value: unknown,
  knownTurns: ReadonlyArray<TurnFinalRevisionLookup>,
): Map<number, ConfirmationRevisionTurnRecord> {
  if (!Array.isArray(value)) throw new Error("confirmation_revisions 必须是数组");
  const finalRevisionByTurnId = new Map<number, number>();
  for (const turn of knownTurns) {
    if (!positiveInteger(turn.id)) throw new Error("turn id 非法，已拒绝显示确认修订履历");
    if (finalRevisionByTurnId.has(turn.id)) throw new Error(`turn #${turn.id} 在本场次重复出现，已拒绝显示`);
    if (!nonNegativeInteger(turn.confirmation_revision) || turn.confirmation_revision > MAX_TRACKED_REVISIONS_PER_TURN) {
      throw new Error(`turn #${turn.id} 的 confirmation_revision 非法或超出合理业务上限，已拒绝显示`);
    }
    finalRevisionByTurnId.set(turn.id, turn.confirmation_revision);
  }

  const groupedEntries = new Map<number, ConfirmationRevisionEntry[]>();
  value.forEach((entry, index) => {
    const path = `confirmation_revisions[${index}]`;
    const row = asRecord(entry, path);
    requireExactKeys(row, CONFIRMATION_REVISION_KEYS, path);
    if (!positiveInteger(row.turn_id) || !finalRevisionByTurnId.has(row.turn_id as number)) {
      throw new Error(`${path} 指向本场次未知的 turn，已拒绝显示`);
    }
    if (!positiveInteger(row.revision)) throw new Error(`${path}.revision 必须是 >=1 的整数`);
    if (!boundedNonEmptyString(row.actor_display_id)) throw new Error(`${path}.actor_display_id 非法`);
    if (!validServerTimestamp(row.changed_at)) throw new Error(`${path}.changed_at 非法`);
    const turnId = row.turn_id as number;
    const list = groupedEntries.get(turnId) ?? [];
    list.push({
      revision: row.revision as number,
      actorDisplayId: row.actor_display_id as string,
      changedAt: row.changed_at as string,
    });
    groupedEntries.set(turnId, list);
  });

  const result = new Map<number, ConfirmationRevisionTurnRecord>();
  for (const turn of knownTurns) {
    const entries = (groupedEntries.get(turn.id) ?? []).slice().sort((a, b) => a.revision - b.revision);
    if (turn.confirmation_revision === 0) {
      if (entries.length > 0) {
        throw new Error(`turn #${turn.id} 声明修订账本未回填(revision=0)，却存在账本行，结构矛盾，已拒绝显示`);
      }
      result.set(turn.id, { turnId: turn.id, finalRevision: 0, entries: [] });
      continue;
    }
    const seenRevisions = new Set<number>();
    for (const revisionEntry of entries) {
      if (seenRevisions.has(revisionEntry.revision)) {
        throw new Error(`turn #${turn.id} 的修订账本存在重复 revision，已拒绝显示`);
      }
      seenRevisions.add(revisionEntry.revision);
    }
    // 先比较行数，再做连续性枚举：枚举循环的上界必须先被"调用方实际提交的
    // 行数"卡住，绝不能只靠一个未经校验的数字驱动循环次数。
    if (entries.length !== turn.confirmation_revision) {
      throw new Error(`turn #${turn.id} 的修订账本行数与最终 revision 不一致，已拒绝显示`);
    }
    for (let expected = 1; expected <= turn.confirmation_revision; expected += 1) {
      if (!seenRevisions.has(expected)) {
        throw new Error(`turn #${turn.id} 的修订账本缺少 revision ${expected}，账本不连续，已拒绝显示`);
      }
    }
    const maxRevision = entries[entries.length - 1]!.revision;
    if (maxRevision !== turn.confirmation_revision) {
      throw new Error(`turn #${turn.id} 的账本最终 revision 与 Turn.confirmation_revision 不一致，已拒绝显示`);
    }
    result.set(turn.id, { turnId: turn.id, finalRevision: turn.confirmation_revision, entries });
  }
  return result;
}
