// GET /sessions/{id}/ai-usage:后端场次级聚合的实际使用账本(不是从 journal 重算,
// 也不是 provider readiness/健康度、模型准确率或 autopilot 控制状态)。
type UnknownRecord = Record<string, unknown>;

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

function nonNegativeInteger(row: UnknownRecord, key: string, path: string): number {
  const value = row[key];
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${path}.${key} 必须是非负安全整数`);
  }
  return value as number;
}

const CONTROL_CHARACTER = /[\p{Cc}\p{Cf}]/u;

// 与 sessionAiEvidenceContract 的 TTS parser 口径对齐:必须 trim 后非空、原值无首尾
// 空白、无控制/格式字符——不能只查 length，否则全空格、换行、零宽字符都会被放行。
function boundedNonEmptyString(row: UnknownRecord, key: string, path: string, max = 200): string {
  const value = row[key];
  if (typeof value !== "string" || value.length === 0 || value.length > max
      || value.trim() !== value || value.trim().length === 0 || CONTROL_CHARACTER.test(value)) {
    throw new Error(`${path}.${key} 必须是非空且有界的字符串`);
  }
  return value;
}

// judge_engine_version 允许后端上报为空字符串(规则引擎无版本号),不得因此误拒绝；
// 但一旦非空，同样必须 trim 后无首尾空白、无控制/格式字符。
function boundedString(row: UnknownRecord, key: string, path: string, max = 200): string {
  const value = row[key];
  if (typeof value !== "string" || value.length > max) {
    throw new Error(`${path}.${key} 必须是有界字符串`);
  }
  if (value.length > 0 && (value.trim() !== value || CONTROL_CHARACTER.test(value))) {
    throw new Error(`${path}.${key} 非空时不得含首尾空白或控制字符`);
  }
  return value;
}

export interface SessionAiUsageTtsEngineRow {
  engineVersion: string;
  served: number;
  cacheHits: number;
  degraded: number;
}

export interface SessionAiUsageAsrEngineRow {
  engineVersion: string;
  attempts: number;
}

export interface SessionAiUsageJudgeModeRow {
  judgeMode: string;
  judgeEngineVersion: string;
  attempts: number;
}

export interface SessionAiUsageContract {
  sessionId: string;
  tts: { engines: SessionAiUsageTtsEngineRow[] };
  asr: { engines: SessionAiUsageAsrEngineRow[]; degradedAttempts: number };
  judge: { modes: SessionAiUsageJudgeModeRow[] };
}

const TOP_KEYS = new Set(["session_id", "tts", "asr", "judge"]);
const TTS_KEYS = new Set(["engines"]);
const TTS_ROW_KEYS = new Set(["engine_version", "served", "cache_hits", "degraded"]);
const ASR_KEYS = new Set(["engines", "degraded_attempts"]);
const ASR_ROW_KEYS = new Set(["engine_version", "attempts"]);
const JUDGE_KEYS = new Set(["modes"]);
const JUDGE_ROW_KEYS = new Set(["judge_mode", "judge_engine_version", "attempts"]);

export function parseSessionAiUsage(value: unknown, expectedSessionId: string): SessionAiUsageContract {
  const row = asRecord(value, "AI 使用汇总");
  requireExactKeys(row, TOP_KEYS, "AI 使用汇总");
  if (row.session_id !== expectedSessionId) throw new Error("AI 使用汇总返回了其他场次，已拒绝显示");

  const ttsRow = asRecord(row.tts, "AI 使用汇总.tts");
  requireExactKeys(ttsRow, TTS_KEYS, "AI 使用汇总.tts");
  if (!Array.isArray(ttsRow.engines)) throw new Error("AI 使用汇总.tts.engines 必须是数组");
  const seenTtsEngines = new Set<string>();
  const ttsEngines = ttsRow.engines.map((entry, index): SessionAiUsageTtsEngineRow => {
    const path = `AI 使用汇总.tts.engines[${index}]`;
    const entryRow = asRecord(entry, path);
    requireExactKeys(entryRow, TTS_ROW_KEYS, path);
    const engineVersion = boundedNonEmptyString(entryRow, "engine_version", path);
    if (seenTtsEngines.has(engineVersion)) throw new Error(`${path} 引擎重复出现`);
    seenTtsEngines.add(engineVersion);
    const served = nonNegativeInteger(entryRow, "served", path);
    const cacheHits = nonNegativeInteger(entryRow, "cache_hits", path);
    const degraded = nonNegativeInteger(entryRow, "degraded", path);
    if (cacheHits > served) throw new Error(`${path} 的 cache_hits 不能大于 served`);
    return { engineVersion, served, cacheHits, degraded };
  });

  const asrRow = asRecord(row.asr, "AI 使用汇总.asr");
  requireExactKeys(asrRow, ASR_KEYS, "AI 使用汇总.asr");
  if (!Array.isArray(asrRow.engines)) throw new Error("AI 使用汇总.asr.engines 必须是数组");
  const seenAsrEngines = new Set<string>();
  const asrEngines = asrRow.engines.map((entry, index): SessionAiUsageAsrEngineRow => {
    const path = `AI 使用汇总.asr.engines[${index}]`;
    const entryRow = asRecord(entry, path);
    requireExactKeys(entryRow, ASR_ROW_KEYS, path);
    const engineVersion = boundedNonEmptyString(entryRow, "engine_version", path);
    if (seenAsrEngines.has(engineVersion)) throw new Error(`${path} 引擎重复出现`);
    seenAsrEngines.add(engineVersion);
    const attempts = nonNegativeInteger(entryRow, "attempts", path);
    return { engineVersion, attempts };
  });
  const degradedAttempts = nonNegativeInteger(asrRow, "degraded_attempts", "AI 使用汇总.asr");

  const judgeRow = asRecord(row.judge, "AI 使用汇总.judge");
  requireExactKeys(judgeRow, JUDGE_KEYS, "AI 使用汇总.judge");
  if (!Array.isArray(judgeRow.modes)) throw new Error("AI 使用汇总.judge.modes 必须是数组");
  const seenJudgeModes = new Set<string>();
  const judgeModes = judgeRow.modes.map((entry, index): SessionAiUsageJudgeModeRow => {
    const path = `AI 使用汇总.judge.modes[${index}]`;
    const entryRow = asRecord(entry, path);
    requireExactKeys(entryRow, JUDGE_ROW_KEYS, path);
    const judgeMode = boundedNonEmptyString(entryRow, "judge_mode", path);
    const judgeEngineVersion = boundedString(entryRow, "judge_engine_version", path);
    const dedupeKey = `${judgeMode}\u0000${judgeEngineVersion}`;
    if (seenJudgeModes.has(dedupeKey)) throw new Error(`${path} 的 judge_mode/judge_engine_version 组合重复`);
    seenJudgeModes.add(dedupeKey);
    const attempts = nonNegativeInteger(entryRow, "attempts", path);
    return { judgeMode, judgeEngineVersion, attempts };
  });

  return {
    sessionId: row.session_id as string,
    tts: { engines: ttsEngines },
    asr: { engines: asrEngines, degradedAttempts },
    judge: { modes: judgeModes },
  };
}
