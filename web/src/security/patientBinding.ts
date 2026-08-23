// 受试者绑定令牌:老人端一次输入本人配对码后长期保存(localStorage,跨标签页/
// 重启存活),此后本人每次开场由 /device/attach 静默换取场次能力。它不是任何
// 设备路由的凭据;设备能力仍是短时 sessionStorage,规则不变。
import type { DeviceStorageLike } from "./deviceCapability";

export const PATIENT_BINDING_STORAGE_KEY = "nmu:patient-binding:v1";

const BINDING_PATTERN = /^pb1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const DEVICE_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const BINDING_MAX_LENGTH = 1024;

export interface PatientBindingRecord {
  binding: string;
  deviceId: string;
}

export function parsePatientBindingRecord(value: unknown): PatientBindingRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("受试者绑定记录格式错误");
  }
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  if (keys.length !== 2 || keys[0] !== "binding" || keys[1] !== "deviceId") {
    throw new Error("受试者绑定记录字段不完整或包含未知字段");
  }
  const { binding, deviceId } = record;
  if (typeof binding !== "string" || binding.length > BINDING_MAX_LENGTH
      || !BINDING_PATTERN.test(binding)) {
    throw new Error("受试者绑定令牌格式错误");
  }
  if (typeof deviceId !== "string" || !DEVICE_ID_PATTERN.test(deviceId)) {
    throw new Error("受试者绑定设备标识格式错误");
  }
  return { binding, deviceId };
}

export interface PatientBindingStore {
  get(): PatientBindingRecord | null;
  save(value: unknown): PatientBindingRecord;
  clear(): void;
}

export function createPatientBindingStore(
  local: DeviceStorageLike,
): PatientBindingStore {
  return {
    get() {
      let raw: string | null;
      try { raw = local.getItem(PATIENT_BINDING_STORAGE_KEY); }
      catch { return null; }
      if (!raw) return null;
      try {
        return parsePatientBindingRecord(JSON.parse(raw) as unknown);
      } catch {
        // 损坏/旧格式即清除:绝不带着解析不了的凭据反复重试。
        try { local.removeItem(PATIENT_BINDING_STORAGE_KEY); } catch { /* 已不可用 */ }
        return null;
      }
    },
    save(value: unknown) {
      const record = parsePatientBindingRecord(value);
      try {
        local.setItem(PATIENT_BINDING_STORAGE_KEY, JSON.stringify(record));
      } catch (error) {
        throw new Error("本机无法保存受试者绑定", { cause: error });
      }
      return record;
    },
    clear() {
      try { local.removeItem(PATIENT_BINDING_STORAGE_KEY); } catch { /* 已不可用 */ }
    },
  };
}
