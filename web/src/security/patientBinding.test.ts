import assert from "node:assert/strict";
import test from "node:test";
import {
  createPatientBindingStore,
  parsePatientBindingRecord,
  PATIENT_BINDING_STORAGE_KEY,
} from "./patientBinding.ts";
import type { DeviceStorageLike } from "./deviceCapability.ts";

class MemoryStorage implements DeviceStorageLike {
  values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

const RECORD = {
  binding: `pb1.${"a".repeat(40)}.${"b".repeat(43)}`,
  deviceId: "c".repeat(24),
};

test("绑定记录严格校验:形状、字段集合、令牌与设备标识格式", () => {
  assert.deepEqual(parsePatientBindingRecord({ ...RECORD }), RECORD);
  for (const bad of [
    null,
    [],
    {},
    { binding: RECORD.binding },
    { ...RECORD, extra: 1 },
    { ...RECORD, binding: "not-a-token" },
    { ...RECORD, binding: `pb2.${"a".repeat(40)}.${"b".repeat(43)}` },
    { ...RECORD, deviceId: "short" },
    { ...RECORD, binding: RECORD.binding + " " },
  ]) {
    assert.throws(() => parsePatientBindingRecord(bad), undefined, JSON.stringify(bad));
  }
});

test("保存/读取往返;损坏的存量记录被清除而不是反复重试", () => {
  const local = new MemoryStorage();
  const store = createPatientBindingStore(local);
  assert.equal(store.get(), null);
  store.save({ ...RECORD });
  assert.deepEqual(store.get(), RECORD);

  local.setItem(PATIENT_BINDING_STORAGE_KEY, "{broken json");
  assert.equal(store.get(), null);
  assert.equal(local.getItem(PATIENT_BINDING_STORAGE_KEY), null);

  local.setItem(PATIENT_BINDING_STORAGE_KEY, JSON.stringify({ binding: "x", deviceId: "y" }));
  assert.equal(store.get(), null);
  assert.equal(local.getItem(PATIENT_BINDING_STORAGE_KEY), null);
});

test("clear 幂等且不吞存储异常之外的状态", () => {
  const local = new MemoryStorage();
  const store = createPatientBindingStore(local);
  store.save({ ...RECORD });
  store.clear();
  assert.equal(store.get(), null);
  store.clear();
  assert.equal(store.get(), null);
});
