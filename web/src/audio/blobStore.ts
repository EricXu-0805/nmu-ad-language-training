// 设备侧音频恢复副本(IndexedDB,key=raw_audio_id)。录音会先保存在这里，再上传同源后端；
// 后端明确确认保存前绝不丢本机副本。删除只在服务端治理闸门放行后镜像执行。
import { parseAudioOutboxEntry, type AudioOutboxEntry } from "./audioOutbox.ts";
import type { AudioTerminalDisposition } from "./audioTerminalDisposition.ts";
import { sha256Blob } from "./audioUploadReceipt.ts";
import {
  automaticCacheDeleteDecision,
  createLegacyAudioOrphan,
  findLegacyBlobIds,
  isSafeAudioStorageId,
  parseLegacyAudioOrphan,
  type LegacyAudioOrphan,
} from "./audioStoragePolicy.ts";
import {
  nextAutopilotAckCheckpoint,
  parseAutopilotAckCheckpoint,
  parseAutopilotAckEnvelope,
  sameAutopilotAckEnvelope,
  type AutopilotAckCheckpoint,
  type AutopilotAckEnvelope,
} from "../patient/autopilotAckOutbox.ts";

const DB = "nmu-audio";
const STORE = "blobs";
const OUTBOX_STORE = "outbox";
const LEGACY_STORE = "legacy-orphans";
const AUTOPILOT_ACK_STORE = "autopilot-acks";
const AUTOPILOT_ACK_STATE_STORE = "autopilot-ack-state";
const DB_VERSION = 4;

export interface AudioRecoverySnapshot {
  entries: AudioOutboxEntry[];
  legacyOrphans: LegacyAudioOrphan[];
  invalidBlobKeyCount: number;
}

export interface AutopilotAckStorageSnapshot {
  pending: AutopilotAckEnvelope | null;
  checkpoint: AutopilotAckCheckpoint | null;
}

export type AudioCacheDeleteResult =
  | "deleted"
  | "missing"
  | "preserved-outbox"
  | "preserved-legacy";

export type TerminalOutboxDiscardResult = "discarded" | "already-discarded";

export class TerminalOutboxIntegrityError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "TerminalOutboxIntegrityError";
  }
}

function sameOutboxSnapshot(left: AudioOutboxEntry, right: AudioOutboxEntry): boolean {
  return left.schemaVersion === right.schemaVersion
    && left.rawAudioId === right.rawAudioId
    && left.sessionId === right.sessionId
    && left.turnKey === right.turnKey
    && left.containsDirectIdentifier === right.containsDirectIdentifier
    && left.durationSeconds === right.durationSeconds
    && left.blobBytes === right.blobBytes
    && left.mimeType === right.mimeType
    && left.phase === right.phase
    && left.checksum === right.checksum
    && left.autopilotStopReason === right.autopilotStopReason
    && left.captureReceiptServerSeq === right.captureReceiptServerSeq
    && left.createdAtMs === right.createdAtMs
    && left.updatedAtMs === right.updatedAtMs;
}

/** Pure integrity plan used inside the readwrite transaction before either delete. */
export function planTerminalOutboxDiscard(input: {
  expectedEntry: AudioOutboxEntry;
  storedEntry: unknown | undefined;
  storedBlob: Blob | undefined;
  hasLegacyMarker: boolean;
}): TerminalOutboxDiscardResult {
  const expected = parseAudioOutboxEntry(input.expectedEntry);
  if (input.hasLegacyMarker) {
    throw new TerminalOutboxIntegrityError("终态录音带有历史孤儿标记，拒绝自动丢弃");
  }
  const blobMissing = input.storedBlob === undefined;
  const outboxMissing = input.storedEntry === undefined;
  if (blobMissing && outboxMissing) return "already-discarded";
  if (blobMissing !== outboxMissing) {
    throw new TerminalOutboxIntegrityError("终态录音 Blob 与 outbox 仅缺一项，拒绝部分清理");
  }
  let stored: AudioOutboxEntry;
  try { stored = parseAudioOutboxEntry(input.storedEntry); }
  catch (error) {
    throw new TerminalOutboxIntegrityError("终态录音 outbox 事务内校验失败", { cause: error });
  }
  if (!sameOutboxSnapshot(stored, expected)) {
    throw new TerminalOutboxIntegrityError("终态录音 outbox 在处置前已变化");
  }
  const blob = input.storedBlob as Blob;
  if (blob.size !== expected.blobBytes || (blob.type || "audio/webm") !== expected.mimeType) {
    throw new TerminalOutboxIntegrityError("终态录音 Blob 在处置前已变化");
  }
  return "discarded";
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, DB_VERSION);
    let blocked = false;
    req.onupgradeneeded = (event) => {
      if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
      if (!req.result.objectStoreNames.contains(OUTBOX_STORE)) {
        req.result.createObjectStore(OUTBOX_STORE, { keyPath: "rawAudioId" });
      }
      if (!req.result.objectStoreNames.contains(LEGACY_STORE)) {
        req.result.createObjectStore(LEGACY_STORE, { keyPath: "rawAudioId" });
      }
      if (!req.result.objectStoreNames.contains(AUTOPILOT_ACK_STORE)) {
        req.result.createObjectStore(AUTOPILOT_ACK_STORE, { keyPath: "ownerKey" });
      }
      if (!req.result.objectStoreNames.contains(AUTOPILOT_ACK_STATE_STORE)) {
        req.result.createObjectStore(AUTOPILOT_ACK_STATE_STORE, { keyPath: "ownerKey" });
      }

      // v1 only had raw blobs; early v2 could also contain untracked legacy blobs.
      // The upgrade transaction durably quarantines every safe untracked ID. It
      // deliberately never guesses session/turn metadata from a Blob.
      if (event.oldVersion >= 1) {
        const transaction = req.transaction;
        if (!transaction) throw new Error("录音缓存升级缺少事务");
        const blobs = transaction.objectStore(STORE);
        const outbox = transaction.objectStore(OUTBOX_STORE);
        const legacy = transaction.objectStore(LEGACY_STORE);
        const blobKeysRequest = blobs.getAllKeys();
        const outboxKeysRequest = outbox.getAllKeys();
        let blobKeys: IDBValidKey[] | null = null;
        let outboxKeys: IDBValidKey[] | null = null;
        const markMissing = () => {
          if (!blobKeys || !outboxKeys) return;
          for (const rawAudioId of findLegacyBlobIds(blobKeys, outboxKeys)) {
            legacy.put(createLegacyAudioOrphan(rawAudioId, event.oldVersion));
          }
        };
        blobKeysRequest.onsuccess = () => { blobKeys = blobKeysRequest.result; markMissing(); };
        outboxKeysRequest.onsuccess = () => { outboxKeys = outboxKeysRequest.result; markMissing(); };
      }
    };
    req.onsuccess = () => {
      if (blocked) {
        req.result.close();
        return;
      }
      req.result.onversionchange = () => req.result.close();
      resolve(req.result);
    };
    req.onerror = () => reject(req.error);
    req.onblocked = () => {
      blocked = true;
      reject(new Error("录音缓存升级被另一个页面阻塞，请关闭旧页面后重试"));
    };
  });
}

function tx<T>(storeName: string, mode: IDBTransactionMode,
               run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return open().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const transaction = db.transaction(storeName, mode);
        const store = transaction.objectStore(storeName);
        const req = run(store);
        let result: T;
        req.onsuccess = () => { result = req.result; };
        req.onerror = () => reject(req.error);
        // request.onsuccess 只表示操作已排入事务；只有 transaction.oncomplete
        // 才是设备缓存真正持久化/删除成功的回执。
        transaction.oncomplete = () => { db.close(); resolve(result); };
        transaction.onabort = () => { db.close(); reject(transaction.error ?? new Error("IndexedDB 事务已中止")); };
        transaction.onerror = () => { /* onabort 统一返回事务错误 */ };
      }),
  );
}

export const blobStore = {
  get: (rawAudioId: string): Promise<Blob | undefined> =>
    tx<Blob | undefined>(STORE, "readonly", (s) => s.get(rawAudioId) as IDBRequest<Blob | undefined>),
  keys: (): Promise<string[]> =>
    tx<IDBValidKey[]>(STORE, "readonly", (s) => s.getAllKeys()).then((keys) =>
      keys.filter((key): key is string => typeof key === "string")),
  /** Manual server-governed cleanup still refuses to race a live outbox. */
  del: (rawAudioId: string): Promise<AudioCacheDeleteResult> =>
    open().then((db) => new Promise<AudioCacheDeleteResult>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE, LEGACY_STORE], "readwrite");
      const blobs = transaction.objectStore(STORE);
      const outbox = transaction.objectStore(OUTBOX_STORE);
      const legacy = transaction.objectStore(LEGACY_STORE);
      const blobRequest = blobs.getKey(rawAudioId);
      const outboxRequest = outbox.getKey(rawAudioId);
      let blobKey: IDBValidKey | undefined;
      let outboxKey: IDBValidKey | undefined;
      let result: AudioCacheDeleteResult = "missing";
      let ready = 0;
      const decide = () => {
        ready += 1;
        if (ready !== 2) return;
        if (outboxKey !== undefined) {
          result = "preserved-outbox";
          return;
        }
        if (blobKey === undefined) return;
        blobs.delete(rawAudioId);
        legacy.delete(rawAudioId);
        result = "deleted";
      };
      blobRequest.onsuccess = () => { blobKey = blobRequest.result; decide(); };
      outboxRequest.onsuccess = () => { outboxKey = outboxRequest.result; decide(); };
      transaction.oncomplete = () => { db.close(); resolve(result); };
      transaction.onabort = () => {
        db.close();
        reject(transaction.error ?? new Error("录音缓存删除事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    })),

  /** 首次封存必须让 blob 与恢复元数据在同一 IndexedDB 事务里一起成功。 */
  stageOutbox: (entry: AudioOutboxEntry, blob: Blob): Promise<void> => {
    const safe = parseAudioOutboxEntry(entry);
    if (safe.phase !== "captured" || blob.size !== safe.blobBytes
        || (blob.type || "audio/webm") !== safe.mimeType) {
      return Promise.reject(new Error("录音与 outbox 元数据不一致"));
    }
    return open().then((db) => new Promise<void>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE], "readwrite");
      // add 而非 put：同 ID 的本机记录绝不能被另一段字节静默覆盖。
      transaction.objectStore(STORE).add(blob, safe.rawAudioId);
      transaction.objectStore(OUTBOX_STORE).add(safe);
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onabort = () => {
        db.close();
        reject(transaction.error ?? new Error("录音 outbox 封存事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    }));
  },
  putOutbox: (entry: AudioOutboxEntry): Promise<void> => {
    const safe = parseAudioOutboxEntry(entry);
    return tx(OUTBOX_STORE, "readwrite", (s) => s.put(safe)).then(() => undefined);
  },
  getOutbox: (rawAudioId: string): Promise<AudioOutboxEntry | undefined> =>
    tx<unknown>(OUTBOX_STORE, "readonly", (s) => s.get(rawAudioId)).then((value) =>
      value === undefined ? undefined : parseAudioOutboxEntry(value)),
  outboxEntries: (): Promise<AudioOutboxEntry[]> =>
    tx<unknown[]>(OUTBOX_STORE, "readonly", (s) => s.getAll()).then((rows) =>
      rows.map(parseAudioOutboxEntry).sort((a, b) =>
        a.createdAtMs - b.createdAtMs || a.rawAudioId.localeCompare(b.rawAudioId))),
  /**
   * One transaction turns every untracked blob into a durable manual-disposition
   * marker before reporting readiness. Empty outbox alone is never sufficient.
   */
  recoverySnapshot: (): Promise<AudioRecoverySnapshot> =>
    open().then((db) => new Promise<AudioRecoverySnapshot>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE, LEGACY_STORE], "readwrite");
      const blobs = transaction.objectStore(STORE);
      const outbox = transaction.objectStore(OUTBOX_STORE);
      const legacy = transaction.objectStore(LEGACY_STORE);
      const blobKeysRequest = blobs.getAllKeys();
      const entriesRequest = outbox.getAll();
      const markersRequest = legacy.getAll();
      let blobKeys: IDBValidKey[] | null = null;
      let rawEntries: unknown[] | null = null;
      let rawMarkers: unknown[] | null = null;
      let snapshot: AudioRecoverySnapshot | null = null;
      let validationError: unknown = null;
      const assemble = () => {
        if (!blobKeys || !rawEntries || !rawMarkers) return;
        try {
          const entries = rawEntries.map(parseAudioOutboxEntry)
            .sort((a, b) =>
              a.createdAtMs - b.createdAtMs || a.rawAudioId.localeCompare(b.rawAudioId));
          const markers = rawMarkers.map(parseLegacyAudioOrphan)
            .sort((a, b) => a.detectedAtMs - b.detectedAtMs);
          const newIds = findLegacyBlobIds(
            blobKeys,
            entries.map((entry) => entry.rawAudioId),
            markers.map((marker) => marker.rawAudioId),
          );
          const detectedAtMs = Date.now();
          for (const rawAudioId of newIds) {
            const marker = createLegacyAudioOrphan(rawAudioId, DB_VERSION, detectedAtMs);
            legacy.put(marker);
            markers.push(marker);
          }
          snapshot = {
            entries,
            legacyOrphans: markers,
            invalidBlobKeyCount: blobKeys.filter((key) => !isSafeAudioStorageId(key)).length,
          };
        } catch (error) {
          validationError = error;
          transaction.abort();
        }
      };
      blobKeysRequest.onsuccess = () => { blobKeys = blobKeysRequest.result; assemble(); };
      entriesRequest.onsuccess = () => { rawEntries = entriesRequest.result; assemble(); };
      markersRequest.onsuccess = () => { rawMarkers = markersRequest.result; assemble(); };
      transaction.oncomplete = () => {
        db.close();
        if (snapshot) resolve(snapshot);
        else reject(new Error("录音缓存恢复快照未完成"));
      };
      transaction.onabort = () => {
        db.close();
        reject(validationError ?? transaction.error ?? new Error("录音缓存恢复事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    })),
  /**
   * Exact HTTP 410 governance cleanup. The caller must still hold the origin-wide
   * audio Web Lock. Bytes are independently re-read and hashed first; a single
   * readwrite transaction then re-reads all three stores before deleting the
   * blob and outbox together. Legacy-marked material is never auto-discarded.
   */
  discardTerminalOutbox: async (
    entryValue: AudioOutboxEntry,
    disposition: AudioTerminalDisposition,
  ): Promise<TerminalOutboxDiscardResult> => {
    const expected = parseAudioOutboxEntry(entryValue);
    if (disposition.code !== "audio_terminal_disposition"
        || disposition.schemaVersion !== 1
        || disposition.action !== "discard_local_copy"
        || (disposition.reason !== "deleted" && disposition.reason !== "withdrawn")
        || !/^[0-9a-f]{64}$/.test(disposition.checksum)
        || disposition.rawAudioId !== expected.rawAudioId
        || disposition.sessionId !== expected.sessionId
        || disposition.turnKey !== expected.turnKey
        || disposition.byteCount !== expected.blobBytes
        || disposition.containsDirectIdentifier !== expected.containsDirectIdentifier
        || (expected.checksum !== undefined && disposition.checksum !== expected.checksum)) {
      throw new TerminalOutboxIntegrityError("终态处置与 outbox 快照不一致");
    }

    // Second live-byte check. parseAudioTerminalDisposition hashed the pending
    // Blob; this hashes a fresh IndexedDB read before the destructive transaction.
    const reread = await tx<Blob | undefined>(
      STORE,
      "readonly",
      (store) => store.get(expected.rawAudioId) as IDBRequest<Blob | undefined>,
    );
    if (reread !== undefined) {
      if (reread.size !== expected.blobBytes
          || (reread.type || "audio/webm") !== expected.mimeType
          || await sha256Blob(reread) !== disposition.checksum) {
        throw new TerminalOutboxIntegrityError("终态录音现场字节校验失败");
      }
    }

    return open().then((db) => new Promise<TerminalOutboxDiscardResult>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE, LEGACY_STORE], "readwrite");
      const blobs = transaction.objectStore(STORE);
      const outbox = transaction.objectStore(OUTBOX_STORE);
      const legacy = transaction.objectStore(LEGACY_STORE);
      const blobRequest = blobs.get(expected.rawAudioId) as IDBRequest<Blob | undefined>;
      const outboxRequest = outbox.get(expected.rawAudioId) as IDBRequest<unknown | undefined>;
      const legacyRequest = legacy.getKey(expected.rawAudioId);
      let storedBlob: Blob | undefined;
      let storedEntry: unknown | undefined;
      let legacyKey: IDBValidKey | undefined;
      let ready = 0;
      let result: TerminalOutboxDiscardResult | null = null;
      let integrityError: unknown = null;
      const decide = () => {
        ready += 1;
        if (ready !== 3) return;
        try {
          result = planTerminalOutboxDiscard({
            expectedEntry: expected,
            storedEntry,
            storedBlob,
            hasLegacyMarker: legacyKey !== undefined,
          });
          if (result === "discarded") {
            // Both deletes are queued only after every guard passes. Any request
            // failure aborts this transaction and rolls both stores back.
            blobs.delete(expected.rawAudioId);
            outbox.delete(expected.rawAudioId);
          }
        } catch (error) {
          integrityError = error;
          transaction.abort();
        }
      };
      blobRequest.onsuccess = () => { storedBlob = blobRequest.result; decide(); };
      outboxRequest.onsuccess = () => { storedEntry = outboxRequest.result; decide(); };
      legacyRequest.onsuccess = () => { legacyKey = legacyRequest.result; decide(); };
      transaction.oncomplete = () => {
        db.close();
        if (result) resolve(result);
        else reject(new TerminalOutboxIntegrityError("终态录音处置事务未完成"));
      };
      transaction.onabort = () => {
        db.close();
        reject(integrityError ?? transaction.error
          ?? new TerminalOutboxIntegrityError("终态录音处置事务已中止，副本保持不变"));
      };
      transaction.onerror = () => { /* onabort reports the atomic rollback */ };
    }));
  },
  /** Automatic cleanup rechecks both guards in the deletion transaction. */
  deleteIfUnclaimed: (rawAudioId: string): Promise<AudioCacheDeleteResult> =>
    open().then((db) => new Promise<AudioCacheDeleteResult>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE, LEGACY_STORE], "readwrite");
      const blobs = transaction.objectStore(STORE);
      const outbox = transaction.objectStore(OUTBOX_STORE);
      const legacy = transaction.objectStore(LEGACY_STORE);
      const blobRequest = blobs.getKey(rawAudioId);
      const outboxRequest = outbox.getKey(rawAudioId);
      const legacyRequest = legacy.getKey(rawAudioId);
      let blobKey: IDBValidKey | undefined;
      let outboxKey: IDBValidKey | undefined;
      let legacyKey: IDBValidKey | undefined;
      let result: AudioCacheDeleteResult = "missing";
      let ready = 0;
      const decide = () => {
        ready += 1;
        if (ready !== 3) return;
        const decision = automaticCacheDeleteDecision({
          explicitlyPreserved: false,
          hasOutbox: outboxKey !== undefined,
          legacyMarked: legacyKey !== undefined,
        });
        if (decision === "preserve-outbox") result = "preserved-outbox";
        else if (decision === "preserve-legacy") result = "preserved-legacy";
        else if (blobKey !== undefined) {
          blobs.delete(rawAudioId);
          result = "deleted";
        }
      };
      blobRequest.onsuccess = () => { blobKey = blobRequest.result; decide(); };
      outboxRequest.onsuccess = () => { outboxKey = outboxRequest.result; decide(); };
      legacyRequest.onsuccess = () => { legacyKey = legacyRequest.result; decide(); };
      transaction.oncomplete = () => { db.close(); resolve(result); };
      transaction.onabort = () => {
        db.close();
        reject(transaction.error ?? new Error("录音缓存清理事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    })),

  /** Read one capability-scoped ACK owner. The bearer token is never stored. */
  autopilotAckSnapshot: (
    ownerKey: string,
    sessionId: string,
  ): Promise<AutopilotAckStorageSnapshot> =>
    open().then((db) => new Promise<AutopilotAckStorageSnapshot>((resolve, reject) => {
      const transaction = db.transaction(
        [AUTOPILOT_ACK_STORE, AUTOPILOT_ACK_STATE_STORE], "readonly");
      const pendingRequest = transaction.objectStore(AUTOPILOT_ACK_STORE).get(ownerKey);
      const checkpointRequest = transaction.objectStore(AUTOPILOT_ACK_STATE_STORE).get(ownerKey);
      let pendingRaw: unknown;
      let checkpointRaw: unknown;
      let validationError: unknown = null;
      pendingRequest.onsuccess = () => { pendingRaw = pendingRequest.result; };
      checkpointRequest.onsuccess = () => { checkpointRaw = checkpointRequest.result; };
      transaction.oncomplete = () => {
        db.close();
        try {
          const pending = pendingRaw === undefined ? null : parseAutopilotAckEnvelope(pendingRaw);
          const checkpoint = checkpointRaw === undefined
            ? null : parseAutopilotAckCheckpoint(checkpointRaw);
          if ((pending && (pending.ownerKey !== ownerKey || pending.sessionId !== sessionId))
              || (checkpoint
                && (checkpoint.ownerKey !== ownerKey || checkpoint.sessionId !== sessionId))) {
            throw new Error("自动驾驶 ACK 所有权摘要与场次不一致");
          }
          resolve({ pending, checkpoint });
        } catch (error) { reject(error); }
      };
      transaction.onabort = () => {
        db.close();
        reject(validationError ?? transaction.error ?? new Error("ACK outbox 读取事务中止"));
      };
      transaction.onerror = () => { validationError = transaction.error; };
    })),

  /** Persist an immutable exact ACK before any network write. */
  stageAutopilotAck: (value: AutopilotAckEnvelope): Promise<void> => {
    const expected = parseAutopilotAckEnvelope(value);
    return open().then((db) => new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(
        [AUTOPILOT_ACK_STORE, AUTOPILOT_ACK_STATE_STORE], "readwrite");
      const pendingStore = transaction.objectStore(AUTOPILOT_ACK_STORE);
      const checkpointStore = transaction.objectStore(AUTOPILOT_ACK_STATE_STORE);
      const pendingRequest = pendingStore.get(expected.ownerKey);
      const checkpointRequest = checkpointStore.get(expected.ownerKey);
      let pendingRaw: unknown;
      let checkpointRaw: unknown;
      let ready = 0;
      let integrityError: unknown = null;
      const decide = () => {
        ready += 1;
        if (ready !== 2) return;
        try {
          const pending = pendingRaw === undefined ? null : parseAutopilotAckEnvelope(pendingRaw);
          const checkpoint = checkpointRaw === undefined
            ? null : parseAutopilotAckCheckpoint(checkpointRaw);
          if (pending) {
            if (!sameAutopilotAckEnvelope(pending, expected)) {
              throw new Error("已有未完成 ACK，拒绝改写序号或事实");
            }
            return;
          }
          if (checkpoint && (checkpoint.ownerKey !== expected.ownerKey
              || checkpoint.sessionId !== expected.sessionId)) {
            throw new Error("ACK checkpoint 与当前设备场次不一致");
          }
          const lastSeq = checkpoint?.lastDeviceEventSeq ?? 0;
          if (expected.ack.device_event_seq !== lastSeq + 1) {
            throw new Error("自动驾驶 ACK 事件序号不连续");
          }
          pendingStore.add(expected);
        } catch (error) {
          integrityError = error;
          transaction.abort();
        }
      };
      pendingRequest.onsuccess = () => { pendingRaw = pendingRequest.result; decide(); };
      checkpointRequest.onsuccess = () => { checkpointRaw = checkpointRequest.result; decide(); };
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onabort = () => {
        db.close();
        reject(integrityError ?? transaction.error ?? new Error("ACK outbox 封存事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    }));
  },

  /**
   * The record_stopped ACK and removal of its local bytes are one transaction.
   * A crash can therefore leave either the complete audio outbox or the exact
   * immutable ACK, never neither.
   */
  stageRecordStoppedAckAndCompleteAudio: (
    value: AutopilotAckEnvelope,
  ): Promise<void> => {
    const expected = parseAutopilotAckEnvelope(value);
    if (expected.ack.ack_type !== "record_stopped" || expected.command.kind !== "record") {
      return Promise.reject(new Error("录音原件仅可由 record_stopped ACK 原子接管"));
    }
    const stoppedAck = expected.ack;
    const recordCommand = expected.command;
    const rawAudioId = stoppedAck.raw_audio_id;
    return open().then((db) => new Promise<void>((resolve, reject) => {
      const transaction = db.transaction([
        STORE, OUTBOX_STORE, LEGACY_STORE,
        AUTOPILOT_ACK_STORE, AUTOPILOT_ACK_STATE_STORE,
      ], "readwrite");
      const blobs = transaction.objectStore(STORE);
      const outbox = transaction.objectStore(OUTBOX_STORE);
      const legacy = transaction.objectStore(LEGACY_STORE);
      const pendingStore = transaction.objectStore(AUTOPILOT_ACK_STORE);
      const checkpointStore = transaction.objectStore(AUTOPILOT_ACK_STATE_STORE);
      const requests = {
        blob: blobs.get(rawAudioId) as IDBRequest<Blob | undefined>,
        outbox: outbox.get(rawAudioId) as IDBRequest<unknown | undefined>,
        legacy: legacy.getKey(rawAudioId),
        pending: pendingStore.get(expected.ownerKey),
        checkpoint: checkpointStore.get(expected.ownerKey),
      };
      let storedBlob: Blob | undefined;
      let outboxRaw: unknown | undefined;
      let legacyKey: IDBValidKey | undefined;
      let pendingRaw: unknown;
      let checkpointRaw: unknown;
      let ready = 0;
      let integrityError: unknown = null;
      const decide = () => {
        ready += 1;
        if (ready !== 5) return;
        try {
          const pending = pendingRaw === undefined ? null : parseAutopilotAckEnvelope(pendingRaw);
          const checkpoint = checkpointRaw === undefined
            ? null : parseAutopilotAckCheckpoint(checkpointRaw);
          if (checkpoint && (checkpoint.ownerKey !== expected.ownerKey
              || checkpoint.sessionId !== expected.sessionId)) {
            throw new Error("ACK checkpoint 与录音场次或设备所有权不一致");
          }
          if (pending && !sameAutopilotAckEnvelope(pending, expected)) {
            throw new Error("已有未完成 ACK，拒绝用录音事实改写");
          }
          if (!pending) {
            const lastSeq = checkpoint?.lastDeviceEventSeq ?? 0;
            if (checkpoint && checkpoint.sessionId !== expected.sessionId) {
              throw new Error("ACK checkpoint 与录音场次不一致");
            }
            if (expected.ack.device_event_seq !== lastSeq + 1) {
              throw new Error("录音 ACK 事件序号不连续");
            }
          }
          const blobMissing = storedBlob === undefined;
          const outboxMissing = outboxRaw === undefined;
          if (blobMissing !== outboxMissing || legacyKey !== undefined) {
            throw new Error("录音 ACK 接管前本机原件状态不完整");
          }
          if (blobMissing && outboxMissing) {
            if (!pending) throw new Error("未封存 ACK 时录音原件已消失");
            return;
          }
          const entry = parseAudioOutboxEntry(outboxRaw);
          const blob = storedBlob as Blob;
          if (entry.rawAudioId !== rawAudioId
              || entry.sessionId !== expected.sessionId
              || entry.turnKey !== recordCommand.payload.turn_ref
              || entry.phase !== "uploaded"
              || entry.checksum !== stoppedAck.checksum
              || entry.captureReceiptServerSeq !== stoppedAck.receipt_server_seq
              || entry.autopilotStopReason !== stoppedAck.stop_reason
              || entry.blobBytes !== stoppedAck.byte_count
              || entry.durationSeconds !== stoppedAck.duration_seconds
              || blob.size !== entry.blobBytes
              || (blob.type || "audio/webm") !== entry.mimeType) {
            throw new Error("录音 ACK 与 IndexedDB 采集证据不一致");
          }
          if (!pending) pendingStore.add(expected);
          blobs.delete(rawAudioId);
          outbox.delete(rawAudioId);
        } catch (error) {
          integrityError = error;
          transaction.abort();
        }
      };
      requests.blob.onsuccess = () => { storedBlob = requests.blob.result; decide(); };
      requests.outbox.onsuccess = () => { outboxRaw = requests.outbox.result; decide(); };
      requests.legacy.onsuccess = () => { legacyKey = requests.legacy.result; decide(); };
      requests.pending.onsuccess = () => { pendingRaw = requests.pending.result; decide(); };
      requests.checkpoint.onsuccess = () => { checkpointRaw = requests.checkpoint.result; decide(); };
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onabort = () => {
        db.close();
        reject(integrityError ?? transaction.error
          ?? new Error("录音 ACK 原子接管事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    }));
  },

  /** Advance the durable sequence only after an exact server receipt. */
  completeAutopilotAck: (value: AutopilotAckEnvelope): Promise<void> => {
    const expected = parseAutopilotAckEnvelope(value);
    return open().then((db) => new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(
        [AUTOPILOT_ACK_STORE, AUTOPILOT_ACK_STATE_STORE], "readwrite");
      const pendingStore = transaction.objectStore(AUTOPILOT_ACK_STORE);
      const checkpointStore = transaction.objectStore(AUTOPILOT_ACK_STATE_STORE);
      const pendingRequest = pendingStore.get(expected.ownerKey);
      const checkpointRequest = checkpointStore.get(expected.ownerKey);
      let pendingRaw: unknown;
      let checkpointRaw: unknown;
      let ready = 0;
      let integrityError: unknown = null;
      const decide = () => {
        ready += 1;
        if (ready !== 2) return;
        try {
          const pending = pendingRaw === undefined ? null : parseAutopilotAckEnvelope(pendingRaw);
          const checkpoint = checkpointRaw === undefined
            ? null : parseAutopilotAckCheckpoint(checkpointRaw);
          if (!pending || !sameAutopilotAckEnvelope(pending, expected)) {
            throw new Error("服务器收据与待完成 ACK 不一致");
          }
          if (checkpoint && (checkpoint.ownerKey !== expected.ownerKey
              || checkpoint.sessionId !== expected.sessionId)) {
            throw new Error("ACK checkpoint 与待完成收据所有权不一致");
          }
          const lastSeq = checkpoint?.lastDeviceEventSeq ?? 0;
          if (expected.ack.device_event_seq !== lastSeq + 1) {
            throw new Error("ACK checkpoint 不允许跳号完成");
          }
          checkpointStore.put(nextAutopilotAckCheckpoint(expected));
          pendingStore.delete(expected.ownerKey);
        } catch (error) {
          integrityError = error;
          transaction.abort();
        }
      };
      pendingRequest.onsuccess = () => { pendingRaw = pendingRequest.result; decide(); };
      checkpointRequest.onsuccess = () => { checkpointRaw = checkpointRequest.result; decide(); };
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onabort = () => {
        db.close();
        reject(integrityError ?? transaction.error ?? new Error("ACK checkpoint 事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    }));
  },

  /** 最终 audioSaved 已获服务器回执后，原件缓存与 outbox 元数据一起清场。 */
  completeOutbox: (rawAudioId: string): Promise<void> =>
    open().then((db) => new Promise<void>((resolve, reject) => {
      const transaction = db.transaction([STORE, OUTBOX_STORE], "readwrite");
      transaction.objectStore(STORE).delete(rawAudioId);
      transaction.objectStore(OUTBOX_STORE).delete(rawAudioId);
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onabort = () => {
        db.close();
        reject(transaction.error ?? new Error("录音 outbox 完成事务中止"));
      };
      transaction.onerror = () => { /* onabort 统一返回 */ };
    })),
};
