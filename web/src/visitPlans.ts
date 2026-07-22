import type {
  EventLine,
  PhaseType,
  Session,
  SessionRuntimeStatus,
  VisitPlanReceipt,
  VisitPlanToday,
} from "./types.ts";

type UnknownRecord = Record<string, unknown>;
const RECEIPT_KEYS = [
  "plan_id", "patient_id", "scheduled_date", "scheduled_time", "queue_order",
  "session_sitting_no", "week_no", "phase_type", "event_line",
  "item_bank_version_id", "is_simulation", "data_classification", "status",
  "revision", "created_by", "created_at", "approved_by", "approved_at",
  "started_by", "started_at", "cancelled_by", "cancelled_at", "session_id",
] as const;
const TODAY_KEYS = ["as_of_date", "plans"] as const;
const SESSION_KEYS = [
  "session_id", "patient_id", "visit_plan_id", "session_sitting_no",
  "training_date", "week_no", "phase_type", "event_line", "trainer_id",
  "item_bank_version_id", "item_bank_definition_digest",
  "autopilot_protocol_version_id", "autopilot_protocol_definition_digest",
  "is_simulation", "data_classification",
] as const;
const RECOVERY_SESSION_KEYS = [...SESSION_KEYS, "runtime_status"] as const;
const PLAN_ID = /^vp_[A-Za-z0-9_-]{24}$/;
const SESSION_ID = /^s_[A-Za-z0-9_-]{24}$/;
const PATIENT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const VERSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const LOCAL_TIME = /^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?$/;
const LOCAL_DATETIME = /^(\d{4}-\d{2}-\d{2})T((?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?)$/;
const SHA256 = /^[a-f0-9]{64}$/;
const STATUSES = new Set(["draft", "approved", "started", "cancelled"]);
const RUNTIME_STATUSES = new Set<SessionRuntimeStatus>([
  "active", "paused", "intervention_completed", "completed", "aborted", "failed",
]);
const PHASES = new Set<PhaseType>([
  "关系建立", "基线测评", "正式训练", "前测", "后测", "随访", "探针测评",
]);
const EVENT_LINES = new Set<EventLine>(["关系建立环节", "基线测评窗", "正式训练"]);

function exactRecord(value: unknown, keys: readonly string[]): UnknownRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as UnknownRecord;
  const actual = Object.keys(row);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(row, key)) ? row : null;
}

function safeActor(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 128
    && value.trim() === value && value.trim().length > 0
    && !/[\p{Cc}\p{Cf}]/u.test(value);
}

function validDate(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = DATE.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 1 || month > 12 || day < 1) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day <= (days[month - 1] ?? 0);
}

function validTime(value: unknown): value is string {
  return typeof value === "string" && LOCAL_TIME.test(value);
}

function validTimestamp(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = LOCAL_DATETIME.exec(value);
  return Boolean(match && validDate(match[1]) && validTime(match[2]));
}

function timestampMillis(value: string): number { return Date.parse(`${value}Z`); }

function pairedFact(actor: unknown, occurredAt: unknown): {
  actor: string | null; occurredAt: string | null;
} | null {
  if (actor === null && occurredAt === null) return { actor: null, occurredAt: null };
  if (!safeActor(actor) || !validTimestamp(occurredAt)) return null;
  return { actor, occurredAt };
}

function validContext(weekNo: number, phase: PhaseType, eventLine: EventLine): boolean {
  return (weekNo === 1 && phase === "关系建立" && eventLine === "关系建立环节")
    || (weekNo === 1 && (phase === "基线测评" || phase === "前测")
      && eventLine === "基线测评窗")
    || (weekNo >= 2 && weekNo <= 8 && phase === "正式训练" && eventLine === "正式训练");
}

export interface VisitPlanReceiptExpectation {
  planId?: string;
  patientId?: string;
  status?: VisitPlanReceipt["status"];
}

export function parseVisitPlanReceipt(
  value: unknown,
  expected: VisitPlanReceiptExpectation = {},
): VisitPlanReceipt {
  const row = exactRecord(value, RECEIPT_KEYS);
  if (!row
      || typeof row.plan_id !== "string" || !PLAN_ID.test(row.plan_id)
      || typeof row.patient_id !== "string" || !PATIENT_ID.test(row.patient_id)
      || !validDate(row.scheduled_date)
      || !(row.scheduled_time === null || validTime(row.scheduled_time))
      || !(row.queue_order === null || (Number.isSafeInteger(row.queue_order)
        && (row.queue_order as number) >= 0 && (row.queue_order as number) <= 100_000))
      || !Number.isSafeInteger(row.session_sitting_no)
      || (row.session_sitting_no as number) < 1 || (row.session_sitting_no as number) > 1_000
      || !Number.isSafeInteger(row.week_no) || (row.week_no as number) < 1 || (row.week_no as number) > 8
      || typeof row.phase_type !== "string" || !PHASES.has(row.phase_type as PhaseType)
      || typeof row.event_line !== "string" || !EVENT_LINES.has(row.event_line as EventLine)
      || typeof row.item_bank_version_id !== "string" || !VERSION_ID.test(row.item_bank_version_id)
      || typeof row.is_simulation !== "boolean"
      || (row.data_classification !== "research" && row.data_classification !== "simulation")
      || row.data_classification !== (row.is_simulation ? "simulation" : "research")
      || typeof row.status !== "string" || !STATUSES.has(row.status)
      || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1
      || !safeActor(row.created_by) || !validTimestamp(row.created_at)) {
    throw new Error("训练安排响应不符合严格收据契约");
  }
  const weekNo = row.week_no as number;
  const phase = row.phase_type as PhaseType;
  const eventLine = row.event_line as EventLine;
  if (!validContext(weekNo, phase, eventLine)) throw new Error("训练安排周次与事件线矛盾");

  const approved = pairedFact(row.approved_by, row.approved_at);
  const started = pairedFact(row.started_by, row.started_at);
  const cancelled = pairedFact(row.cancelled_by, row.cancelled_at);
  if (!approved || !started || !cancelled) throw new Error("训练安排审计事实不完整");
  const hasApproved = approved.actor !== null;
  const hasStarted = started.actor !== null;
  const hasCancelled = cancelled.actor !== null;
  const status = row.status as VisitPlanReceipt["status"];
  const revision = row.revision as number;
  const stateValid = status === "draft"
    ? revision === 1 && !hasApproved && !hasStarted && !hasCancelled && row.session_id === null
    : status === "approved"
      ? revision === 2 && hasApproved && !hasStarted && !hasCancelled && row.session_id === null
      : status === "started"
        ? revision === 3 && hasApproved && hasStarted && !hasCancelled
          && typeof row.session_id === "string" && SESSION_ID.test(row.session_id)
        : revision === (hasApproved ? 3 : 2) && !hasStarted && hasCancelled && row.session_id === null;
  if (!stateValid) throw new Error("训练安排状态、版本与审计事实矛盾");

  const createdMs = timestampMillis(row.created_at);
  const factTimes = [approved.occurredAt, started.occurredAt, cancelled.occurredAt]
    .filter((entry): entry is string => entry !== null);
  if (!Number.isFinite(createdMs)
      || factTimes.some((entry) => timestampMillis(entry) < createdMs)
      || (approved.occurredAt && started.occurredAt
        && timestampMillis(started.occurredAt) < timestampMillis(approved.occurredAt))
      || (approved.occurredAt && cancelled.occurredAt
        && timestampMillis(cancelled.occurredAt) < timestampMillis(approved.occurredAt))) {
    throw new Error("训练安排审计时间顺序矛盾");
  }

  const receipt: VisitPlanReceipt = {
    plan_id: row.plan_id, patient_id: row.patient_id,
    scheduled_date: row.scheduled_date, scheduled_time: row.scheduled_time as string | null,
    queue_order: row.queue_order as number | null,
    session_sitting_no: row.session_sitting_no as number, week_no: weekNo,
    phase_type: phase, event_line: eventLine,
    item_bank_version_id: row.item_bank_version_id,
    is_simulation: row.is_simulation, data_classification: row.data_classification,
    status, revision, created_by: row.created_by, created_at: row.created_at,
    approved_by: approved.actor, approved_at: approved.occurredAt,
    started_by: started.actor, started_at: started.occurredAt,
    cancelled_by: cancelled.actor, cancelled_at: cancelled.occurredAt,
    session_id: row.session_id as string | null,
  };
  if ((expected.planId !== undefined && receipt.plan_id !== expected.planId)
      || (expected.patientId !== undefined && receipt.patient_id !== expected.patientId)
      || (expected.status !== undefined && receipt.status !== expected.status)) {
    throw new Error("训练安排收据与当前请求不一致");
  }
  return receipt;
}

function queueCompare(left: VisitPlanReceipt, right: VisitPlanReceipt): number {
  if (left.scheduled_date !== right.scheduled_date) return left.scheduled_date.localeCompare(right.scheduled_date);
  if (left.scheduled_time !== right.scheduled_time) {
    if (left.scheduled_time === null) return 1;
    if (right.scheduled_time === null) return -1;
    return left.scheduled_time.localeCompare(right.scheduled_time);
  }
  if (left.queue_order !== right.queue_order) {
    if (left.queue_order === null) return 1;
    if (right.queue_order === null) return -1;
    return left.queue_order - right.queue_order;
  }
  return left.plan_id.localeCompare(right.plan_id);
}

export function parseVisitPlanToday(value: unknown): VisitPlanToday {
  const row = exactRecord(value, TODAY_KEYS);
  if (!row || !validDate(row.as_of_date) || !Array.isArray(row.plans)) {
    throw new Error("今日训练队列响应格式无效");
  }
  const plans = row.plans.map((plan) => parseVisitPlanReceipt(plan));
  const seen = new Set<string>();
  for (let index = 0; index < plans.length; index += 1) {
    const plan = plans[index];
    if (!plan || plan.status !== "approved" || plan.scheduled_date > row.as_of_date
        || seen.has(plan.plan_id)
        || (index > 0 && queueCompare(plans[index - 1] as VisitPlanReceipt, plan) > 0)) {
      throw new Error("今日训练队列包含非权威、重复或乱序安排");
    }
    seen.add(plan.plan_id);
  }
  return { as_of_date: row.as_of_date, plans };
}

export function parseVisitPlanList(value: unknown, patientId?: string): VisitPlanReceipt[] {
  if (!Array.isArray(value) || (patientId !== undefined && !PATIENT_ID.test(patientId))) {
    throw new Error("受试者训练安排列表响应格式无效");
  }
  const plans = value.map((plan) => parseVisitPlanReceipt(plan));
  const seen = new Set<string>();
  for (const plan of plans) {
    if (seen.has(plan.plan_id) || (patientId !== undefined && plan.patient_id !== patientId)) {
      throw new Error("受试者训练安排列表越界或重复");
    }
    seen.add(plan.plan_id);
  }
  return plans;
}

export function startedVisitSessionId(receipt: VisitPlanReceipt): string {
  if (receipt.status !== "started" || receipt.session_id === null || !SESSION_ID.test(receipt.session_id)) {
    throw new Error("服务端尚未返回可启动的权威场次标识");
  }
  return receipt.session_id;
}

const FROZEN_PLAN_FACTS = [
  "plan_id", "patient_id", "scheduled_date", "scheduled_time", "queue_order",
  "session_sitting_no", "week_no", "phase_type", "event_line",
  "item_bank_version_id", "is_simulation", "data_classification",
  "created_by", "created_at", "approved_by", "approved_at",
] as const satisfies readonly (keyof VisitPlanReceipt)[];

/**
 * A start response is a transition receipt, not permission to replace the
 * approved plan snapshot already shown to the operator.  Every immutable and
 * approval fact must still be byte-for-byte identical.
 */
export function reconcileStartedVisitPlan(
  approvedPlan: VisitPlanReceipt,
  startedReceipt: VisitPlanReceipt,
): VisitPlanReceipt {
  if (approvedPlan.status !== "approved" || startedReceipt.status !== "started"
      || startedReceipt.revision !== approvedPlan.revision + 1
      || FROZEN_PLAN_FACTS.some((key) => startedReceipt[key] !== approvedPlan[key])) {
    throw new Error("启动收据与已审核训练安排不一致");
  }
  startedVisitSessionId(startedReceipt);
  return startedReceipt;
}

/** True only for the exact approved snapshot that was originally actionable. */
export function isSameApprovedVisitPlan(
  approvedPlan: VisitPlanReceipt,
  candidate: VisitPlanReceipt,
): boolean {
  return approvedPlan.status === "approved"
    && candidate.status === "approved"
    && candidate.revision === approvedPlan.revision
    && FROZEN_PLAN_FACTS.every((key) => candidate[key] === approvedPlan[key]);
}

function parseSessionRecord(value: unknown, includeRuntime: boolean): Session {
  const row = exactRecord(value, includeRuntime ? RECOVERY_SESSION_KEYS : SESSION_KEYS);
  const definitionValues = [
    row?.item_bank_definition_digest,
    row?.autopilot_protocol_version_id,
    row?.autopilot_protocol_definition_digest,
  ];
  const definitionBindingAbsent = definitionValues.every((entry) => entry === null);
  const definitionBindingComplete = typeof row?.item_bank_definition_digest === "string"
    && SHA256.test(row.item_bank_definition_digest)
    && typeof row.autopilot_protocol_version_id === "string"
    && VERSION_ID.test(row.autopilot_protocol_version_id)
    && typeof row.autopilot_protocol_definition_digest === "string"
    && SHA256.test(row.autopilot_protocol_definition_digest);
  if (!row
      || typeof row.session_id !== "string" || !PATIENT_ID.test(row.session_id)
      || typeof row.patient_id !== "string" || !PATIENT_ID.test(row.patient_id)
      || !(row.visit_plan_id === null
        || (typeof row.visit_plan_id === "string" && PLAN_ID.test(row.visit_plan_id)))
      || !Number.isSafeInteger(row.session_sitting_no)
      || (row.session_sitting_no as number) < 1 || (row.session_sitting_no as number) > 1_000
      || !(row.training_date === null || validDate(row.training_date))
      || !Number.isSafeInteger(row.week_no) || (row.week_no as number) < 1 || (row.week_no as number) > 8
      || typeof row.phase_type !== "string" || !PHASES.has(row.phase_type as PhaseType)
      || typeof row.event_line !== "string" || !EVENT_LINES.has(row.event_line as EventLine)
      || !(row.trainer_id === null || safeActor(row.trainer_id))
      || typeof row.item_bank_version_id !== "string" || !VERSION_ID.test(row.item_bank_version_id)
      || (!definitionBindingAbsent && !definitionBindingComplete)
      || (row.visit_plan_id !== null && !definitionBindingComplete)
      || typeof row.is_simulation !== "boolean"
      || (row.data_classification !== "research"
        && row.data_classification !== "simulation"
        && row.data_classification !== "legacy_unknown")
      || (row.data_classification !== "legacy_unknown"
        && row.data_classification !== (row.is_simulation ? "simulation" : "research"))
      || (includeRuntime && (typeof row.runtime_status !== "string"
        || !RUNTIME_STATUSES.has(row.runtime_status as SessionRuntimeStatus)))) {
    throw new Error("训练场次响应不符合严格契约");
  }
  const weekNo = row.week_no as number;
  const phase = row.phase_type as PhaseType;
  const eventLine = row.event_line as EventLine;
  if (!validContext(weekNo, phase, eventLine)) throw new Error("训练场次周次与事件线矛盾");
  return {
    session_id: row.session_id,
    patient_id: row.patient_id,
    visit_plan_id: row.visit_plan_id as string | null,
    session_sitting_no: row.session_sitting_no as number,
    training_date: row.training_date as string | null,
    week_no: weekNo,
    phase_type: phase,
    event_line: eventLine,
    trainer_id: row.trainer_id as string | null,
    item_bank_version_id: row.item_bank_version_id,
    item_bank_definition_digest: row.item_bank_definition_digest as string | null,
    autopilot_protocol_version_id: row.autopilot_protocol_version_id as string | null,
    autopilot_protocol_definition_digest: row.autopilot_protocol_definition_digest as string | null,
    is_simulation: row.is_simulation,
    data_classification: row.data_classification,
    ...(includeRuntime
      ? { runtime_status: row.runtime_status as SessionRuntimeStatus }
      : {}),
  };
}

/**
 * Parse the server-created session and bind every available frozen fact back to
 * the exact started receipt before the UI is allowed to navigate into it.
 */
export function parseStartedVisitSession(
  value: unknown,
  startedReceipt: VisitPlanReceipt,
): Session {
  const session = parseSessionRecord(value, false);
  const sessionId = startedVisitSessionId(startedReceipt);
  if (session.session_id !== sessionId
      || session.visit_plan_id !== startedReceipt.plan_id
      || session.patient_id !== startedReceipt.patient_id
      || session.session_sitting_no !== startedReceipt.session_sitting_no
      || session.week_no !== startedReceipt.week_no
      || session.phase_type !== startedReceipt.phase_type
      || session.event_line !== startedReceipt.event_line
      || session.item_bank_version_id !== startedReceipt.item_bank_version_id
      || session.is_simulation !== startedReceipt.is_simulation
      || session.data_classification !== startedReceipt.data_classification
      || session.trainer_id !== startedReceipt.started_by
      || typeof session.training_date !== "string"
      || session.training_date < startedReceipt.scheduled_date) {
    throw new Error("服务器返回的场次与训练安排不一致，系统已阻止进入");
  }
  return session;
}

export function parsePatientSessionList(value: unknown, patientId: string): Session[] {
  if (!Array.isArray(value) || !PATIENT_ID.test(patientId)) {
    throw new Error("受试者历史场次响应格式无效");
  }
  const sessions = value.map((entry) => parseSessionRecord(entry, true));
  const seen = new Set<string>();
  for (const session of sessions) {
    if (session.patient_id !== patientId || seen.has(session.session_id)) {
      throw new Error("受试者历史场次越界或重复");
    }
    seen.add(session.session_id);
  }
  return sessions;
}

/**
 * A command key remains stable only while the caller cannot prove the command
 * completed. Once a strict server receipt is accepted, `settle` rotates the
 * next command even when its business facts are identical (for example,
 * recreating a protocol slot after a controlled cancellation).
 */
export class PendingVisitPlanCommandKeys {
  private readonly pending = new Map<string, string>();
  private readonly createKey: (scope: string) => string;

  constructor(createKey: (scope: string) => string = (scope) =>
    `vp-${scope}-${crypto.randomUUID()}`) {
    this.createKey = createKey;
  }

  acquire(scope: string, fingerprint: string): string {
    const mapKey = `${scope}:${fingerprint}`;
    const current = this.pending.get(mapKey);
    if (current) return current;
    const created = this.createKey(scope);
    this.pending.set(mapKey, created);
    return created;
  }

  settle(scope: string, fingerprint: string, acceptedKey: string): boolean {
    return this.release(scope, fingerprint, acceptedKey);
  }

  release(scope: string, fingerprint: string, resolvedKey: string): boolean {
    const mapKey = `${scope}:${fingerprint}`;
    if (this.pending.get(mapKey) !== resolvedKey) return false;
    this.pending.delete(mapKey);
    return true;
  }
}

/** Monotonic gate used to prevent a slower history request replacing a newer one. */
export class LatestVisitPlanHistoryRequest {
  private generation = 0;

  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  isLatest(token: number): boolean {
    return token === this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }
}
