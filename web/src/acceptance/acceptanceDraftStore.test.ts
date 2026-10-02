import assert from "node:assert/strict";
import test from "node:test";
import {
  ACCEPTANCE_DRAFT_STORAGE_KEY,
  loadAcceptanceWork,
  saveAcceptanceWork,
  type AcceptanceDraftStorage,
} from "./acceptanceDraftStore.ts";
import {
  ACCEPTANCE_HEADER_FIELDS,
  ACCEPTANCE_STEP_KEYS,
  emptyDraft,
  freezeAcceptanceReceipt,
  type MachineFacts,
} from "./acceptanceProtocol.ts";
import { collectAcceptanceDeviceFacts, type DeviceFactEnvironment } from "./acceptanceDeviceFacts.ts";

class MemoryStorage implements AcceptanceDraftStorage {
  values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, String(value)); }
}

/** 私隐模式、站点存储被禁、配额满：一碰就抛。 */
class ThrowingStorage implements AcceptanceDraftStorage {
  getItem(): string | null { throw new DOMException("denied", "SecurityError"); }
  setItem(): void { throw new DOMException("full", "QuotaExceededError"); }
}

const RECEIPT_ID = "0f8fad5b-d9cb-469f-a165-70867728950e";
const OTHER_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const STARTED = "2026-08-20T01:00:00.000Z";
const MACHINE: MachineFacts = {
  userAgent: "Mozilla/5.0 (iPad; CPU OS 18_5)",
  buildId: "1755100000000",
  screen: "1180×820 @2x",
  finishedAtIso: "2026-08-20T02:30:00.000Z",
};

function deviceFacts() {
  const env = { navigator: { maxTouchPoints: 5 }, devicePixelRatio: 2 } as unknown as DeviceFactEnvironment;
  return collectAcceptanceDeviceFacts(env, {
    acquireLease: () => Promise.reject(new Error("没有麦克风接口时不该要锁")),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    now: () => Date.parse("2026-08-20T01:10:00.000Z"),
  });
}

function storedWith(raw: string): MemoryStorage {
  const storage = new MemoryStorage();
  storage.setItem(ACCEPTANCE_DRAFT_STORAGE_KEY, raw);
  return storage;
}

test("草稿存了再读回来一模一样：表头、勾选、备注、设备参数、改正关系", async () => {
  const storage = new MemoryStorage();
  const draft = emptyDraft(RECEIPT_ID, STARTED);
  draft.supersedesReceiptId = OTHER_ID;
  draft.header.device = "iPad 第 10 代";
  draft.header.assetTag = "NMU-T03";
  draft.outcomes["5c"] = "fail";
  draft.outcomes["1"] = "pass";
  draft.notes["5c"] = "静音后没有提示";
  draft.deviceFacts = await deviceFacts();
  assert.equal(saveAcceptanceWork(storage, { draft, frozenJson: null }), true);
  assert.deepEqual(loadAcceptanceWork(storage), { draft, frozenJson: null });
});

test("冻结的字节跟着草稿一起存，读回来逐字节相同", () => {
  const storage = new MemoryStorage();
  const draft = emptyDraft(RECEIPT_ID, STARTED);
  draft.header.device = "iPad 第 10 代";
  const frozen = freezeAcceptanceReceipt(draft, MACHINE);
  assert.equal(saveAcceptanceWork(storage, { draft, frozenJson: frozen.json }), true);
  const loaded = loadAcceptanceWork(storage);
  assert.ok(loaded);
  assert.equal(loaded.frozenJson, frozen.json);
  assert.equal(loaded.draft.receiptId, RECEIPT_ID);
});

test("没存过就是 null：页面开一份新的", () => {
  assert.equal(loadAcceptanceWork(new MemoryStorage()), null);
});

test("坏数据一律当没有：坏 JSON、旧结构、编号或时间不合格式、值类型不对、冻结字节对不上编号", () => {
  const draft = emptyDraft(RECEIPT_ID, STARTED);
  const valid = { version: 1, draft, frozenJson: null };
  const withDraft = (patch: Record<string, unknown>) => JSON.stringify({ ...valid, draft: { ...draft, ...patch } });
  const corruptions = [
    "{",
    "null",
    "[]",
    "\"草稿\"",
    JSON.stringify({ ...valid, version: 2 }),
    JSON.stringify({ ...valid, draft: null }),
    // v1 页面从来不存草稿；没有编号的结构也一律不认。
    JSON.stringify({ version: 1, draft: { header: {}, outcomes: {}, notes: {} }, frozenJson: null }),
    withDraft({ receiptId: "not-a-uuid" }),
    withDraft({ supersedesReceiptId: "x" }),
    withDraft({ startedAtIso: "昨天下午" }),
    withDraft({ header: { device: 3 } }),
    withDraft({ outcomes: { 1: "maybe" } }),
    withDraft({ notes: { 1: 3 } }),
    withDraft({ notes: null }),
    withDraft({ deviceFacts: { collectedAtIso: 1 } }),
    JSON.stringify({ ...valid, frozenJson: "{" }),
    JSON.stringify({ ...valid, frozenJson: 3 }),
    JSON.stringify({ ...valid, frozenJson: freezeAcceptanceReceipt(emptyDraft(OTHER_ID, STARTED), MACHINE).json }),
  ];
  for (const raw of corruptions) {
    assert.equal(loadAcceptanceWork(storedWith(raw)), null, raw.slice(0, 80));
  }
});

test("存储一碰就抛（私隐模式、存储被禁、配额满）：读是 null，写是 false，都不抛给页面", () => {
  const storage = new ThrowingStorage();
  const work = { draft: emptyDraft(), frozenJson: null };
  assert.doesNotThrow(() => loadAcceptanceWork(storage));
  assert.equal(loadAcceptanceWork(storage), null);
  assert.doesNotThrow(() => saveAcceptanceWork(storage, work));
  assert.equal(saveAcceptanceWork(storage, work), false);
  // 连 window.localStorage 这个属性都读不到时，页面拿到的是 null。
  assert.equal(loadAcceptanceWork(null), null);
  assert.equal(saveAcceptanceWork(null, work), false);
});

test("协议后来加了项或删了项：缺的按没填、没勾补上，不认识的键丢掉", () => {
  const draft = emptyDraft(RECEIPT_ID, STARTED);
  draft.outcomes["1"] = "pass";
  draft.header.device = "iPad 第 10 代";
  const stored = JSON.parse(JSON.stringify({ version: 1, draft, frozenJson: null }));
  delete stored.draft.outcomes["13b"];
  delete stored.draft.header.assetTag;
  stored.draft.outcomes["99"] = "pass";
  stored.draft.notes["99"] = "已经删掉的项";
  stored.draft.header.retired = "旧表头";
  const loaded = loadAcceptanceWork(storedWith(JSON.stringify(stored)));
  assert.ok(loaded);
  assert.equal(loaded.draft.outcomes["1"], "pass");
  assert.equal(loaded.draft.outcomes["13b"], "unset");
  assert.equal(loaded.draft.header.device, "iPad 第 10 代");
  assert.equal(loaded.draft.header.assetTag, "");
  assert.deepEqual(new Set(Object.keys(loaded.draft.outcomes)), new Set(ACCEPTANCE_STEP_KEYS));
  assert.deepEqual(new Set(Object.keys(loaded.draft.header)), new Set(ACCEPTANCE_HEADER_FIELDS.map((field) => field.key)));
  assert.deepEqual(loaded.draft.notes, {});
});

test("本机只存当前这一份，键名固定：开始新记录就把上一份覆盖掉", () => {
  const storage = new MemoryStorage();
  saveAcceptanceWork(storage, { draft: emptyDraft(RECEIPT_ID, STARTED), frozenJson: null });
  saveAcceptanceWork(storage, { draft: emptyDraft(OTHER_ID, STARTED), frozenJson: null });
  assert.deepEqual([...storage.values.keys()], ["nmu:acceptance:draft:v1"]);
  assert.equal(loadAcceptanceWork(storage)?.draft.receiptId, OTHER_ID);
});
