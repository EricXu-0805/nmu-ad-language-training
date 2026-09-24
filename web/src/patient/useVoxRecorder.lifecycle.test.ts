import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

// Execute the complete production hook, Recorder and persistence validators.
// React scheduling, browser devices, storage and HTTP are controlled ports.
function harness() {
  const slots: Array<{ value?: unknown; deps?: unknown[]; cleanup?: () => void }> = [];
  let cursor = 0, scheduled = false, mounted = true;
  let effects: Array<() => void> = [];
  type View = { startNow(): Promise<void>; stopAndSave(): Promise<void>; retrySave(): Promise<void> };
  let output: View, hook: (input: object) => View;
  const input = { sessionId: "SIM-LIFECYCLE", turnKey: "itm-0001#1", recording: "idle",
    recSeq: 1, commandSeq: 1, selfStartAllowed: true, connectionReady: true, containsDirectIdentifier: true };
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
  let auth: (() => Promise<unknown>) = async () => ({ allowed: true, runtime_status: "active", is_simulation: true });
  let acquireStream: (() => Promise<unknown>) = async () => stream;
  let uploadBarrier: Promise<void> | null = null;
  let microphoneRequests = 0, mediaStarts = 0, mediaStops = 0, trackStops = 0, failUpload = false;
  const stream = { getTracks: () => [{ stop: () => { trackStops += 1; } }] };
  const blobs = new Map<string, Blob>(), entries = new Map<string, unknown>();
  const staged: Array<Record<string, unknown>> = [], saved: Array<Record<string, unknown>> = [];
  class MediaDouble {
    static isTypeSupported() { return true; }
    state = "inactive"; mimeType = "audio/webm";
    onstart?: () => void; ondataavailable?: (event: { data: Blob }) => void; onstop?: () => void;
    start() { mediaStarts += 1; this.state = "recording"; queueMicrotask(() => this.onstart?.()); }
    stop() {
      mediaStops += 1; this.state = "inactive";
      queueMicrotask(() => { this.ondataavailable?.({ data: new Blob(["synthetic-before-hide"], { type: this.mimeType }) }); this.onstop?.(); });
    }
  }
  const api = {
    recordingAuthorization: () => auth(), putLiveState: async (_kind: string, payload: object) => payload,
    createAudio: async () => {},
    uploadAudioBlob: async (id: string, blob: Blob) => {
      if (failUpload) throw new Error("synthetic offline");
      if (uploadBarrier) await uploadBarrier;
      const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
      return { raw_audio_id: id, bytes: blob.size, checksum: Buffer.from(digest).toString("hex") };
    },
    putAudioSaved: async (payload: Record<string, unknown>) => {
      saved.push(payload);
      return { audioReceipt: { rawAudioId: payload.rawAudioId, serverSeq: saved.length, idempotent: false } };
    },
  };
  const blobStore = {
    recoverySnapshot: async () => ({ invalidBlobKeyCount: 0, legacyOrphans: [], entries: [...entries.values()] }),
    stageOutbox: async (entry: Record<string, unknown>, blob: Blob) => {
      staged.push({ ...entry }); entries.set(String(entry.rawAudioId), entry); blobs.set(String(entry.rawAudioId), blob);
    },
    get: async (id: string) => blobs.get(id), getOutbox: async (id: string) => entries.get(id),
    putOutbox: async (entry: Record<string, unknown>) => entries.set(String(entry.rawAudioId), entry),
    outboxEntries: async () => [...entries.values()],
    completeOutbox: async (id: string) => { entries.delete(id); blobs.delete(id); },
  };
  const modules: Record<string, unknown> = {
    react: hooks,
    "../api": { api, getRecoveryDeviceCapability: () => null, removeRecoveryDeviceCapabilityIfMatches: () => false },
    "../audio/audioDeviceLease": { acquireAudioDeviceLease: async () => ({ release() {}, released: Promise.resolve() }), AudioDeviceLeaseUnavailableError: class extends Error {} },
    "../audio/blobStore": { blobStore }, "../sync/bus": { bus: { post() {} } },
    "../lib/ids": { newAudioId: () => `raw-synthetic-${staged.length + 1}` },
  };
  const context = vm.createContext({
    Blob, DOMException, AbortController, crypto, queueMicrotask,
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
    entries, blobs, staged, saved, stream,
    setAuthorization: (value: () => Promise<unknown>) => { auth = value; },
    setStream: (value: () => Promise<unknown>) => { acquireStream = value; },
    holdUpload: (value: Promise<void>) => { uploadBarrier = value; },
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
    async flush() { for (let i = 0; i < 25; i += 1) await new Promise<void>((done) => setImmediate(done)); },
    async close() { mounted = false; for (const slot of slots) slot.cleanup?.(); for (let i = 0; i < 30; i += 1) await Promise.resolve(); },
  };
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
