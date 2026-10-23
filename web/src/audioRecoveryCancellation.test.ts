import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

function apiHarness() {
  const timers = new Map<number, () => void>();
  let timerId = 0;
  const storage = () => {
    const values = new Map<string, string>();
    return { getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => values.delete(key) };
  };
  const requests: Array<{ path: string; signal: AbortSignal }> = [];
  const events: string[] = [];
  let respond: (() => Response | Promise<Response>) | null = null;
  const context = vm.createContext({
    Blob, DOMException, AbortController, URLSearchParams, crypto, queueMicrotask, Event,
    localStorage: storage(), sessionStorage: storage(), document: { cookie: "" },
    window: { setTimeout: (callback: () => void) => { timers.set(++timerId, callback); return timerId; },
      clearTimeout: (id: number) => timers.delete(id), dispatchEvent(event: Event) { events.push(event.type); } },
    fetch: (path: string, init: RequestInit) => {
      const signal = init.signal as AbortSignal;
      requests.push({ path, signal });
      if (respond) return Promise.resolve(respond());
      return new Promise<Response>((_resolve, reject) => {
        const abort = () => reject(new DOMException("synthetic fetch abort", "AbortError"));
        if (signal.aborted) abort(); else signal.addEventListener("abort", abort, { once: true });
      });
    },
  });
  const cache = new Map<string, object>();
  const load = (filename: string): object => {
    const cached = cache.get(filename); if (cached) return cached;
    const exports = {}; cache.set(filename, exports);
    const source = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText;
    const require = (name: string) => load(resolve(dirname(filename), name.endsWith(".ts") ? name : `${name}.ts`));
    vm.runInContext(`(function(exports, require){${source}\n})`, context)(exports, require);
    return exports;
  };
  const { api } = load(fileURLToPath(new URL("./api.ts", import.meta.url))) as {
    api: {
      createAudio(body: object, signal?: AbortSignal): Promise<unknown>;
      uploadAudioBlob(id: string, blob: Blob, session: string, signal?: AbortSignal): Promise<unknown>;
      putAudioSaved(body: object, signal?: AbortSignal): Promise<unknown>;
      putAudioDisposalConfirmed(body: object, signal?: AbortSignal): Promise<unknown>;
    };
  };
  return { api, requests, timers, events, respond: (callback: () => Response | Promise<Response>) => { respond = callback; } };
}

function trackedAbortController() {
  const controller = new AbortController();
  let listeners = 0;
  const signal = controller.signal;
  const add = signal.addEventListener.bind(signal), remove = signal.removeEventListener.bind(signal);
  signal.addEventListener = (...args: Parameters<typeof signal.addEventListener>) => { listeners += 1; add(...args); };
  signal.removeEventListener = (...args: Parameters<typeof signal.removeEventListener>) => { listeners -= 1; remove(...args); };
  return { controller, signal, get listeners() { return listeners; } };
}

const audio = { raw_audio_id: "raw-cancel-1", session_id: "SIM-CANCEL", turn_key: "itm-0001#1" };
for (const operation of ["register", "upload", "saved", "disposal"] as const) {
  test(`录音恢复 ${operation} 请求传递取消且释放超时器和调用方监听器`, async () => {
    const h = apiHarness(), cancellation = trackedAbortController();
    const signal = cancellation.signal;
    const pending = operation === "register" ? h.api.createAudio(audio, signal)
      : operation === "upload" ? h.api.uploadAudioBlob(audio.raw_audio_id, new Blob(["synthetic"]), audio.session_id, signal)
      : operation === "saved" ? h.api.putAudioSaved({ sessionId: audio.session_id }, signal)
      : h.api.putAudioDisposalConfirmed({ sessionId: audio.session_id }, signal);
    assert.equal(h.requests.length, 1); assert.equal(cancellation.listeners, 1);
    cancellation.controller.abort();
    await assert.rejects(pending, (error: unknown) => (error as Error).name === "AbortError");
    assert.equal(h.requests[0].signal.aborted, true); assert.equal(h.timers.size, 0);
    assert.equal(cancellation.listeners, 0);
  });
}

test("音频上传原有 60 秒超时继续报告 408，与主动取消区分", async () => {
  const h = apiHarness();
  const pending = h.api.uploadAudioBlob(audio.raw_audio_id, new Blob(["synthetic"]), audio.session_id);
  assert.equal(h.timers.size, 1);
  [...h.timers.values()][0]();
  await assert.rejects(pending, (error: unknown) => (error as { status?: number }).status === 408);
  assert.equal(h.timers.size, 0);
});

test("音频上传正常完成也移除取消监听，不改变上传回执", async () => {
  const h = apiHarness(), cancellation = trackedAbortController();
  const receipt = { raw_audio_id: audio.raw_audio_id, bytes: 9, checksum: "synthetic", format: "webm" };
  h.respond(() => Response.json(receipt));
  const result = await h.api.uploadAudioBlob(audio.raw_audio_id, new Blob(["synthetic"]), audio.session_id, cancellation.signal);
  assert.equal(JSON.stringify(result), JSON.stringify(receipt));
  assert.equal(cancellation.listeners, 0); assert.equal(h.timers.size, 0);
});

for (const operation of ["register", "upload", "saved", "disposal"] as const) {
  test(`录音恢复 ${operation} 取消后迟到 401 不再更改凭据或唤起重新配对`, async () => {
    const h = apiHarness(), cancellation = trackedAbortController();
    let deliver!: (value: Response) => void;
    const response = new Promise<Response>((resolve) => { deliver = resolve; });
    h.respond(() => response);
    const signal = cancellation.signal;
    const pending = operation === "register" ? h.api.createAudio(audio, signal)
      : operation === "upload" ? h.api.uploadAudioBlob(audio.raw_audio_id, new Blob(["synthetic"]), audio.session_id, signal)
      : operation === "saved" ? h.api.putAudioSaved({ sessionId: audio.session_id }, signal)
      : h.api.putAudioDisposalConfirmed({ sessionId: audio.session_id }, signal);
    cancellation.controller.abort();
    deliver(Response.json({ detail: "synthetic expired capability" }, { status: 401 }));
    await assert.rejects(pending, (error: unknown) => (error as Error).name === "AbortError");
    assert.deepEqual(h.events, [], "已取消的响应不得触发新的重新配对提示");
    assert.equal(cancellation.listeners, 0); assert.equal(h.timers.size, 0);
  });
}

test("主动取消后即使超时器在 fetch 落稳前到期，上传仍报告 AbortError", async () => {
  const h = apiHarness(), cancellation = trackedAbortController();
  const pending = h.api.uploadAudioBlob(audio.raw_audio_id, new Blob(["synthetic"]), audio.session_id, cancellation.signal);
  cancellation.controller.abort();
  [...h.timers.values()][0]();
  await assert.rejects(pending, (error: unknown) => (error as Error).name === "AbortError");
  assert.equal(cancellation.listeners, 0); assert.equal(h.timers.size, 0);
});
