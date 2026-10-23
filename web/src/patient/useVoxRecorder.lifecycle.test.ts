import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";
import { advanceAudioOutbox, createAudioOutboxEntry } from "../audio/audioOutbox.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function sharedAudioLease() {
  let held = false, releases = 0;
  const waiting: Array<() => void> = [];
  return {
    get held() { return held; }, get releases() { return releases; },
    acquire: () => new Promise<unknown>((resolve) => {
      const grant = () => {
        held = true;
        const released = deferred<void>();
        resolve({ released: released.promise, release() {
          assert(held); held = false; releases += 1; released.resolve(); waiting.shift()?.();
        } });
      };
      if (held) waiting.push(grant); else grant();
    }),
  };
}

// Execute the complete production hook, Recorder and persistence validators.
// React scheduling, browser devices, storage and HTTP are controlled ports.
function harness(options: {
  manualStart?: boolean;
  lease?: ReturnType<typeof sharedAudioLease>;
  foreignRecovery?: {
    upload: number | "network";
    saved: number | "exact-410" | "mismatched-410" | "receipt";
    sessionId?: string;
    uploadBarrier?: Promise<void>;
    savedBarrier?: Promise<void>;
    discardBarrier?: Promise<void>;
    completionBarrier?: Promise<void>;
    disposalBarrier?: Promise<void>;
    abortSettlement?: Promise<void>;
    ignoreAbort?: boolean;
    corrupt?: "missing" | "size" | "checksum" | "legacy" | "invalid";
  };
} = {}) {
  const slots: Array<{ value?: unknown; deps?: unknown[]; cleanup?: () => void }> = [];
  let cursor = 0, scheduled = false, mounted = true;
  let effects: Array<() => void> = [];
  type View = { startNow(): Promise<void>; stopAndSave(): Promise<void>; retrySave(): Promise<void>; starting: boolean; recActive: boolean; blockReason: string | null };
  let output: View, hook: (input: object) => View;
  const input = { sessionId: "SIM-LIFECYCLE", turnKey: "itm-0001#1", recording: "idle",
    recSeq: 1, commandSeq: 1, selfStartAllowed: true, connectionReady: true, containsDirectIdentifier: true, stopRequested: false };
  const sameDeps = (a?: unknown[], b?: unknown[]) => !!a && !!b
    && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const schedule = () => {
    if (scheduled || !mounted) return;
    scheduled = true;
    queueMicrotask(() => { scheduled = false; if (mounted) render(); });
  };
  const hooks = {
    useRef(value: unknown) { return (slots[cursor++] ??= { value: { current: value } }).value; },
    useState(initial: unknown) {
      const slot = slots[cursor++] ??= { value: typeof initial === "function" ? initial() : initial };
      return [slot.value, (change: unknown) => {
        const next = typeof change === "function" ? change(slot.value) : change;
        if (!Object.is(next, slot.value)) { slot.value = next; schedule(); }
      }];
    },
    useCallback(callback: unknown, deps: unknown[]) {
      const slot = slots[cursor++] ??= {};
      if (!sameDeps(slot.deps, deps)) { slot.deps = deps; slot.value = callback; }
      return slot.value;
    },
    useEffect(setup: () => (() => void) | undefined, deps?: unknown[]) {
      const slot = slots[cursor++] ??= {};
      if (sameDeps(slot.deps, deps)) return;
      slot.deps = deps; effects.push(() => { slot.cleanup?.(); slot.cleanup = setup(); });
    },
  };
  const document = Object.assign(new EventTarget(), { visibilityState: "visible", onfreeze: null });
  const timers = new Map<number, () => void>();
  let timerId = 0;
  const setTimer = (callback: () => void) => { timers.set(++timerId, callback); return timerId; };
  const window = Object.assign(new EventTarget(), { setTimeout: setTimer, clearTimeout: (id: number) => timers.delete(id) });
  const pendingDigests = new Set<Promise<ArrayBuffer>>();
  const trackedCrypto = { randomUUID: crypto.randomUUID.bind(crypto), subtle: {
    digest(...args: Parameters<SubtleCrypto["digest"]>): Promise<ArrayBuffer> {
      const task = crypto.subtle.digest(...args);
      pendingDigests.add(task);
      void task.then(() => pendingDigests.delete(task), () => pendingDigests.delete(task));
      return task;
    },
  } };
  let auth: (() => Promise<unknown>) = async () => ({ allowed: true, runtime_status: "active", is_simulation: true });
  let acquireStream: (() => Promise<unknown>) = async () => stream;
  let uploadBarrier: Promise<void> | null = null;
  let stageBarrier: Promise<void> | null = null;
  const recoverySignals: Array<AbortSignal | undefined> = [];
  let recoveryAborted = false;
  let microphoneRequests = 0, mediaStarts = 0, mediaStops = 0, trackStops = 0, failUpload = false;
  const stream = { getTracks: () => [{ stop: () => { trackStops += 1; } }] };
  const blobs = new Map<string, Blob>(), entries = new Map<string, unknown>();
  const staged: Array<Record<string, unknown>> = [], saved: Array<Record<string, unknown>> = [];
  const uploads: string[] = [], discarded: string[] = [], disposalReports: unknown[] = [], broadcasts: unknown[] = [];
  if (options.foreignRecovery) {
    const blob = new Blob(["synthetic-old-answer"], { type: "audio/webm" });
    const entry = advanceAudioOutbox(createAudioOutboxEntry({
      rawAudioId: "raw-foreign-recovery-0001", sessionId: options.foreignRecovery.sessionId ?? "SIM-PREVIOUS", turnKey: "itm-0001#1",
      containsDirectIdentifier: false, durationSeconds: 2.5, blob, nowMs: 100,
    }), "registered", { nowMs: 200 });
    blobs.set(entry.rawAudioId, blob); entries.set(entry.rawAudioId, entry);
    if (options.foreignRecovery.corrupt === "missing") blobs.delete(entry.rawAudioId);
    if (options.foreignRecovery.corrupt === "size") blobs.set(entry.rawAudioId, new Blob(["bad"], { type: "audio/webm" }));
    if (options.foreignRecovery.corrupt === "checksum") entries.set(entry.rawAudioId,
      advanceAudioOutbox(entry, "uploaded", { checksum: "00".repeat(32) }));
  }
  const startEvents: Array<() => void> = [];
  class MediaDouble {
    static isTypeSupported() { return true; }
    state = "inactive"; mimeType = "audio/webm";
    onstart?: () => void; ondataavailable?: (event: { data: Blob }) => void; onstop?: () => void;
    start() {
      mediaStarts += 1; this.state = "recording";
      const onstart = this.onstart;
      if (options.manualStart) startEvents.push(() => onstart?.());
      else queueMicrotask(() => this.onstart?.());
    }
    stop() {
      mediaStops += 1; this.state = "inactive";
      queueMicrotask(() => { this.ondataavailable?.({ data: new Blob(["synthetic-before-hide"], { type: this.mimeType }) }); this.onstop?.(); });
    }
  }
  const api = {
    recordingAuthorization: () => auth(), putLiveState: async (_kind: string, payload: object) => payload,
    createAudio: async () => {},
    uploadAudioBlob: async (id: string, blob: Blob, _session: string, signal?: AbortSignal) => {
      uploads.push(id);
      if (options.foreignRecovery && id === "raw-foreign-recovery-0001") {
        recoverySignals.push(signal);
        if (options.foreignRecovery.uploadBarrier) {
          await new Promise<void>((resolve, reject) => {
            options.foreignRecovery!.uploadBarrier!.then(resolve, reject);
            const abort = () => {
              recoveryAborted = true;
              if (options.foreignRecovery!.ignoreAbort) return;
              void (options.foreignRecovery!.abortSettlement ?? Promise.resolve()).then(() =>
                reject(new DOMException("synthetic cancelled upload", "AbortError")));
            };
            if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
          });
        }
        if (options.foreignRecovery.upload === "network") throw new Error("synthetic offline");
        throw new ApiError(options.foreignRecovery.upload, "synthetic upload refusal");
      }
      if (failUpload) throw new Error("synthetic offline");
      if (uploadBarrier) await uploadBarrier;
      const digest = await trackedCrypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      return { raw_audio_id: id, bytes: blob.size, checksum: Buffer.from(digest).toString("hex") };
    },
    putAudioSaved: async (payload: Record<string, unknown>, signal?: AbortSignal) => {
      saved.push(payload);
      if (options.foreignRecovery && payload.rawAudioId === "raw-foreign-recovery-0001") {
        if (options.foreignRecovery.savedBarrier) await new Promise<void>((resolve, reject) => {
          options.foreignRecovery!.savedBarrier!.then(resolve, reject);
          const abort = () => reject(new DOMException("synthetic cancelled metadata", "AbortError"));
          if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
        });
        const status = options.foreignRecovery.saved;
        if (status === "receipt") return { audioReceipt: { rawAudioId: payload.rawAudioId, serverSeq: saved.length, idempotent: false } };
        if (typeof status === "number") throw new ApiError(status, "synthetic metadata refusal");
        throw new ApiError(410, "terminal", {
          code: "audio_terminal_disposition", schemaVersion: 1, action: "discard_local_copy", reason: "deleted",
          rawAudioId: payload.rawAudioId, sessionId: payload.sessionId, turnKey: payload.turnKey,
          byteCount: payload.byteCount, containsDirectIdentifier: payload.containsDirectIdentifier,
          checksum: status === "mismatched-410" ? "00".repeat(32) : payload.checksum,
        }, "nested-detail");
      }
      return { audioReceipt: { rawAudioId: payload.rawAudioId, serverSeq: saved.length, idempotent: false } };
    },
    putAudioDisposalConfirmed: async (disposition: unknown, signal?: AbortSignal) => {
      disposalReports.push(disposition);
      if (options.foreignRecovery?.disposalBarrier) await new Promise<void>((resolve, reject) => {
        options.foreignRecovery!.disposalBarrier!.then(resolve, reject);
        const abort = () => reject(new DOMException("synthetic cancelled disposal", "AbortError"));
        if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
      });
    },
  };
  const blobStore = {
    recoverySnapshot: async () => ({
      invalidBlobKeyCount: options.foreignRecovery?.corrupt === "invalid" ? 1 : 0,
      legacyOrphans: options.foreignRecovery?.corrupt === "legacy" ? ["orphan"] : [],
      entries: [...entries.values()],
    }),
    stageOutbox: async (entry: Record<string, unknown>, blob: Blob) => {
      if (stageBarrier) await stageBarrier;
      staged.push({ ...entry }); entries.set(String(entry.rawAudioId), entry); blobs.set(String(entry.rawAudioId), blob);
    },
    get: async (id: string) => blobs.get(id), getOutbox: async (id: string) => entries.get(id),
    putOutbox: async (entry: Record<string, unknown>) => entries.set(String(entry.rawAudioId), entry),
    outboxEntries: async () => [...entries.values()],
    completeOutbox: async (id: string) => {
      if (options.foreignRecovery?.completionBarrier) await options.foreignRecovery.completionBarrier;
      entries.delete(id); blobs.delete(id);
    },
    discardTerminalOutbox: async (entry: { rawAudioId: string }) => {
      if (options.foreignRecovery?.discardBarrier) await options.foreignRecovery.discardBarrier;
      discarded.push(entry.rawAudioId); entries.delete(entry.rawAudioId); blobs.delete(entry.rawAudioId);
    },
  };
  const modules: Record<string, unknown> = {
    react: hooks,
    "../apiResponse": { ApiError }, "../apiResponse.ts": { ApiError },
    "../api": { api, getRecoveryDeviceCapability: () => null, removeRecoveryDeviceCapabilityIfMatches: () => false },
    "../audio/audioDeviceLease": { acquireAudioDeviceLease: options.lease?.acquire ?? (async () => ({ release() {}, released: Promise.resolve() })), AudioDeviceLeaseUnavailableError: class extends Error {} },
    "../audio/blobStore": { blobStore }, "../sync/bus": { bus: { post(value: unknown) { broadcasts.push(value); } } },
    "../lib/ids": { newAudioId: () => `raw-synthetic-${staged.length + 1}` },
  };
  const context = vm.createContext({
    Blob, DOMException, AbortController, crypto: trackedCrypto, queueMicrotask,
    setTimeout: setTimer, clearTimeout: window.clearTimeout, window, document,
    navigator: { mediaDevices: { getUserMedia: () => { microphoneRequests += 1; return acquireStream(); } } },
    MediaRecorder: MediaDouble, performance: { now: () => 500 },
  });
  const cache = new Map<string, object>();
  const load = (filename: string): object => {
    const cached = cache.get(filename); if (cached) return cached;
    const exports = {}; cache.set(filename, exports);
    const source = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText;
    const require = (name: string) => Object.hasOwn(modules, name) ? modules[name]
      : load(resolve(dirname(filename), name.endsWith(".ts") ? name : `${name}.ts`));
    vm.runInContext(`(function(exports, require){${source}\n})`, context)(exports, require);
    return exports;
  };
  hook = (load(fileURLToPath(new URL("./useVoxRecorder.ts", import.meta.url))) as { useVoxRecorder: typeof hook }).useVoxRecorder;
  function render() { cursor = 0; effects = []; output = hook(input); const pending = effects; effects = []; for (const effect of pending) effect(); }
  render();
  return {
    get view() { return output; }, counters: () => ({ microphoneRequests, mediaStarts, mediaStops, trackStops }),
    entries, blobs, staged, saved, stream, uploads, discarded, disposalReports, broadcasts, recoverySignals,
    get recoveryAborted() { return recoveryAborted; },
    deliverStart: (index: number) => startEvents[index]?.(),
    setAuthorization: (value: () => Promise<unknown>) => { auth = value; },
    setStream: (value: () => Promise<unknown>) => { acquireStream = value; },
    holdUpload: (value: Promise<void>) => { uploadBarrier = value; },
    holdStage: (value: Promise<void>) => { stageBarrier = value; },
    offline: (value: boolean) => { failUpload = value; },
    update: (values: Partial<typeof input>) => { Object.assign(input, values); render(); },
    hide(type = "visibilitychange") {
      if (type === "visibilitychange") document.visibilityState = "hidden";
      (type === "pagehide" ? window : document).dispatchEvent(new Event(type));
    },
    show(type = "visibilitychange") {
      document.visibilityState = "visible";
      (type === "pageshow" ? window : document).dispatchEvent(new Event(type));
    },
    fire(type: string) { (type === "pageshow" ? window : document).dispatchEvent(new Event(type)); },
    setHiddenWithoutEvent() { document.visibilityState = "hidden"; },
    async flush() {
      for (let i = 0; i < 25; i += 1) {
        await new Promise<void>((done) => setImmediate(done));
        // Event-loop turns do not imply WebCrypto worker completion on busy CI.
        // Join real digests while leaving explicit authorization/upload gates held.
        await Promise.all([...pendingDigests]);
      }
    },
    remount() {
      mounted = true;
      for (const slot of slots) slot.deps = undefined;
      render();
    },
    async close() {
      mounted = false;
      for (const slot of slots) { const cleanup = slot.cleanup; slot.cleanup = undefined; cleanup?.(); }
      for (let i = 0; i < 30; i += 1) await Promise.resolve();
    },
  };
}

test("准备页退出会取消慢旧录音恢复，待请求真正结束后交锁给后续录音", async () => {
  const lease = sharedAudioLease(), upload = deferred<void>(), settled = deferred<void>();
  const h = harness({ lease, foreignRecovery: {
    upload: 404, saved: 409, uploadBarrier: upload.promise, abortSettlement: settled.promise,
  } });
  await h.flush();
  const before = JSON.stringify([...h.entries]);
  await h.close();
  const next = harness({ lease });
  try {
    assert.equal(h.recoveryAborted, true, "退出准备页应主动取消旧恢复请求");
    await next.flush();
    assert.equal(lease.releases, 0, "AbortSignal 发出不等于请求已结束，仍须持锁");
    assert.equal(next.counters().microphoneRequests, 0);
    settled.resolve(); await h.flush(); await next.flush();
    assert.equal(lease.releases, 1, "不等待 60 秒上传超时即可完成锁交接");
    await next.view.startNow(); await next.flush();
    assert.equal(next.counters().mediaStarts, 1);
    assert.equal(h.saved.length, 0, "取消后不得继续发起 audioSaved 探测");
    assert.equal(JSON.stringify([...h.entries]), before);
    assert.equal(await h.blobs.get("raw-foreign-recovery-0001")?.text(), "synthetic-old-answer");
  } finally { upload.resolve(); settled.resolve(); await h.flush(); await next.close(); }
});

test("不响应取消的迟到上传拒绝结束前不交锁，结束后不继续旧恢复链", async () => {
  const lease = sharedAudioLease(), upload = deferred<void>();
  const h = harness({ lease, foreignRecovery: {
    upload: 404, saved: "exact-410", uploadBarrier: upload.promise, ignoreAbort: true,
  } });
  await h.flush(); await h.close();
  try {
    assert.equal(lease.releases, 0);
    upload.resolve(); await h.flush();
    assert.equal(lease.releases, 1);
    assert.equal(h.saved.length, 0, "迟到 404 不得越过取消围栏继续请求或删除");
    assert.equal(h.discarded.length, 0); assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
  } finally { upload.resolve(); await h.flush(); }
});

test("退出准备页会取消旧录音元数据核对，保留字节且不继续终态删除", async () => {
  const lease = sharedAudioLease(), metadata = deferred<void>();
  const h = harness({ lease, foreignRecovery: { upload: 404, saved: "exact-410", savedBarrier: metadata.promise } });
  await h.flush(); assert.equal(h.saved.length, 1);
  await h.close(); await h.flush();
  try {
    assert.equal(lease.releases, 1); assert.equal(h.discarded.length, 0);
    assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
  } finally { metadata.resolve(); await h.flush(); }
});

test("精确终态清理已进入本地事务时，取消后仍等待该事务结束才交锁", async () => {
  const lease = sharedAudioLease(), discard = deferred<void>();
  const h = harness({ lease, foreignRecovery: { upload: 404, saved: "exact-410", discardBarrier: discard.promise } });
  await h.flush(); assert.equal(h.saved.length, 1);
  await h.close(); await h.flush();
  try {
    assert.equal(lease.releases, 0); assert.equal(h.blobs.size, 1);
    discard.resolve(); await h.flush();
    assert.equal(lease.releases, 1); assert.equal(h.discarded.length, 1);
    assert.equal(h.entries.size, 0); assert.equal(h.blobs.size, 0);
    assert.equal(h.disposalReports.length, 0, "取消后不发起新的 best-effort 治理上报");
  } finally { discard.resolve(); await h.flush(); }
});

test("旧副本已精确清理后的治理上报可取消，不能继续占用麦克风锁", async () => {
  const lease = sharedAudioLease(), report = deferred<void>();
  const h = harness({ lease, foreignRecovery: { upload: 404, saved: "exact-410", disposalBarrier: report.promise } });
  await h.flush(); assert.equal(h.disposalReports.length, 1); assert.equal(h.blobs.size, 0);
  await h.close(); await h.flush();
  try { assert.equal(lease.releases, 1); assert.equal(h.discarded.length, 1); }
  finally { report.resolve(); await h.flush(); }
});

for (const saved of ["receipt", "exact-410"] as const) {
  test(`StrictMode 取消当前恢复的 ${saved} 本地事务后不留下已删除的重试指针`, async () => {
    const lease = sharedAudioLease(), completion = deferred<void>();
    const h = harness({ lease, foreignRecovery: {
      upload: 404, saved, sessionId: "SIM-LIFECYCLE",
      ...(saved === "receipt" ? { completionBarrier: completion.promise } : { discardBarrier: completion.promise }),
    } });
    await h.flush(); assert.equal(h.saved.length, 1); assert.equal(h.entries.size, 1);
    await h.close(); h.remount();
    try {
      assert.equal(lease.releases, 0);
      completion.resolve(); await h.flush();
      assert.equal(h.entries.size, 0); assert.equal(h.blobs.size, 0); assert.equal(lease.releases, 1);
      if (saved === "receipt") {
        await h.view.startNow(); await h.flush();
        assert.equal(h.counters().mediaStarts, 1, "空恢复扫描不能被已删除条目的旧 pendingSave 指针阻断");
      } else {
        await h.view.retrySave(); await h.flush();
        assert.equal(h.saved.length, 1, "精确终态已清理的副本不能从陈旧 pendingSave 再次上传或探测");
        assert.equal(h.view.blockReason, "terminal-discarded");
        await h.view.startNow(); await h.flush();
        assert.equal(h.counters().mediaStarts, 0, "当前场次精确终态不能因空扫描重新开麦");
        h.update({ sessionId: "SIM-AFTER-TERMINAL" }); await h.flush();
        await h.view.startNow(); await h.flush();
        assert.equal(h.counters().mediaStarts, 1, "旧场次终态事实不能阻断独立新场次");
      }
    } finally { completion.resolve(); await h.close(); await h.flush(); }
  });
}

test("退出时当前录音必须完成本地封存与正在保存的链，不能因取消恢复提前交锁", async () => {
  const lease = sharedAudioLease(), stage = deferred<void>(), upload = deferred<void>();
  const h = harness({ lease }); await h.flush();
  await h.view.startNow(); await h.flush();
  h.holdStage(stage.promise); h.holdUpload(upload.promise);
  const saving = h.view.stopAndSave(); await h.flush(); await h.close();
  try {
    assert.equal(lease.releases, 0); assert.equal(h.staged.length, 0);
    stage.resolve(); await h.flush();
    assert.equal(h.staged.length, 1); assert.equal(h.blobs.size, 1);
    assert.equal(lease.releases, 0, "当前保存不能被恢复取消策略中断");
    upload.resolve(); await saving; await h.flush();
    assert.equal(h.saved.length, 1); assert.equal(h.entries.size, 0); assert.equal(h.blobs.size, 0);
    assert.equal(lease.releases, 1);
  } finally { stage.resolve(); upload.resolve(); await h.flush(); }
});

for (const upload of [404, 409]) {
  test(`人工恢复旧场次上传 ${upload} 后核对精确 410，清理旧副本并允许新场次录音`, async () => {
    const h = harness({ foreignRecovery: { upload, saved: "exact-410" } }); await h.flush();
    try {
      assert.deepEqual(h.uploads, ["raw-foreign-recovery-0001"]);
      assert.equal(h.saved.length, 1, "上传隐藏 404 或冲突只能触发一次权威元数据核对");
      assert.equal(h.saved[0].sessionId, "SIM-PREVIOUS");
      assert.deepEqual(h.discarded, ["raw-foreign-recovery-0001"]);
      assert.equal(h.entries.size, 0); assert.equal(h.blobs.size, 0);
      assert.equal(h.disposalReports.length, 1);
      assert.equal(h.broadcasts.length, 0, "终态处置不能向新场次广播保存成功");
      await h.view.startNow(); await h.flush();
      assert.equal(h.counters().mediaStarts, 1, "旧场次精确处置完毕后解除恢复阻断");
    } finally { await h.close(); }
  });
}

for (const upload of [401, 403, 422, 429, 500, "network"] as const) {
  test(`人工恢复旧场次上传 ${upload} 保留副本，不探测或删除`, async () => {
    const h = harness({ foreignRecovery: { upload, saved: "exact-410" } }); await h.flush();
    try {
      assert.equal(h.saved.length, 0); assert.equal(h.discarded.length, 0);
      assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
      assert.equal(h.disposalReports.length, 0); assert.equal(h.broadcasts.length, 0);
      await h.view.startNow(); await h.flush();
      assert.equal(h.counters().mediaStarts, 1, "有效旧场次录音保留但不阻断当前场次");
      assert.equal(await h.blobs.get("raw-foreign-recovery-0001")?.text(), "synthetic-old-answer");
    } finally { await h.close(); }
  });
}

for (const saved of [401, 403, 404, 409, 410, 500, "mismatched-410"] as const) {
  test(`人工恢复上传隐藏 404 后元数据 ${saved} 不是精确终态，仍保留副本`, async () => {
    const h = harness({ foreignRecovery: { upload: 404, saved } }); await h.flush();
    try {
      assert.equal(h.saved.length, 1);
      assert.equal(h.discarded.length, 0); assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
      assert.equal(h.disposalReports.length, 0); assert.equal(h.broadcasts.length, 0);
      await h.view.startNow(); await h.flush();
      assert.equal(h.counters().mediaStarts, 1, "未获旧场次处置权限不阻断当前场次");
      await h.view.stopAndSave(); await h.flush();
      assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
      assert.equal(await h.blobs.get("raw-foreign-recovery-0001")?.text(), "synthetic-old-answer");
    } finally { await h.close(); }
  });
}

test("当前场次未保存录音继续阻止开新麦，重试仍指向原副本", async () => {
  const h = harness({ foreignRecovery: { upload: 404, saved: 409, sessionId: "SIM-LIFECYCLE" } });
  await h.flush();
  try {
    await h.view.startNow(); await h.flush();
    assert.equal(h.counters().microphoneRequests, 0);
    await h.view.retrySave(); await h.flush();
    assert.equal(h.uploads.length, 2);
    assert.equal(h.entries.size, 1); assert.equal(h.blobs.size, 1);
  } finally { await h.close(); }
});

for (const corrupt of ["missing", "size", "checksum", "legacy", "invalid"] as const) {
  test(`旧场次本地存储 ${corrupt} 异常继续阻断开麦`, async () => {
    const h = harness({ foreignRecovery: { upload: 404, saved: 409, corrupt } }); await h.flush();
    try {
      await h.view.startNow(); await h.flush();
      assert.equal(h.counters().microphoneRequests, 0);
      assert.equal(h.uploads.length, 0); assert.equal(h.discarded.length, 0);
      assert.equal(h.entries.size, 1);
    } finally { await h.close(); }
  });
}

for (const trigger of ["visibilitychange", "pagehide", "freeze"]) {
  test(`人工录音 ${trigger} 同步关麦并保存原题，离线副本保留且重试不重录`, async () => {
    const h = harness(); await h.flush();
    try {
      await h.view.startNow(); await h.flush(); assert.equal(h.counters().mediaStarts, 1);
      h.offline(true); h.hide(trigger);
      assert.equal(h.counters().mediaStops, 1, "原事件内必须停止采集");
      assert(h.counters().trackStops > 0, "原事件内必须关闭真实音轨");
      await h.flush(); assert.equal(h.staged.length, 1);
      assert.equal(h.staged[0].sessionId, "SIM-LIFECYCLE"); assert.equal(h.staged[0].turnKey, "itm-0001#1");
      assert.equal(h.staged[0].containsDirectIdentifier, true); assert.equal(h.entries.size, 1); assert.equal(h.saved.length, 0);
      h.show(); await h.flush(); assert.equal(h.counters().mediaStarts, 1, "回前台不自动重录");
      h.offline(false); await h.view.retrySave(); await h.flush();
      assert.equal(h.saved.length, 1); assert.equal(h.entries.size, 0);
      assert.equal(h.saved[0].rawAudioId, h.staged[0].rawAudioId); assert.equal(h.saved[0].turnKey, "itm-0001#1");
      assert.equal(h.counters().mediaStarts, 1);
    } finally { await h.close(); }
  });
}

test("人工录音等待授权时离开，迟到授权不请求麦克风", async () => {
  const h = harness(); await h.flush();
  const authorization = deferred<unknown>(); h.setAuthorization(() => authorization.promise);
  const starting = h.view.startNow(); h.hide();
  authorization.resolve({ allowed: true, runtime_status: "active", is_simulation: true });
  await starting; await h.flush();
  try { assert.equal(h.counters().microphoneRequests, 0); assert.equal(h.staged.length, 0); } finally { await h.close(); }
});

test("人工录音等待权限流时离开，迟到流立即关闭且零次开录", async () => {
  const h = harness(); await h.flush();
  const pendingStream = deferred<unknown>(); h.setStream(() => pendingStream.promise);
  const starting = h.view.startNow(); await h.flush(); h.hide(); pendingStream.resolve(h.stream); await starting; await h.flush();
  try { assert.equal(h.counters().mediaStarts, 0); assert(h.counters().trackStops > 0); assert.equal(h.staged.length, 0); } finally { await h.close(); }
});

test("浏览器已隐藏但事件尚未送达，准备好的权限流也不得进入开录", async () => {
  const h = harness(); await h.flush();
  const pendingStream = deferred<unknown>(); h.setStream(() => pendingStream.promise);
  const starting = h.view.startNow(); await h.flush(); h.setHiddenWithoutEvent(); pendingStream.resolve(h.stream); await starting; await h.flush();
  try { assert.equal(h.counters().mediaStarts, 0); assert(h.counters().trackStops > 0); assert.equal(h.staged.length, 0); } finally { await h.close(); }
});

test("新提示在原生开录回执前撤销启动，先关真实音轨再解除 starting，迟到回执不能复活旧流", async () => {
  const h = harness({ manualStart: true }); await h.flush();
  try {
    const starting = h.view.startNow(); await h.flush();
    assert.equal(h.counters().mediaStarts, 1);
    assert.equal(h.view.starting, true); assert.equal(h.view.recActive, false);

    h.update({ stopRequested: true, selfStartAllowed: false });
    assert.equal(h.counters().mediaStops, 1, "撤销启动的同一调用栈必须停止原生采集");
    assert.equal(h.counters().trackStops, 1, "不能仅清 starting 就放行新提示音");
    await starting; await h.flush();
    assert.equal(h.view.starting, false); assert.equal(h.staged.length, 0);

    h.update({ stopRequested: false, selfStartAllowed: true }); await h.flush();
    const fresh = h.view.startNow(); await h.flush();
    assert.equal(h.counters().mediaStarts, 2);
    h.deliverStart(0); await h.flush();
    assert.equal(h.view.starting, true); assert.equal(h.view.recActive, false);
    assert.equal(h.counters().mediaStops, 1, "旧 onstart 不得收掉新许可的流");
    h.deliverStart(1); await fresh; await h.flush();
    assert.equal(h.view.recActive, true);
    await h.view.stopAndSave(); await h.flush();
    assert.equal(h.saved.length, 1, "只保存新许可实际完成的作答");
  } finally { await h.close(); }
});

test("新提示撤销已开录的回答仍走唯一保存链，后续撤销不丢弃正在保存的内容", async () => {
  const h = harness(); await h.flush();
  const upload = deferred<void>(); h.holdUpload(upload.promise);
  try {
    await h.view.startNow(); await h.flush();
    h.update({ stopRequested: true, selfStartAllowed: false }); await h.flush();
    assert.equal(h.counters().mediaStops, 1); assert.equal(h.staged.length, 1);
    assert.equal(h.saved.length, 0);
    const rawAudioId = h.staged[0].rawAudioId;
    h.update({ turnKey: "itm-0002#1" }); await h.flush();
    assert.equal(h.staged.length, 1); assert.equal(h.entries.size, 1);
    upload.resolve(); await h.flush();
    assert.equal(h.saved.length, 1); assert.equal(h.saved[0].rawAudioId, rawAudioId);
    assert.equal(h.saved[0].turnKey, "itm-0001#1");
  } finally { upload.resolve(); await h.close(); }
});

test("后台收到远程 arm，回前台不能自动执行它；新的明确 arm 才能开录", async () => {
  const h = harness(); await h.flush();
  try {
    h.hide(); h.update({ recording: "armed", recSeq: 2, commandSeq: 2 }); await h.flush(); assert.equal(h.counters().microphoneRequests, 0);
    h.show(); h.update({ commandSeq: 3 }); await h.flush(); assert.equal(h.counters().microphoneRequests, 0);
    h.update({ recSeq: 3, commandSeq: 4 }); await h.flush(); assert.equal(h.counters().mediaStarts, 1);
    await h.view.stopAndSave(); await h.flush();
  } finally { await h.close(); }
});

test("正常收麦已进入上传时离开，保留同一 outbox 并等待原始保存完成", async () => {
  const h = harness(); await h.flush();
  const upload = deferred<void>(); h.holdUpload(upload.promise);
  try {
    await h.view.startNow();
    const saving = h.view.stopAndSave(); await h.flush();
    assert.equal(h.entries.size, 1); assert.equal(h.saved.length, 0);
    const originalId = h.staged[0].rawAudioId;
    h.hide(); h.hide("pagehide"); h.hide("freeze"); await h.flush();
    assert.equal(h.entries.size, 1); assert.equal(h.staged.length, 1);
    assert.equal(h.counters().mediaStops, 1);
    upload.resolve(); await saving; await h.flush();
    assert.equal(h.saved.length, 1); assert.equal(h.saved[0].rawAudioId, originalId);
    assert.equal(h.entries.size, 0);
  } finally { upload.resolve(); await h.close(); }
});

test("前台的服务端拒绝仍阻止开麦，生命周期修复不放宽授权", async () => {
  const h = harness(); await h.flush();
  h.setAuthorization(async () => ({ allowed: false, runtime_status: "active", is_simulation: true }));
  try {
    await h.view.startNow(); await h.flush();
    assert.equal(h.counters().microphoneRequests, 0); assert.equal(h.staged.length, 0);
  } finally { await h.close(); }
});

for (const [leave, resume] of [["pagehide", "pageshow"], ["freeze", "resume"]]) {
  test(`${leave} → ${resume} 无可见性边沿时允许新的明确 arm，但不复活旧指令`, async () => {
    const h = harness(); await h.flush();
    try {
      h.hide(leave); h.update({ recording: "armed", recSeq: 2, commandSeq: 2 }); await h.flush();
      h.show(resume); h.update({ commandSeq: 3 }); await h.flush();
      assert.equal(h.counters().microphoneRequests, 0);
      h.update({ recSeq: 3, commandSeq: 4 }); await h.flush();
      assert.equal(h.counters().mediaStarts, 1);
      await h.view.stopAndSave(); await h.flush();
    } finally { await h.close(); }
  });
  test(`${resume} 在页面仍隐藏时不放行自助开录`, async () => {
    const h = harness(); await h.flush();
    try {
      h.hide(); h.fire(resume); await h.view.startNow(); await h.flush();
      assert.equal(h.counters().microphoneRequests, 0);
      h.show(resume); await h.view.startNow(); await h.flush();
      assert.equal(h.counters().mediaStarts, 1, "前台恢复后仍需新的明确点击");
      await h.view.stopAndSave(); await h.flush();
    } finally { await h.close(); }
  });
}
