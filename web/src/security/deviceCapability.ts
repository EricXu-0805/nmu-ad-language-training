export const DEVICE_CAPABILITY_STORAGE_KEY = "nmu:device-capability:v1";
export const DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY = "nmu:device-recovery-capabilities:v1";
export const DEVICE_ID_STORAGE_KEY = "nmu:device-id:v1";
export const LEGACY_PIN_STORAGE_KEY = "nmu:pin";

const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,256}$/;
const DEVICE_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const SESSION_ID_PATTERN = /^[^\r\n]{1,128}$/;
const ISO_INSTANT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

export interface DeviceStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface DeviceCapabilityRecord {
  capability: string;
  sessionId: string;
  expiresAt: string;
}

export interface DeviceCapabilityStore {
  get(): DeviceCapabilityRecord | null;
  save(value: unknown): DeviceCapabilityRecord;
  clear(): void;
  clearIfMatches(value: DeviceCapabilityRecord): boolean;
  getRecovery(sessionId: string): DeviceCapabilityRecord | null;
  retainForRecovery(value: unknown): DeviceCapabilityRecord;
  demoteIfMatches(value: DeviceCapabilityRecord): boolean;
  removeRecovery(sessionId: string): void;
  removeRecoveryIfMatches(value: DeviceCapabilityRecord): boolean;
  clearAll(): void;
  getOrCreateDeviceId(): string;
}

function exactKeys(value: Record<string, unknown>, expected: string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((key, i) => key === wanted[i]);
}

export function parseDevicePairResponse(
  value: unknown,
  nowMs = Date.now(),
): DeviceCapabilityRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("设备配对响应格式错误");
  }
  const record = value as Record<string, unknown>;
  if (!exactKeys(record, ["capability", "sessionId", "expiresAt"])) {
    throw new Error("设备配对响应字段不完整或包含未知字段");
  }
  const capability = record.capability;
  const sessionId = record.sessionId;
  const expiresAt = record.expiresAt;
  if (typeof capability !== "string" || !TOKEN_PATTERN.test(capability)) {
    throw new Error("设备配对凭据格式错误");
  }
  if (typeof sessionId !== "string" || sessionId.includes("\u0000")
      || !SESSION_ID_PATTERN.test(sessionId)) {
    throw new Error("设备配对场次格式错误");
  }
  if (typeof expiresAt !== "string" || !ISO_INSTANT_PATTERN.test(expiresAt)) {
    throw new Error("设备配对有效期格式错误");
  }
  const expiryMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiryMs) || expiryMs <= nowMs) {
    throw new Error("设备配对已过期");
  }
  return { capability, sessionId, expiresAt };
}

function randomDeviceId(): string {
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi?.getRandomValues) throw new Error("当前浏览器不能安全生成设备标识");
  const bytes = new Uint8Array(24);
  cryptoApi.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

export function createDeviceCapabilityStore(
  local: DeviceStorageLike,
  session: DeviceStorageLike,
  now: () => number = Date.now,
  createDeviceId: () => string = randomDeviceId,
): DeviceCapabilityStore {
  // Upgrade cleanup happens immediately.  A PIN is never migrated and neither the
  // capability nor device id is ever accepted from persistent localStorage.
  for (const key of [
    LEGACY_PIN_STORAGE_KEY,
    DEVICE_CAPABILITY_STORAGE_KEY,
    DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY,
    DEVICE_ID_STORAGE_KEY,
  ]) {
    try { local.removeItem(key); } catch { /* unavailable storage stays fail closed */ }
  }
  try { session.removeItem(LEGACY_PIN_STORAGE_KEY); } catch { /* no PIN may survive */ }

  const writeRecoveryRecords = (records: DeviceCapabilityRecord[]) => {
    if (records.length === 0) session.removeItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY);
    else session.setItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY, JSON.stringify(records));
  };
  const decodeRecoveryRecords = (raw: string | null): DeviceCapabilityRecord[] => {
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    const bySession = new Map<string, DeviceCapabilityRecord>();
    for (const candidate of parsed) {
      try {
        const record = parseDevicePairResponse(candidate, now());
        bySession.set(record.sessionId, record);
      } catch { /* malformed/expired entries are discarded independently */ }
    }
    return [...bySession.values()];
  };
  const readRecoveryRecords = (): DeviceCapabilityRecord[] => {
    try {
      const raw = session.getItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY);
      if (!raw) return [];
      const records = decodeRecoveryRecords(raw);
      writeRecoveryRecords(records);
      return records;
    } catch {
      try { session.removeItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY); } catch { /* unavailable */ }
      return [];
    }
  };
  const removeRecovery = (sessionId: string) => {
    try {
      writeRecoveryRecords(readRecoveryRecords().filter((record) => record.sessionId !== sessionId));
    } catch { /* unavailable storage stays fail closed */ }
  };
  const sameCredential = (
    left: DeviceCapabilityRecord | null,
    right: DeviceCapabilityRecord,
  ): boolean => left?.capability === right.capability && left.sessionId === right.sessionId;
  const rawActiveMatches = (value: DeviceCapabilityRecord): boolean => {
    try {
      const raw = session.getItem(DEVICE_CAPABILITY_STORAGE_KEY);
      if (!raw) return false;
      const parsed = JSON.parse(raw) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return false;
      const record = parsed as Record<string, unknown>;
      return exactKeys(record, ["capability", "sessionId", "expiresAt"])
        && record.capability === value.capability
        && record.sessionId === value.sessionId
        && record.expiresAt === value.expiresAt;
    } catch { return false; }
  };
  const restoreRaw = (key: string, value: string | null) => {
    if (value === null) session.removeItem(key);
    else session.setItem(key, value);
  };

  return {
    get() {
      let raw: string | null;
      try { raw = session.getItem(DEVICE_CAPABILITY_STORAGE_KEY); }
      catch { return null; }
      if (!raw) return null;
      try {
        return parseDevicePairResponse(JSON.parse(raw) as unknown, now());
      } catch {
        try { session.removeItem(DEVICE_CAPABILITY_STORAGE_KEY); } catch { /* already unusable */ }
        return null;
      }
    },
    save(value: unknown) {
      const record = parseDevicePairResponse(value, now());
      let previousActive: string | null = null;
      let previousRecovery: string | null = null;
      let activeCaptured = false;
      let recoveryCaptured = false;
      try {
        previousActive = session.getItem(DEVICE_CAPABILITY_STORAGE_KEY);
        activeCaptured = true;
        previousRecovery = session.getItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY);
        recoveryCaptured = true;
        // A new same-session pair hard-revokes all prior tokens server-side.  The new
        // active token can carry that session's remaining exact outbox ACKs.
        const recoveries = decodeRecoveryRecords(previousRecovery).filter(
          (existing) => existing.sessionId !== record.sessionId);
        // Write the dependent recovery update first.  If either storage operation
        // fails, restore both old raw values so callers never observe a thrown
        // "pair failed" after the new active token was already left installed.
        writeRecoveryRecords(recoveries);
        session.setItem(DEVICE_CAPABILITY_STORAGE_KEY, JSON.stringify(record));
      } catch (error) {
        if (activeCaptured) {
          try { restoreRaw(DEVICE_CAPABILITY_STORAGE_KEY, previousActive); } catch { /* fail closed */ }
        }
        if (recoveryCaptured) {
          try { restoreRaw(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY, previousRecovery); } catch { /* fail closed */ }
        }
        throw new Error("当前标签页无法保存设备配对", { cause: error });
      }
      return record;
    },
    clear() {
      try { session.removeItem(DEVICE_CAPABILITY_STORAGE_KEY); } catch { /* already unavailable */ }
    },
    clearIfMatches(value: DeviceCapabilityRecord) {
      // Capture the raw exact match before get() performs expiry pruning.  A token
      // may expire while its request is in flight; that server 401 must still be
      // reported as an auth loss even though validation just removed the record.
      const matchedBeforeValidation = rawActiveMatches(value);
      if (!sameCredential(this.get(), value)) {
        return matchedBeforeValidation && !rawActiveMatches(value);
      }
      try {
        session.removeItem(DEVICE_CAPABILITY_STORAGE_KEY);
        return !sameCredential(this.get(), value);
      } catch { return false; }
    },
    getRecovery(sessionId: string) {
      if (!SESSION_ID_PATTERN.test(sessionId) || sessionId.includes("\u0000")) return null;
      return readRecoveryRecords().find((record) => record.sessionId === sessionId) ?? null;
    },
    retainForRecovery(value: unknown) {
      const record = parseDevicePairResponse(value, now());
      try {
        const others = readRecoveryRecords().filter(
          (existing) => existing.sessionId !== record.sessionId);
        writeRecoveryRecords([...others, record]);
      } catch (error) {
        throw new Error("当前标签页无法保存录音回执恢复凭据", { cause: error });
      }
      return record;
    },
    demoteIfMatches(value: DeviceCapabilityRecord) {
      const matchedBeforeValidation = rawActiveMatches(value);
      if (!sameCredential(this.get(), value)) {
        return matchedBeforeValidation && !rawActiveMatches(value);
      }
      // A credential that expired while its request was in flight must not be
      // resurrected into the recovery slot.
      if (Date.parse(value.expiresAt) <= now()) return this.clearIfMatches(value);
      let previousActive: string | null = null;
      let previousRecovery: string | null = null;
      let activeCaptured = false;
      let recoveryCaptured = false;
      try {
        previousActive = session.getItem(DEVICE_CAPABILITY_STORAGE_KEY);
        activeCaptured = true;
        previousRecovery = session.getItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY);
        recoveryCaptured = true;
        // Re-check after the reads: a late response for token A must never demote
        // a newly paired token B from the same tab.
        if (!sameCredential(this.get(), value)) return false;
        const others = decodeRecoveryRecords(previousRecovery).filter(
          (existing) => existing.sessionId !== value.sessionId);
        writeRecoveryRecords([...others, value]);
        session.removeItem(DEVICE_CAPABILITY_STORAGE_KEY);
        return true;
      } catch (error) {
        if (activeCaptured) {
          try { restoreRaw(DEVICE_CAPABILITY_STORAGE_KEY, previousActive); } catch { /* fail closed */ }
        }
        if (recoveryCaptured) {
          try { restoreRaw(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY, previousRecovery); } catch { /* fail closed */ }
        }
        throw new Error("当前标签页无法切换设备恢复凭据", { cause: error });
      }
    },
    removeRecovery,
    removeRecoveryIfMatches(value: DeviceCapabilityRecord) {
      const existing = this.getRecovery(value.sessionId);
      if (!sameCredential(existing, value)) return false;
      try {
        writeRecoveryRecords(readRecoveryRecords().filter(
          (record) => !sameCredential(record, value)));
        return !sameCredential(this.getRecovery(value.sessionId), value);
      } catch { return false; }
    },
    clearAll() {
      try {
        session.removeItem(DEVICE_CAPABILITY_STORAGE_KEY);
        session.removeItem(DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY);
        session.removeItem(DEVICE_ID_STORAGE_KEY);
        session.removeItem(LEGACY_PIN_STORAGE_KEY);
      } catch { /* best-effort logout cleanup also scans sensitive keys */ }
    },
    getOrCreateDeviceId() {
      try {
        const existing = session.getItem(DEVICE_ID_STORAGE_KEY);
        if (existing && DEVICE_ID_PATTERN.test(existing)) return existing;
        session.removeItem(DEVICE_ID_STORAGE_KEY);
        const generated = createDeviceId();
        if (!DEVICE_ID_PATTERN.test(generated)) throw new Error("随机设备标识格式错误");
        session.setItem(DEVICE_ID_STORAGE_KEY, generated);
        return generated;
      } catch (error) {
        if (error instanceof Error && error.message.includes("设备标识")) throw error;
        throw new Error("当前标签页无法保存设备标识", { cause: error });
      }
    },
  };
}
