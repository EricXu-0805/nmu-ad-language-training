export const SESSION_CLOSEOUT_NOTE_MAX_LENGTH = 2000;

export type SessionCloseoutReportStatus = "no_additional_observation" | "observation_recorded";

// 这里只记录工作人员能够直接观察到的事实；字段名不表达原因、诊断或临床推断。
export interface SessionCloseoutObservationFlags {
  fatigue_observed: boolean;
  distress_or_discomfort_observed: boolean;
  participant_declined_to_continue: boolean;
  staff_assistance_occurred: boolean;
  environment_interruption_occurred: boolean;
  device_or_network_interruption_occurred: boolean;
}

export interface SessionCloseoutDraft extends SessionCloseoutObservationFlags {
  report_status: SessionCloseoutReportStatus | null;
  note: string;
}

export interface SessionCloseoutSaveRequest extends SessionCloseoutObservationFlags {
  expected_revision: number;
  idempotency_key: string;
  report_status: SessionCloseoutReportStatus;
  note: string | null;
}

export interface SessionCloseoutRecord extends SessionCloseoutObservationFlags {
  session_id: string;
  schema_version: string;
  revision: number;
  report_status: SessionCloseoutReportStatus;
  note: string | null;
  locked: boolean;
  recorded_by?: string | null;
  recorded_at?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
  locked_by?: string | null;
  locked_at?: string | null;
  idempotent?: boolean;
}

// 由服务端完成证据核对后返回。前端只展示这些计数，不据此推断受试者状态。
export interface SessionCloseoutOutcomeSummary {
  session_id: string;
  schema_version: string;
  generator_version: string;
  item_bank_version_id: string;
  is_simulation: boolean;
  data_classification: "research" | "simulation";
  expected_turns: number;
  matched_turns: number;
  completed_attempt_turns: number;
  audio_evidenced_turns: number;
  total_attempts?: number;
  completed_attempts?: number;
  needs_review_attempts?: number;
  technical_failure_attempts?: number;
  technical_pause_count?: number;
  researcher_takeover_count?: number;
  prompt_level_0_count?: number;
  prompt_level_1_count?: number;
  prompt_level_2_count?: number;
  prompt_level_3_count?: number;
  source_digest: string;
  generated_at: string;
}

export interface SessionCloseoutValidation {
  valid: boolean;
  errors: string[];
}

export interface SessionCloseoutEditorGateState {
  dirty: boolean;
  submitting: boolean;
  reconciliation_required: boolean;
}

export type SessionCloseoutBuildResult =
  | { ok: true; value: SessionCloseoutSaveRequest }
  | { ok: false; errors: string[] };

export const EMPTY_SESSION_CLOSEOUT_FLAGS: SessionCloseoutObservationFlags = {
  fatigue_observed: false,
  distress_or_discomfort_observed: false,
  participant_declined_to_continue: false,
  staff_assistance_occurred: false,
  environment_interruption_occurred: false,
  device_or_network_interruption_occurred: false,
};

const FLAG_KEYS = Object.keys(EMPTY_SESSION_CLOSEOUT_FLAGS) as (keyof SessionCloseoutObservationFlags)[];

export function emptySessionCloseoutDraft(): SessionCloseoutDraft {
  return {
    report_status: null,
    note: "",
    ...EMPTY_SESSION_CLOSEOUT_FLAGS,
  };
}

export function sessionCloseoutDraftFromRecord(
  record: SessionCloseoutRecord | null,
): SessionCloseoutDraft {
  if (!record) return emptySessionCloseoutDraft();
  return {
    report_status: record.report_status,
    note: record.note ?? "",
    ...pickFlags(record),
  };
}

export function sessionCloseoutDraftMatchesRecord(
  draft: SessionCloseoutDraft,
  record: SessionCloseoutRecord | null,
): boolean {
  if (!record || draft.report_status !== record.report_status) return false;
  const normalizedNote = draft.note.trim() || null;
  if (normalizedNote !== record.note) return false;
  return FLAG_KEYS.every((key) => draft[key] === record[key]);
}

export function closeoutFailureNeedsReconciliation(error: unknown): boolean {
  if (error === null || typeof error !== "object") return true;
  const status = (error as { status?: unknown }).status;
  if (typeof status !== "number") return true;
  return status === 0 || status === 408 || status === 409 || status >= 500;
}

export function hasStructuredCloseoutObservation(
  value: SessionCloseoutObservationFlags,
): boolean {
  return FLAG_KEYS.some((key) => value[key]);
}

export function validateSessionCloseoutDraft(
  draft: SessionCloseoutDraft,
): SessionCloseoutValidation {
  const errors: string[] = [];
  const note = draft.note.trim();
  if (draft.note.length > SESSION_CLOSEOUT_NOTE_MAX_LENGTH) {
    errors.push(`现场备注不能超过 ${SESSION_CLOSEOUT_NOTE_MAX_LENGTH} 字。`);
  }
  if (draft.report_status === null) {
    errors.push("请选择“无额外观察”或“已记录观察”。");
  } else if (draft.report_status === "observation_recorded"
    && !note
    && !hasStructuredCloseoutObservation(draft)) {
    errors.push("选择“已记录观察”后，请填写现场备注或至少勾选一项可观察事实。");
  }
  return { valid: errors.length === 0, errors };
}

export function buildSessionCloseoutRequest(
  draft: SessionCloseoutDraft,
  existingRevision: number | null,
  idempotencyKey: string,
): SessionCloseoutBuildResult {
  const validation = validateSessionCloseoutDraft(draft);
  const normalizedIdempotencyKey = idempotencyKey.trim();
  if (!normalizedIdempotencyKey) {
    validation.errors.push("保存意图缺少幂等键，已禁止提交。");
    validation.valid = false;
  }
  if (!validation.valid || draft.report_status === null) {
    return { ok: false, errors: validation.errors };
  }
  const noAdditionalObservation = draft.report_status === "no_additional_observation";
  return {
    ok: true,
    value: {
      expected_revision: existingRevision ?? 0,
      idempotency_key: normalizedIdempotencyKey,
      report_status: draft.report_status,
      note: noAdditionalObservation ? null : draft.note.trim() || null,
      ...(noAdditionalObservation ? EMPTY_SESSION_CLOSEOUT_FLAGS : pickFlags(draft)),
    },
  };
}

function pickFlags(value: SessionCloseoutObservationFlags): SessionCloseoutObservationFlags {
  return {
    fatigue_observed: value.fatigue_observed,
    distress_or_discomfort_observed: value.distress_or_discomfort_observed,
    participant_declined_to_continue: value.participant_declined_to_continue,
    staff_assistance_occurred: value.staff_assistance_occurred,
    environment_interruption_occurred: value.environment_interruption_occurred,
    device_or_network_interruption_occurred: value.device_or_network_interruption_occurred,
  };
}

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown, label: string): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label}不是对象`);
  }
  return value as UnknownRecord;
}

function requiredString(row: UnknownRecord, key: string): string {
  const value = row[key];
  if (typeof value !== "string" || !value.trim()) throw new Error(`${key} 缺失或非法`);
  return value;
}

function nullableString(row: UnknownRecord, key: string): string | null | undefined {
  const value = row[key];
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "string") throw new Error(`${key} 必须是字符串或 null`);
  return value;
}

function nonnegativeInteger(row: UnknownRecord, key: string, optional = false): number | undefined {
  const value = row[key];
  if (optional && value === undefined) return undefined;
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error(`${key} 必须是非负整数`);
  return value as number;
}

function requiredBoolean(row: UnknownRecord, key: string): boolean {
  const value = row[key];
  if (typeof value !== "boolean") throw new Error(`${key} 必须是布尔值`);
  return value;
}

function parseFlags(row: UnknownRecord): SessionCloseoutObservationFlags {
  return {
    fatigue_observed: requiredBoolean(row, "fatigue_observed"),
    distress_or_discomfort_observed: requiredBoolean(row, "distress_or_discomfort_observed"),
    participant_declined_to_continue: requiredBoolean(row, "participant_declined_to_continue"),
    staff_assistance_occurred: requiredBoolean(row, "staff_assistance_occurred"),
    environment_interruption_occurred: requiredBoolean(row, "environment_interruption_occurred"),
    device_or_network_interruption_occurred: requiredBoolean(row, "device_or_network_interruption_occurred"),
  };
}

export function parseSessionOutcomeSummary(
  value: unknown,
  expectedSessionId: string,
): SessionCloseoutOutcomeSummary {
  const row = asRecord(value, "场次自动汇总");
  const sessionId = requiredString(row, "session_id");
  if (sessionId !== expectedSessionId) throw new Error("自动汇总属于其他场次，已拒绝显示");
  const classification = requiredString(row, "data_classification");
  if (classification !== "research" && classification !== "simulation") {
    throw new Error("自动汇总数据分区非法");
  }
  const digest = requiredString(row, "source_digest");
  if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error("自动汇总来源摘要非法");
  const result: SessionCloseoutOutcomeSummary = {
    session_id: sessionId,
    schema_version: requiredString(row, "schema_version"),
    generator_version: requiredString(row, "generator_version"),
    item_bank_version_id: requiredString(row, "item_bank_version_id"),
    is_simulation: requiredBoolean(row, "is_simulation"),
    data_classification: classification,
    expected_turns: nonnegativeInteger(row, "expected_turns")!,
    matched_turns: nonnegativeInteger(row, "matched_turns")!,
    completed_attempt_turns: nonnegativeInteger(row, "completed_attempt_turns")!,
    audio_evidenced_turns: nonnegativeInteger(row, "audio_evidenced_turns")!,
    total_attempts: nonnegativeInteger(row, "total_attempts", true),
    completed_attempts: nonnegativeInteger(row, "completed_attempts", true),
    needs_review_attempts: nonnegativeInteger(row, "needs_review_attempts", true),
    technical_failure_attempts: nonnegativeInteger(row, "technical_failure_attempts", true),
    technical_pause_count: nonnegativeInteger(row, "technical_pause_count", true),
    researcher_takeover_count: nonnegativeInteger(row, "researcher_takeover_count", true),
    prompt_level_0_count: nonnegativeInteger(row, "prompt_level_0_count", true),
    prompt_level_1_count: nonnegativeInteger(row, "prompt_level_1_count", true),
    prompt_level_2_count: nonnegativeInteger(row, "prompt_level_2_count", true),
    prompt_level_3_count: nonnegativeInteger(row, "prompt_level_3_count", true),
    source_digest: digest,
    generated_at: requiredString(row, "generated_at"),
  };
  if (result.matched_turns > result.expected_turns
      || result.completed_attempt_turns > result.matched_turns
      || result.audio_evidenced_turns > result.matched_turns
      || result.is_simulation !== (classification === "simulation")) {
    throw new Error("自动汇总计数或数据分区互相矛盾");
  }
  return result;
}

export function parseSessionCloseoutRecord(
  value: unknown,
  expectedSessionId: string,
): SessionCloseoutRecord {
  const row = asRecord(value, "现场收尾记录");
  const sessionId = requiredString(row, "session_id");
  if (sessionId !== expectedSessionId) throw new Error("现场收尾属于其他场次，已拒绝显示");
  const reportStatus = requiredString(row, "report_status");
  if (reportStatus !== "no_additional_observation" && reportStatus !== "observation_recorded") {
    throw new Error("现场收尾状态非法");
  }
  const note = nullableString(row, "note") ?? null;
  if (note !== null && (note.length > SESSION_CLOSEOUT_NOTE_MAX_LENGTH || !note.trim())) {
    throw new Error("现场收尾备注非法");
  }
  const flags = parseFlags(row);
  const hasObservation = note !== null || hasStructuredCloseoutObservation(flags);
  if ((reportStatus === "no_additional_observation" && hasObservation)
      || (reportStatus === "observation_recorded" && !hasObservation)) {
    throw new Error("现场收尾状态与观察内容矛盾");
  }
  const locked = requiredBoolean(row, "locked");
  const lockedBy = nullableString(row, "locked_by");
  const lockedAt = nullableString(row, "locked_at");
  if (locked !== (lockedBy != null && lockedAt != null)
      || (lockedBy == null) !== (lockedAt == null)) {
    throw new Error("现场收尾锁定状态不完整");
  }
  return {
    session_id: sessionId,
    schema_version: requiredString(row, "schema_version"),
    revision: nonnegativeInteger(row, "revision")!,
    report_status: reportStatus,
    note,
    locked,
    ...flags,
    recorded_by: nullableString(row, "recorded_by"),
    recorded_at: nullableString(row, "recorded_at"),
    updated_by: nullableString(row, "updated_by"),
    updated_at: nullableString(row, "updated_at"),
    locked_by: lockedBy,
    locked_at: lockedAt,
    idempotent: row.idempotent === undefined ? undefined : requiredBoolean(row, "idempotent"),
  };
}
