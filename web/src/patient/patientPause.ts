const STORAGE_KEY = "nmu:patient-pause:v1";
export const PATIENT_PAUSE_STORAGE_LOCK_NAME = "nmu-patient-pause-storage:v1";
const IDEMPOTENCY_PATTERN = /^patient_pause:[0-9a-f]{32}$/;
export const PATIENT_PAUSE_OUTBOX_TTL_MS = 24 * 60 * 60 * 1_000;

export type PatientPauseOutbox = {
  version: 1;
  sessionId: string;
  idempotencyKey: string;
  state:
    | "pending"
    | "acknowledged"
    | "server_resolution_required"
    | "server_pause_observed"
    | "resolved";
  createdAt: string;
  expiresAt: string;
  /** Server SessionMsg.wseq at the first authoritative paused projection. */
  pauseSessionWseq: number | null;
};

export interface PatientPauseReceipt {
  sessionId: string;
  status: "paused";
  runtimeRevision: number;
  liveSeq: number;
  eventSeq: number;
  idempotent: boolean;
}

export function parsePatientPauseReceipt(
  value: unknown,
  sessionId: string,
): PatientPauseReceipt {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("老人暂停回执格式错误");
  }
  const row = value as Record<string, unknown>;
  const expectedKeys = [
    "sessionId", "status", "runtimeRevision", "liveSeq", "eventSeq", "idempotent",
  ];
  if (Object.keys(row).length !== expectedKeys.length
      || expectedKeys.some((key) => !Object.hasOwn(row, key))
      || row.sessionId !== sessionId || row.status !== "paused"
      || !Number.isSafeInteger(row.runtimeRevision) || Number(row.runtimeRevision) < 1
      || !Number.isSafeInteger(row.liveSeq) || Number(row.liveSeq) < 1
      || !Number.isSafeInteger(row.eventSeq) || Number(row.eventSeq) < 1
      || typeof row.idempotent !== "boolean") {
    throw new Error("老人暂停回执与当前场次不一致");
  }
  return row as unknown as PatientPauseReceipt;
}

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function parsePatientPauseOutbox(value: unknown): PatientPauseOutbox | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const expectedKeys = [
    "version", "sessionId", "idempotencyKey", "state", "createdAt", "expiresAt",
    "pauseSessionWseq",
  ];
  if (Object.keys(row).length !== expectedKeys.length
      || expectedKeys.some((key) => !Object.hasOwn(row, key))) return null;
  if (row.version !== 1
      || typeof row.sessionId !== "string" || !row.sessionId
      || typeof row.idempotencyKey !== "string"
      || !IDEMPOTENCY_PATTERN.test(row.idempotencyKey)
      || ![
        "pending", "acknowledged", "server_resolution_required",
        "server_pause_observed", "resolved",
      ].includes(String(row.state))
      || typeof row.createdAt !== "string"
      || typeof row.expiresAt !== "string") return null;
  const pauseObserved = row.state === "server_pause_observed" || row.state === "resolved";
  if (pauseObserved
    ? (!Number.isSafeInteger(row.pauseSessionWseq) || Number(row.pauseSessionWseq) < 1)
    : row.pauseSessionWseq !== null) return null;
  const createdAt = Date.parse(row.createdAt);
  const expiresAt = Date.parse(row.expiresAt);
  if (!Number.isFinite(createdAt) || !Number.isFinite(expiresAt)
      || expiresAt - createdAt !== PATIENT_PAUSE_OUTBOX_TTL_MS) return null;
  return row as PatientPauseOutbox;
}

export function loadPatientPauseOutbox(
  storage: StorageLike,
  sessionId?: string,
  now: () => Date = () => new Date(),
): PatientPauseOutbox | null {
  let raw: string | null;
  try { raw = storage.getItem(STORAGE_KEY); }
  catch { return null; }
  if (!raw) return null;
  let parsed: unknown;
  try { parsed = JSON.parse(raw) as unknown; }
  catch { return null; }
  const outbox = parsePatientPauseOutbox(parsed);
  if (!outbox || (sessionId && outbox.sessionId !== sessionId)) return null;
  if (now().getTime() >= Date.parse(outbox.expiresAt)) {
    return outbox.state === "resolved"
      ? null : {
        ...outbox,
        state: "server_resolution_required",
        pauseSessionWseq: null,
      };
  }
  return outbox;
}

export function createPatientPauseOutbox(
  sessionId: string,
  randomUuid: () => string = () => crypto.randomUUID(),
  now: () => Date = () => new Date(),
): PatientPauseOutbox {
  const compact = randomUuid().replaceAll("-", "").toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(compact)) {
    throw new Error("无法生成安全的暂停请求标识");
  }
  return createPatientPauseOutboxWithKey(
    sessionId, `patient_pause:${compact}`, now);
}

export function createPatientPauseOutboxWithKey(
  sessionId: string,
  idempotencyKey: string,
  now: () => Date = () => new Date(),
): PatientPauseOutbox {
  if (!sessionId) throw new Error("暂停请求缺少场次");
  if (!IDEMPOTENCY_PATTERN.test(idempotencyKey)) {
    throw new Error("暂停请求标识格式错误");
  }
  const createdAt = now();
  return {
    version: 1,
    sessionId,
    idempotencyKey,
    state: "pending",
    createdAt: createdAt.toISOString(),
    expiresAt: new Date(
      createdAt.getTime() + PATIENT_PAUSE_OUTBOX_TTL_MS).toISOString(),
    pauseSessionWseq: null,
  };
}

export function savePatientPauseOutbox(
  storage: StorageLike,
  outbox: PatientPauseOutbox,
): boolean {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(outbox));
    return true;
  } catch { return false; }
}

const PATIENT_PAUSE_STATE_RANK: Readonly<Record<PatientPauseOutbox["state"], number>> = {
  pending: 0,
  server_resolution_required: 1,
  acknowledged: 2,
  server_pause_observed: 3,
  resolved: 4,
};

/**
 * Advance the exact shared intent without allowing a late tab to roll it back.
 * Production callers additionally serialize this read/modify/write with the
 * one global outbox Web Lock; this comparison remains the fail-closed inner guard.
 */
function advancePatientPauseOutbox(
  storage: StorageLike,
  expected: PatientPauseOutbox,
  nextState: PatientPauseOutbox["state"],
  pauseSessionWseq: number | null = null,
): PatientPauseOutbox {
  const current = loadPatientPauseOutbox(storage, expected.sessionId);
  if (!current) throw new Error("共享暂停状态缺失");
  if (current.idempotencyKey !== expected.idempotencyKey) return current;
  if (current.state === "resolved") return current;
  if (nextState === "resolved" && current.state !== "server_pause_observed") {
    return current;
  }
  if (nextState === "server_pause_observed") {
    if (!Number.isSafeInteger(pauseSessionWseq) || Number(pauseSessionWseq) < 1) {
      throw new Error("暂停投影缺少可比较的服务器序号");
    }
    if (current.state === "server_pause_observed") return current;
  } else if (PATIENT_PAUSE_STATE_RANK[nextState]
      <= PATIENT_PAUSE_STATE_RANK[current.state]) {
    return current;
  }
  // A response arriving after the local intent expired cannot revive its
  // transport stages. A fresh paused projection may still safely close it.
  if (nextState !== "server_pause_observed" && nextState !== "resolved"
      && Date.now() >= Date.parse(current.expiresAt)) return current;
  const advanced: PatientPauseOutbox = {
    ...current,
    state: nextState,
    pauseSessionWseq: nextState === "server_pause_observed" || nextState === "resolved"
      ? (nextState === "resolved" ? current.pauseSessionWseq : pauseSessionWseq)
      : null,
  };
  if (!savePatientPauseOutbox(storage, advanced)) {
    throw new Error("暂停状态无法持久保存");
  }
  return advanced;
}

export function acknowledgePatientPauseOutbox(
  storage: StorageLike,
  outbox: PatientPauseOutbox,
): PatientPauseOutbox {
  return advancePatientPauseOutbox(storage, outbox, "acknowledged");
}

export function requirePatientPauseServerResolution(
  storage: StorageLike,
  outbox: PatientPauseOutbox,
): PatientPauseOutbox {
  return advancePatientPauseOutbox(
    storage, outbox, "server_resolution_required");
}

/** Persist that an authoritative paused projection has been observed. */
export function observePatientPauseOnServer(
  storage: StorageLike,
  outbox: PatientPauseOutbox,
  pauseSessionWseq: number,
): PatientPauseOutbox {
  return advancePatientPauseOutbox(
    storage, outbox, "server_pause_observed", pauseSessionWseq);
}

/**
 * Retain one opaque resolved key after the exact paused -> resumed transition,
 * until its original 24-hour expiry.  A delayed cross-tab reduction signal can
 * then be identified as the already-finished pause epoch instead of recreating
 * a new permanent latch after resume.
 */
export function resolvePatientPauseOutbox(
  storage: StorageLike,
  outbox: PatientPauseOutbox,
  currentSessionWseq: number | undefined,
  serverPaused: boolean,
  terminal: boolean,
): PatientPauseOutbox {
  const shared = loadPatientPauseOutbox(storage, outbox.sessionId);
  if (!shared) throw new Error("共享暂停状态缺失");
  if (shared.idempotencyKey !== outbox.idempotencyKey) return shared;
  if (!patientPauseCanResolveAfterResume(
    shared, currentSessionWseq, serverPaused, terminal,
  )) return shared;
  return advancePatientPauseOutbox(storage, shared, "resolved");
}

/** A cross-tab stop is accepted only when shared storage proves the same live intent. */
export function patientPauseOutboxForStopSignal(
  storage: StorageLike,
  sessionId: string,
  idempotencyKey: string,
): PatientPauseOutbox | null {
  const current = loadPatientPauseOutbox(storage, sessionId);
  return current !== null && current.idempotencyKey === idempotencyKey
      && current.state !== "resolved"
    ? current : null;
}

/** Retire only after a strictly newer active SessionMsg proves an authorized resume. */
export function patientPauseCanResolveAfterResume(
  outbox: PatientPauseOutbox,
  currentSessionWseq: number | undefined,
  serverPaused: boolean,
  terminal: boolean,
): boolean {
  return outbox.state === "server_pause_observed"
    && Number.isSafeInteger(outbox.pauseSessionWseq)
    && Number.isSafeInteger(currentSessionWseq)
    && Number(currentSessionWseq) > Number(outbox.pauseSessionWseq)
    && !serverPaused
    && !terminal;
}

export function clearPatientPauseOutboxIfMatches(
  storage: StorageLike,
  expected: Pick<PatientPauseOutbox, "sessionId" | "idempotencyKey">,
): boolean {
  const current = loadPatientPauseOutbox(storage);
  if (!current || current.sessionId !== expected.sessionId
      || current.idempotencyKey !== expected.idempotencyKey) return false;
  try { storage.removeItem(STORAGE_KEY); }
  catch { return false; }
  return true;
}

export function patientPauseErrorCode(error: unknown): string | null {
  if (error === null || typeof error !== "object") return null;
  const detail = (error as { detailData?: unknown }).detailData;
  if (detail === null || typeof detail !== "object" || Array.isArray(detail)) return null;
  const code = (detail as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

export type PatientPauseRetryDisposition =
  | "retry"
  | "wait_for_capability"
  | "wait_for_authority";

export function patientPauseRetryDisposition(
  error: unknown,
): PatientPauseRetryDisposition {
  const code = patientPauseErrorCode(error);
  const status = error !== null && typeof error === "object"
    && typeof (error as { status?: unknown }).status === "number"
    ? (error as { status: number }).status : null;
  if (status === 401) return "wait_for_capability";
  if (code === "patient_pause_concurrent_conflict"
      || status === 0 || status === 408 || status === 425 || status === 429
      || (status !== null && status >= 500)) return "retry";
  return "wait_for_authority";
}
