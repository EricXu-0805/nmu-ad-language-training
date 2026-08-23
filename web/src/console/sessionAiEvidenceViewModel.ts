import type { ConfirmationRevisionTurnRecord, TtsServeEvidenceRecord } from "./sessionAiEvidenceContract";
import type { SessionAiUsageContract } from "./sessionAiUsageContract";

const EMPTY_NOTICE = "当前汇总未记录实际使用证据";

function formatTime(value: string): string {
  return value.replace("T", " ").slice(0, 19);
}

// ---- TTS 服务端实际返回/降级证据(场次级) ----

export interface TtsServeEvidenceRowViewModel {
  key: string;
  time: string;
  sourceLabel: string;
  commandLabel: string;
  engineVersion: string;
  resultLabel: string;
  resultTone: "ok" | "warn";
  cacheLabel: string;
  byteLabel: string;
  isSimulation: boolean;
}

export interface TtsServeEvidenceSummaryViewModel {
  served: number;
  degraded: number;
  cacheHits: number;
}

export type TtsServeEvidenceSectionViewModel =
  | { status: "withdrawn" }
  | { status: "contract-error"; message: string }
  | { status: "empty"; notice: string }
  | {
    status: "ready";
    summary: TtsServeEvidenceSummaryViewModel;
    rows: TtsServeEvidenceRowViewModel[];
  };

function buildTtsServeRow(record: TtsServeEvidenceRecord): TtsServeEvidenceRowViewModel {
  return {
    key: `tts-${record.id}`,
    time: formatTime(record.createdAt),
    sourceLabel: record.source === "autopilot_command" ? "自动驾驶指令" : "现场朗读",
    commandLabel: record.commandId != null ? `指令 #${record.commandId}` : "无绑定指令",
    engineVersion: record.engineVersion,
    resultLabel: record.result === "served" ? "已实际返回音频" : "已改用本机语音(服务器未返回音频)",
    resultTone: record.result === "served" ? "ok" : "warn",
    // 降级行没有返回任何字节，缓存命中/未命中的说法不适用，不能写成"未命中"。
    cacheLabel: record.result === "degraded" ? "缓存不适用(服务器未返回音频)" : record.cacheHit ? "缓存命中" : "缓存未命中",
    byteLabel: record.byteCount != null ? `${record.byteCount} 字节` : "—",
    isSimulation: record.isSimulation,
  };
}

export function buildTtsServeEvidenceSection(
  records: TtsServeEvidenceRecord[],
): TtsServeEvidenceSectionViewModel {
  if (records.length === 0) return { status: "empty", notice: EMPTY_NOTICE };
  const summary = records.reduce<TtsServeEvidenceSummaryViewModel>((acc, record) => {
    if (record.result === "served") {
      acc.served += 1;
      if (record.cacheHit) acc.cacheHits += 1;
    } else {
      acc.degraded += 1;
    }
    return acc;
  }, { served: 0, degraded: 0, cacheHits: 0 });
  const rows = records
    .slice()
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt) || a.id - b.id)
    .map(buildTtsServeRow);
  return { status: "ready", summary, rows };
}

// ---- 每个 turn 的确认修订历史(内容无关) ----

export interface ConfirmationRevisionEntryViewModel {
  key: string;
  revision: number;
  actorDisplayId: string;
  time: string;
}

export type ConfirmationRevisionTurnViewModel =
  | { turnId: number; status: "no_ledger"; notice: string }
  | { turnId: number; status: "tracked"; finalRevision: number; entries: ConfirmationRevisionEntryViewModel[] };

export function buildConfirmationRevisionTurnViewModel(
  record: ConfirmationRevisionTurnRecord,
  hasConfirmedText: boolean,
): ConfirmationRevisionTurnViewModel {
  if (record.finalRevision === 0) {
    // 中性表述:revision=0 只说明账本没有可追溯履历,不能断言"从未确认"——
    // 历史迁移未回填时，confirmed_response_text 完全可能早已有值。
    return {
      turnId: record.turnId,
      status: "no_ledger",
      notice: hasConfirmedText
        ? "已存在确认文本，但无可追溯修订履历（历史兼容记录，修订记录未补录）"
        : "当前无可追溯修订履历，可能为历史兼容记录",
    };
  }
  return {
    turnId: record.turnId,
    status: "tracked",
    finalRevision: record.finalRevision,
    entries: record.entries.map((entry) => ({
      key: `turn-${record.turnId}-rev-${entry.revision}`,
      revision: entry.revision,
      actorDisplayId: entry.actorDisplayId,
      time: formatTime(entry.changedAt),
    })),
  };
}

export type ConfirmationRevisionSectionViewModel =
  | { status: "withdrawn" }
  | { status: "contract-error"; message: string }
  | { status: "ready"; turns: ConfirmationRevisionTurnViewModel[] };

export function buildConfirmationRevisionSection(
  byTurn: Map<number, ConfirmationRevisionTurnRecord>,
  hasConfirmedTextByTurnId: ReadonlyMap<number, boolean>,
): ConfirmationRevisionSectionViewModel {
  const turns = [...byTurn.values()]
    .sort((a, b) => a.turnId - b.turnId)
    .map((record) => buildConfirmationRevisionTurnViewModel(record, hasConfirmedTextByTurnId.get(record.turnId) ?? false));
  return { status: "ready", turns };
}

// ---- 场次级 AI 实际使用汇总(TTS / ASR / judge 分开) ----

export interface AiUsageRowViewModel {
  key: string;
  label: string;
  detail: string;
}

export interface AiUsageSectionViewModel {
  hasAnyRecords: boolean;
  ttsRows: AiUsageRowViewModel[];
  asrRows: AiUsageRowViewModel[];
  judgeRows: AiUsageRowViewModel[];
}

export type AiUsageViewModel =
  | { status: "loading" }
  | { status: "forbidden" }
  | { status: "withdrawn" }
  | { status: "network-error"; message: string }
  | { status: "contract-error"; message: string }
  | { status: "ready"; model: AiUsageSectionViewModel };

export function buildAiUsageSection(contract: SessionAiUsageContract): AiUsageSectionViewModel {
  const ttsRows = contract.tts.engines.map((engine): AiUsageRowViewModel => ({
    key: `tts-${engine.engineVersion}`,
    label: engine.engineVersion,
    detail: `实际返回 ${engine.served} 次(缓存命中 ${engine.cacheHits}) · 改用本机语音 ${engine.degraded} 次`,
  }));
  const asrRows: AiUsageRowViewModel[] = contract.asr.engines.map((engine) => ({
    key: `asr-${engine.engineVersion}`,
    label: engine.engineVersion,
    detail: `尝试 ${engine.attempts} 次`,
  }));
  if (contract.asr.degradedAttempts > 0 || asrRows.length > 0) {
    asrRows.push({
      key: "asr-degraded",
      label: "ASR 改用本机识别",
      detail: `${contract.asr.degradedAttempts} 次尝试改用本机识别`,
    });
  }
  const judgeRows = contract.judge.modes.map((mode): AiUsageRowViewModel => ({
    key: `judge-${mode.judgeMode}-${mode.judgeEngineVersion}`,
    label: mode.judgeEngineVersion ? `${mode.judgeMode} · ${mode.judgeEngineVersion}` : mode.judgeMode,
    detail: `${mode.attempts} 次`,
  }));
  const hasAnyRecords = contract.tts.engines.length > 0
    || contract.asr.engines.length > 0
    || contract.asr.degradedAttempts > 0
    || contract.judge.modes.length > 0;
  return { hasAnyRecords, ttsRows, asrRows, judgeRows };
}

export { EMPTY_NOTICE as sessionAiEvidenceEmptyNotice };
