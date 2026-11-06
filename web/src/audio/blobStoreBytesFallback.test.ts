import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { advanceAudioOutbox, createAudioOutboxEntry, type AudioOutboxEntry } from "./audioOutbox.ts";
import type { AudioTerminalDisposition } from "./audioTerminalDisposition.ts";
import { sha256Blob } from "./audioUploadReceipt.ts";
import {
  blobStore,
  planTerminalOutboxDiscard,
  STORED_AUDIO_BYTES_FORMAT,
  storedAudioBlob,
  TerminalOutboxIntegrityError,
} from "./blobStore.ts";
import { createAutopilotAckEnvelope } from "../patient/autopilotAckOutbox.ts";
import { buildAutopilotAck, type NextCommandProjection } from "../patient/autopilotProtocol.ts";

// Node 没有 IndexedDB，也不引 fake-indexeddb 依赖：这里只模拟 blobStore 用到的那一小截语义
// ——事务串行、请求出错或 abort() 整体回滚、写入走结构化克隆；refuseBlobs 复现 WebKit
// 非持久会话拒存 Blob（abort = 事务中止 UnknownError；throw = add 同步抛 DataCloneError）。
type Refusal = "abort" | "throw" | "quota";

class FakeIndexedDb {
  stores = new Map<string, { keyPath?: string; rows: Map<string, unknown> }>();
  version = 0;
  openConnections = 0;
  refuseBlobs: Refusal | null = null;
  private queue: Promise<void> = Promise.resolve();

  open(name: string, version: number) {
    assert.equal(name, "nmu-audio");
    const request: Record<string, unknown> & {
      onupgradeneeded?: (event: { oldVersion: number }) => void; onsuccess?: () => void;
    } = {};
    setTimeout(() => {
      const db = this.connect();
      request.result = db;
      if (version > this.version) {
        const oldVersion = this.version;
        this.version = version;
        request.transaction = db.transaction([...this.stores.keys()], "versionchange");
        request.onupgradeneeded?.({ oldVersion });
      }
      this.schedule(async () => { request.onsuccess?.(); });
    });
    return request;
  }

  schedule(run: () => Promise<void>) {
    this.queue = this.queue.then(run);
  }

  private connect() {
    this.openConnections += 1;
    let closed = false;
    return {
      onversionchange: null as unknown,
      objectStoreNames: { contains: (store: string) => this.stores.has(store) },
      createObjectStore: (store: string, options: { keyPath?: string } = {}) => {
        this.stores.set(store, { keyPath: options.keyPath, rows: new Map() });
      },
      transaction: (names: string | string[], _mode: string) =>
        new FakeTransaction(this, typeof names === "string" ? [names] : names),
      close: () => {
        if (!closed) this.openConnections -= 1;
        closed = true;
      },
    };
  }
}

class FakeTransaction {
  oncomplete: (() => void) | null = null;
  onabort: (() => void) | null = null;
  onerror: (() => void) | null = null;
  error: unknown = null;
  private readonly ops: Array<() => void> = [];
  private working = new Map<string, Map<string, unknown>>();
  private aborted = false;
  private readonly idb: FakeIndexedDb;
  private readonly names: string[];

  constructor(idb: FakeIndexedDb, names: string[]) {
    this.idb = idb;
    this.names = names;
    idb.schedule(() => new Promise<void>((resolve) => setTimeout(() => { this.run(); resolve(); })));
  }

  abort() { this.aborted = true; }

  objectStore(name: string) {
    assert.ok(this.names.includes(name), `事务未声明 ${name}`);
    const keyPath = () => this.idb.stores.get(name)!.keyPath;
    const rows = () => this.working.get(name)!;
    const write = (value: unknown, key: string | undefined, mode: "add" | "put") => {
      if (value instanceof Blob && this.idb.refuseBlobs === "throw") {
        throw new DOMException("could not be cloned", "DataCloneError");
      }
      return this.request(() => {
        if (value instanceof Blob && this.idb.refuseBlobs === "abort") {
          throw new DOMException("Error preparing Blob/File data to be stored in object store", "UnknownError");
        }
        if (value instanceof Blob && this.idb.refuseBlobs === "quota") {
          throw new DOMException("quota", "QuotaExceededError");
        }
        const path = keyPath();
        const id = key ?? (path ? (value as Record<string, string>)[path] : undefined);
        assert.equal(typeof id, "string");
        if (mode === "add" && rows().has(id!)) throw new DOMException("key exists", "ConstraintError");
        rows().set(id!, structuredClone(value));
        return id;
      });
    };
    return {
      add: (value: unknown, key?: string) => write(value, key, "add"),
      put: (value: unknown, key?: string) => write(value, key, "put"),
      get: (key: string) => this.request(() => structuredClone(rows().get(key))),
      getKey: (key: string) => this.request(() => (rows().has(key) ? key : undefined)),
      getAll: () => this.request(() => [...rows().keys()].sort().map((key) => structuredClone(rows().get(key)))),
      getAllKeys: () => this.request(() => [...rows().keys()].sort()),
      delete: (key: string) => this.request(() => { rows().delete(key); return undefined; }),
    };
  }

  private request(op: () => unknown) {
    const request: { result?: unknown; error?: unknown; onsuccess?: () => void; onerror?: () => void } = {};
    this.ops.push(() => {
      try {
        request.result = op();
      } catch (error) {
        request.error = error;
        this.error = error;
        this.aborted = true;
        request.onerror?.();
        this.onerror?.();
        return;
      }
      request.onsuccess?.();
    });
    return request;
  }

  private run() {
    this.working = new Map(this.names.map((name) => [name, new Map(this.idb.stores.get(name)!.rows)]));
    while (this.ops.length > 0 && !this.aborted) this.ops.shift()!();
    if (this.aborted) {
      this.onabort?.();
      return;
    }
    for (const [name, rows] of this.working) this.idb.stores.get(name)!.rows = rows;
    this.oncomplete?.();
  }
}

function install(): FakeIndexedDb {
  const idb = new FakeIndexedDb();
  (globalThis as { indexedDB?: unknown }).indexedDB = idb;
  return idb;
}

function rows(idb: FakeIndexedDb, store: string): Map<string, unknown> {
  return idb.stores.get(store)!.rows;
}

// 覆盖全部 256 个字节值，任何截断、转码或字节序错误都会改变 sha256。
const BYTES = Uint8Array.from({ length: 4099 }, (_, i) => (i * 131 + 7) & 0xff);
const BYTES_SHA256 = createHash("sha256").update(BYTES).digest("hex");
const MIME = "audio/webm;codecs=opus";

function recording(rawAudioId: string, sessionId = "S-BYTES"): { entry: AudioOutboxEntry; blob: Blob } {
  const blob = new Blob([BYTES], { type: MIME });
  const entry = createAudioOutboxEntry({
    rawAudioId, sessionId, turnKey: "itm-0001#1", containsDirectIdentifier: false,
    durationSeconds: 2.5, blob, nowMs: 10,
  });
  return { entry, blob };
}

function bytesRecord(): { format: string; type: string; bytes: ArrayBuffer } {
  return { format: STORED_AUDIO_BYTES_FORMAT, type: MIME, bytes: BYTES.slice().buffer };
}

/** 以兼容形状直接种一条（不经过 stageOutbox），让每个读取点各自被单独守住。 */
async function seedBytesEntry(idb: FakeIndexedDb, entry: AudioOutboxEntry): Promise<void> {
  await blobStore.outboxEntries();   // 走真实 open() 建库
  rows(idb, "blobs").set(entry.rawAudioId, bytesRecord());
  rows(idb, "outbox").set(entry.rawAudioId, structuredClone(entry));
}

async function assertSameBytes(blob: Blob | undefined): Promise<void> {
  assert.ok(blob instanceof Blob, "读回必须是 Blob");
  assert.equal(blob.size, BYTES.length);
  assert.equal(blob.type, MIME);
  assert.equal(await sha256Blob(blob), BYTES_SHA256);
}

test("a Blob the browser accepts is still stored as the raw Blob (format unchanged)", async () => {
  const idb = install();
  const { entry, blob } = recording("raw-blob-normal");
  await blobStore.stageOutbox(entry, blob);
  const stored = rows(idb, "blobs").get(entry.rawAudioId);
  assert.ok(stored instanceof Blob, "正常路径绝不改写成字节记录");
  await assertSameBytes(await blobStore.get(entry.rawAudioId));
  assert.deepEqual(await blobStore.getOutbox(entry.rawAudioId), entry);
  assert.equal(idb.openConnections, 0);
});

for (const refusal of ["abort", "throw"] as const) {
  test(`refused Blob (${refusal === "abort" ? "WebKit UnknownError" : "sync DataCloneError"}) falls back to bytes, bit-identical`, async () => {
    const idb = install();
    idb.refuseBlobs = refusal;
    const { entry, blob } = recording(`raw-refused-${refusal}`);
    await blobStore.stageOutbox(entry, blob);
    const stored = rows(idb, "blobs").get(entry.rawAudioId) as Record<string, unknown>;
    assert.deepEqual(Object.keys(stored).sort(), ["bytes", "format", "type"]);
    assert.equal(stored.format, STORED_AUDIO_BYTES_FORMAT);
    assert.equal(stored.type, MIME);
    assert.ok(stored.bytes instanceof ArrayBuffer);
    assert.equal(createHash("sha256").update(new Uint8Array(stored.bytes)).digest("hex"), BYTES_SHA256);
    assert.deepEqual(rows(idb, "outbox").get(entry.rawAudioId), entry, "字节与元数据同事务落地");
    await assertSameBytes(await blobStore.get(entry.rawAudioId));
    assert.equal(idb.openConnections, 0, "被拒的那次事务也必须关库");
  });
}

test("fallback never masks other write failures or overwrites an existing id", async () => {
  const quota = install();
  quota.refuseBlobs = "quota";
  const first = recording("raw-quota");
  await assert.rejects(blobStore.stageOutbox(first.entry, first.blob), { name: "QuotaExceededError" });
  assert.equal(rows(quota, "blobs").size, 0);
  assert.equal(rows(quota, "outbox").size, 0);

  const idb = install();
  idb.refuseBlobs = "abort";
  const { entry, blob } = recording("raw-duplicate");
  await blobStore.stageOutbox(entry, blob);
  const other = new Blob([BYTES.slice(1), new Uint8Array([0])], { type: MIME });
  await assert.rejects(blobStore.stageOutbox(entry, other), { name: "ConstraintError" });
  // useVoxRecorder 的幂等核对：同 ID 重放只在元数据与字节哈希都一致时才认。
  assert.deepEqual(await blobStore.getOutbox(entry.rawAudioId), entry);
  await assertSameBytes(await blobStore.get(entry.rawAudioId));
  assert.notEqual(await sha256Blob(other), BYTES_SHA256);
  assert.equal(idb.openConnections, 0);
});

test("recovery snapshot, upload read, cache sweep and manual delete handle a mixed Blob + bytes database", async () => {
  const idb = install();
  const old = recording("raw-mixed-blob", "S-OLD");
  await blobStore.stageOutbox(old.entry, old.blob);
  idb.refuseBlobs = "abort";
  const fresh = recording("raw-mixed-bytes", "S-NEW");
  await blobStore.stageOutbox(fresh.entry, fresh.blob);
  assert.ok(rows(idb, "blobs").get(old.entry.rawAudioId) instanceof Blob);
  assert.equal((rows(idb, "blobs").get(fresh.entry.rawAudioId) as { format?: string }).format,
    STORED_AUDIO_BYTES_FORMAT);

  const snapshot = await blobStore.recoverySnapshot();
  assert.deepEqual(snapshot.entries.map((row) => row.rawAudioId), ["raw-mixed-blob", "raw-mixed-bytes"]);
  assert.deepEqual(snapshot.legacyOrphans, []);
  assert.equal(snapshot.invalidBlobKeyCount, 0);
  for (const row of snapshot.entries) await assertSameBytes(await blobStore.get(row.rawAudioId));

  assert.deepEqual(await blobStore.keys(), ["raw-mixed-blob", "raw-mixed-bytes"]);
  assert.equal(await blobStore.deleteIfUnclaimed(fresh.entry.rawAudioId), "preserved-outbox");
  assert.equal(await blobStore.del(fresh.entry.rawAudioId), "preserved-outbox");
  rows(idb, "outbox").delete(fresh.entry.rawAudioId);
  assert.equal(await blobStore.deleteIfUnclaimed(fresh.entry.rawAudioId), "deleted");
  await blobStore.completeOutbox(old.entry.rawAudioId);
  assert.equal(rows(idb, "blobs").size, 0);
  assert.equal(rows(idb, "outbox").size, 0);
  assert.equal(idb.openConnections, 0);
});

function disposition(entry: AudioOutboxEntry, checksum: string): AudioTerminalDisposition {
  return {
    code: "audio_terminal_disposition", schemaVersion: 1, action: "discard_local_copy", reason: "deleted",
    rawAudioId: entry.rawAudioId, sessionId: entry.sessionId, turnKey: entry.turnKey,
    byteCount: entry.blobBytes, checksum, containsDirectIdentifier: entry.containsDirectIdentifier,
  };
}

test("HTTP 410 terminal discard re-hashes and atomically deletes a bytes entry", async () => {
  const idb = install();
  const registered = advanceAudioOutbox(recording("raw-discard-bytes").entry, "registered", { nowMs: 20 });
  await seedBytesEntry(idb, registered);
  await assert.rejects(blobStore.discardTerminalOutbox(registered, disposition(registered, "0".repeat(64))),
    TerminalOutboxIntegrityError);
  assert.equal(rows(idb, "blobs").size, 1, "哈希不符时副本保持不变");
  assert.equal(await blobStore.discardTerminalOutbox(registered, disposition(registered, BYTES_SHA256)),
    "discarded");
  assert.equal(rows(idb, "blobs").size, 0);
  assert.equal(rows(idb, "outbox").size, 0);
  assert.equal(planTerminalOutboxDiscard({
    expectedEntry: registered, storedEntry: registered, storedBlob: bytesRecord(), hasLegacyMarker: false,
  }), "discarded");
});

test("record_stopped ACK takeover accepts a bytes entry in its atomic transaction", async () => {
  const idb = install();
  const rawAudioId = "raw-ack-bytes-0001";
  const command: NextCommandProjection = {
    schema_version: 1, command_key: "cmd-bytes-record-0002", command_seq: 2, kind: "record",
    state: "started", command_revision: 1, control_generation: 1, runner_generation: 1,
    item_ref: "itm-0001", turn_seq: 1, attempt_seq: 1, prompt_level: 0,
    payload: {
      raw_audio_id: rawAudioId, turn_ref: "itm-0001#1", max_duration_seconds: 12,
      contains_direct_identifier: false, presentation_speech_key: "ack.question",
      presentation_speech_text: "请说出图片中的物品。", presentation_purpose: "question",
    },
  };
  const captured = recording(rawAudioId, "S-ACK").entry;
  const uploaded: AudioOutboxEntry = {
    ...advanceAudioOutbox(advanceAudioOutbox(captured, "registered", { nowMs: 20 }), "uploaded",
      { checksum: BYTES_SHA256, nowMs: 30 }),
    autopilotStopReason: "user_done",
    captureReceiptServerSeq: 9,
  };
  await seedBytesEntry(idb, uploaded);
  const ack = buildAutopilotAck(command, 0, "ack:record_stopped:2:00000000-0000-4000-8000-000000000003", {
    ack_type: "record_stopped", stop_reason: "user_done", raw_audio_id: rawAudioId,
    receipt_server_seq: 9, checksum: BYTES_SHA256, byte_count: BYTES.length, duration_seconds: 2.5,
  });
  const envelope = createAutopilotAckEnvelope({
    ownerKey: "a".repeat(64), sessionId: "S-ACK", command, ack, nowMs: 40,
  });
  await blobStore.stageRecordStoppedAckAndCompleteAudio(envelope);
  assert.equal(rows(idb, "blobs").size, 0);
  assert.equal(rows(idb, "outbox").size, 0);
  assert.equal(rows(idb, "autopilot-acks").size, 1);
});

test("unknown stored shapes never yield the recorded bytes and stay fail-closed", async () => {
  const blob = new Blob([BYTES], { type: MIME });
  assert.equal(storedAudioBlob(blob), blob);
  assert.equal(storedAudioBlob(undefined), undefined);
  const malformed: unknown[] = [
    null,
    "raw-text",
    BYTES.slice().buffer,
    { ...bytesRecord(), format: "nmu-audio-bytes-v2" },
    { ...bytesRecord(), bytes: BYTES.slice() },
    { ...bytesRecord(), type: 1 },
    // 伪造 size/type 也骗不过：识别不了就不交出任何字节。
    { ...bytesRecord(), size: BYTES.length },
  ];
  const registered = advanceAudioOutbox(recording("raw-malformed").entry, "registered", { nowMs: 20 });
  for (const value of malformed) {
    assert.equal(storedAudioBlob(value)?.size, 0);
    assert.throws(() => planTerminalOutboxDiscard({
      expectedEntry: registered, storedEntry: registered, storedBlob: value, hasLegacyMarker: false,
    }), TerminalOutboxIntegrityError);
    const idb = install();
    await seedBytesEntry(idb, registered);
    rows(idb, "blobs").set(registered.rawAudioId, value);
    const read = await blobStore.get(registered.rawAudioId);
    assert.ok(read instanceof Blob && read.size !== registered.blobBytes,
      "读取点已有的 size 校验因此照旧判 storage-invalid");
    await assert.rejects(blobStore.discardTerminalOutbox(registered, disposition(registered, BYTES_SHA256)),
      TerminalOutboxIntegrityError);
    assert.equal(rows(idb, "blobs").size, 1);
    assert.equal(rows(idb, "outbox").size, 1);
  }
});
