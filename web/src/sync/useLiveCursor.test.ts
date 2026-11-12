import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";
import { LiveAuthorizationFence } from "./liveAuthorizationFence.ts";
import { livePollingHasActiveCapability } from "./livePollPolicy.ts";
import { liveSnapshotWake, PATIENT_AUTOPILOT_WAKE_EVENT } from "./autopilotWake.ts";
import { parseSyncPayload } from "./messages.ts";

// Run the production hook AND its actual fetch/body parser. Only React, browser
// events, time, credentials and the network are replaced; no copy of the polling
// state machine lives in this harness.
const source = readFileSync(new URL("./useLiveCursor.ts", import.meta.url), "utf8");
const sf = ts.createSourceFile("useLiveCursor.ts", source, ts.ScriptTarget.Latest, true);
const executable = sf.statements.filter((node) => !ts.isImportDeclaration(node))
  .map((node) => node.getText(sf).replace(/^export /, "")).join("\n");
const compiled = ts.transpileModule(`${executable}\nmodule.exports = useLiveCursor;`, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const CAPABILITY_EVENT = "test:capability-updated";
const PAIR_EVENT = "test:pair-required";
const settle = () => new Promise((resolve) => setImmediate(resolve));

type State = { connection: string; session?: { sessionId: string }; cursor?: { itemIdx: number } };
type Timer = { at: number; callback: () => void; interval?: number };
type RequestCall = {
  started: number;
  signal: AbortSignal;
  headers: Record<string, string>;
  ignoreAbort: boolean;
  respond(data: unknown, status?: number): void;
  headersThenBody(status?: number): (data: unknown) => void;
  fail(): void;
};

function snapshot(seq = 1, sessionId = "S1") {
  return {
    seq,
    session: { sessionId, weekNo: 2, eventLine: "正式训练", mode: "task", itemBankVersionId: "test-bank", wseq: seq },
    cursor: { sessionId, screen: "present", itemIdx: seq - 1, turnIdx: 0, responseRole: "命名", cueLevel: 0, recording: "idle", wseq: seq },
    rapportStep: null,
    autopilotWake: { sessionId, stateRevision: seq },
  };
}

function harness({ online = true, paired = true } = {}) {
  let now = 0;
  let nextTimer = 0;
  let current: State = { connection: "connecting" };
  let capability: string | null = paired ? "cap-one" : null;
  let cleanup: (() => void) | undefined;
  let authorizationChecks = 0;
  const timers = new Map<number, Timer>();
  const listeners = new Map<string, Set<() => void>>();
  const requests: RequestCall[] = [];
  const published: State[] = [];
  const events: string[] = [];
  const navigator = { onLine: online };
  const schedule = (callback: () => void, delay: number, interval?: number) => {
    const id = ++nextTimer;
    timers.set(id, { at: now + delay, callback, interval });
    return id;
  };
  const window = {
    setTimeout: (callback: () => void, delay: number) => schedule(callback, delay),
    setInterval: (callback: () => void, delay: number) => schedule(callback, delay, delay),
    addEventListener(name: string, callback: () => void) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name)!.add(callback);
    },
    removeEventListener(name: string, callback: () => void) { listeners.get(name)?.delete(callback); },
    dispatchEvent(event: { type: string }) {
      events.push(event.type);
      listeners.get(event.type)?.forEach((callback) => callback());
    },
  };
  const globals = {
    ApiError, LiveAuthorizationFence, livePollingHasActiveCapability, liveSnapshotWake, parseSyncPayload,
    PATIENT_AUTOPILOT_WAKE_EVENT,
    DEVICE_CAPABILITY_UPDATED_EVENT: CAPABILITY_EVENT,
    DEVICE_PAIR_REQUIRED_EVENT: PAIR_EVENT,
    window, navigator, AbortController, Event, CustomEvent, queueMicrotask,
    setInterval: window.setInterval,
    clearInterval: (id: number) => timers.delete(id),
    clearTimeout: (id: number) => timers.delete(id),
    getDeviceCapability: () => capability ? { capability } : null,
    selectDeviceCredential: () => ({
      source: capability ? "active" : "none",
      record: capability ? { capability } : null,
      headers: capability ? { "X-Device-Capability": capability } : {},
    }),
    handleDeviceAuthorizationFailure: (status: number) => {
      authorizationChecks += 1;
      return status === 401;
    },
    bus: { subscribe: () => () => {}, reset: () => {}, replaceSnapshot: () => {} },
    useRef: (value: unknown) => ({ current: value }),
    useState: (value: State) => {
      current = value;
      return [value, (next: State) => { current = next; published.push(next); }];
    },
    useEffect: (effect: () => () => void) => { cleanup = effect(); },
    fetch: (url: string, init: { signal: AbortSignal; headers: Record<string, string> }) => {
      assert.equal(url, "/live/state");
      return new Promise((resolve, reject) => {
        const call: RequestCall = {
          started: now, signal: init.signal, headers: init.headers, ignoreAbort: false,
          respond(data, status = 200) {
            resolve({ ok: status < 400, status, text: async () => JSON.stringify(data) });
          },
          headersThenBody(status = 200) {
            let finish!: (value: string) => void;
            const body = new Promise<string>((bodyResolve) => { finish = bodyResolve; });
            resolve({ ok: status < 400, status, text: () => body });
            return (data) => finish(JSON.stringify(data));
          },
          fail() { reject(new Error("network failure")); },
        };
        requests.push(call);
        init.signal.addEventListener("abort", () => {
          if (!call.ignoreAbort) reject(new Error("aborted"));
        }, { once: true });
      });
    },
  };
  const context = vm.createContext({ ...globals, module: { exports: undefined } });
  vm.runInContext(compiled, context);
  (context.module as unknown as { exports: () => void }).exports();
  return {
    requests, published, events,
    get state() { return current; },
    get timerCount() { return timers.size; },
    get authorizationChecks() { return authorizationChecks; },
    async advance(milliseconds: number) {
      const end = now + milliseconds;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        if (timer.interval) timer.at += timer.interval;
        else timers.delete(id);
        timer.callback();
        await settle();
      }
      now = end;
      await settle();
    },
    offline() { navigator.onLine = false; window.dispatchEvent(new Event("offline")); },
    online() { navigator.onLine = true; window.dispatchEvent(new Event("online")); },
    rotate(next: string | null) { capability = next; window.dispatchEvent(new Event(CAPABILITY_EVENT)); },
    unmount() { cleanup?.(); },
  };
}

test("4.5 秒有效应答：4 秒仍停媒体，保留同一请求直到恢复，不陷入每 4 秒重试", async () => {
  const h = harness();
  await h.advance(4000);
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.requests.length, 1, "slow requests remain single flight");
  assert.equal(h.requests[0].signal.aborted, false, "the safety stop must not cancel a recoverable response");
  await h.advance(500);
  h.requests[0].respond(snapshot());
  await settle();
  assert.equal(h.state.connection, "connected");
  assert.equal(h.state.session?.sessionId, "S1");
  assert.equal(h.state.cursor?.itemIdx, 0);
  assert.equal(h.events.filter((event) => event === PATIENT_AUTOPILOT_WAKE_EVENT).length, 1);
  h.unmount();
});

test("已连接的场次仍在下一拍超过 4 秒时停媒体，8 秒应答可恢复原场次", async () => {
  const h = harness();
  h.requests[0].respond(snapshot());
  await settle();
  await h.advance(800);
  await h.advance(3999);
  assert.equal(h.state.connection, "connected");
  await h.advance(1);
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.state.session?.sessionId, "S1");
  assert.equal(h.requests.length, 2);
  await h.advance(4000);
  h.requests[1].respond(snapshot(2));
  await settle();
  assert.equal(h.state.connection, "connected");
  assert.equal(h.state.cursor?.itemIdx, 1);
  h.unmount();
});

test("响应头已到但正文慢：同样在 4 秒停媒体，正文完整后才恢复", async () => {
  const h = harness();
  const body = h.requests[0].headersThenBody();
  await settle();
  await h.advance(4000);
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.requests[0].signal.aborted, false);
  await h.advance(500);
  body(snapshot());
  await settle();
  assert.equal(h.state.connection, "connected");
  h.unmount();
});

test("真挂起在 12 秒取消并有界重试，轮询间隔不会叠加并发请求", async () => {
  const h = harness();
  await h.advance(11999);
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].signal.aborted, false);
  await h.advance(801);
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.requests.length, 2);
  assert.ok(h.requests[1].started >= 12000 && h.requests[1].started <= 12800);
  h.requests[1].respond(snapshot());
  await settle();
  assert.equal(h.state.connection, "connected");
  h.unmount();
});

test("硬超时后的排队成功回调也不能恢复连接或唤醒自动带练", async () => {
  const h = harness();
  const body = h.requests[0].headersThenBody();
  await settle();
  await h.advance(12000);
  assert.equal(h.requests[0].signal.aborted, true);
  body(snapshot());
  await settle();
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.state.session, undefined);
  assert.equal(h.events.includes(PATIENT_AUTOPILOT_WAKE_EVENT), false);
  await h.advance(800);
  assert.equal(h.requests.length, 2);
  h.unmount();
});

test("真实 offline 立即停媒体并停止请求，迟到正文不能恢复；online 后才重新证明", async () => {
  const h = harness();
  h.requests[0].respond(snapshot());
  await settle();
  await h.advance(800);
  const body = h.requests[1].headersThenBody();
  await settle();
  h.offline();
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.requests[1].signal.aborted, true);
  const wakeCount = h.events.filter((event) => event === PATIENT_AUTOPILOT_WAKE_EVENT).length;
  body(snapshot(2));
  await settle();
  await h.advance(20000);
  assert.equal(h.requests.length, 2);
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.state.cursor?.itemIdx, 0);
  assert.equal(h.events.filter((event) => event === PATIENT_AUTOPILOT_WAKE_EVENT).length, wakeCount);
  h.online();
  assert.equal(h.requests.length, 3);
  h.requests[2].respond(snapshot(3));
  await settle();
  assert.equal(h.state.connection, "connected");
  assert.equal(h.state.cursor?.itemIdx, 2);
  h.unmount();
});

test("offline 后很快 online，已取消的旧响应仍不能作为恢复证明", async () => {
  const h = harness();
  const body = h.requests[0].headersThenBody();
  await settle();
  h.offline();
  h.online();
  body(snapshot());
  await settle();
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.state.session, undefined);
  assert.equal(h.events.includes(PATIENT_AUTOPILOT_WAKE_EVENT), false);
  await h.advance(800);
  h.requests[1].respond(snapshot());
  await settle();
  assert.equal(h.state.connection, "connected");
  h.unmount();
});

test("初始离线不发请求；未配对也不发无凭据请求", async () => {
  const offline = harness({ online: false });
  await offline.advance(15000);
  assert.equal(offline.requests.length, 0);
  assert.equal(offline.state.connection, "reconnecting");
  offline.online();
  assert.equal(offline.requests.length, 1);
  offline.unmount();

  const unpaired = harness({ paired: false });
  await unpaired.advance(15000);
  assert.equal(unpaired.requests.length, 0);
  unpaired.rotate("first-cap");
  assert.equal(unpaired.requests.length, 1);
  assert.equal(unpaired.requests[0].headers["X-Device-Capability"], "first-cap");
  unpaired.unmount();
});

test("凭据轮换立即清空旧场次，只接受新凭据的快照；旧定时器不能压回重连", async () => {
  const h = harness();
  h.requests[0].respond(snapshot());
  await settle();
  await h.advance(800);
  const body = h.requests[1].headersThenBody();
  await settle();
  h.rotate("cap-two");
  assert.equal(h.state.session, undefined);
  body(snapshot(2));
  await settle();
  assert.equal(h.state.connection, "reconnecting");
  assert.equal(h.state.session, undefined);
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].headers["X-Device-Capability"], "cap-two");
  h.requests[2].respond(snapshot(1, "S2"));
  await settle();
  await h.advance(4000);
  assert.equal(h.state.connection, "connected", "the cancelled old poll's stale timer is cleared");
  assert.equal(h.state.session?.sessionId, "S2");
  h.unmount();
});

test("移除凭据或稳定鉴权失败后停止轮询，迟到响应不能还原旧场次", async () => {
  const removed = harness();
  const body = removed.requests[0].headersThenBody();
  await settle();
  removed.rotate(null);
  body(snapshot());
  await settle();
  await removed.advance(20000);
  assert.equal(removed.state.session, undefined);
  assert.equal(removed.requests.length, 1);
  assert.equal(removed.events.includes(PAIR_EVENT), true);
  removed.unmount();

  const rejected = harness();
  rejected.requests[0].respond({ code: "device_capability_expired" }, 401);
  await settle();
  await rejected.advance(20000);
  assert.equal(rejected.state.connection, "reconnecting");
  assert.equal(rejected.state.session, undefined);
  assert.equal(rejected.requests.length, 1);
  rejected.rotate("replacement-cap");
  assert.equal(rejected.requests.length, 2);
  rejected.unmount();
});

test("卸载取消传输并清掉全部定时器，迟到正文不发布状态也不发唤醒", async () => {
  const h = harness();
  const body = h.requests[0].headersThenBody();
  await settle();
  const published = h.published.length;
  h.unmount();
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.timerCount, 0);
  body(snapshot());
  await settle();
  await h.advance(20000);
  assert.equal(h.published.length, published);
  assert.equal(h.events.includes(PATIENT_AUTOPILOT_WAKE_EVENT), false);
  assert.equal(h.requests.length, 1);
});

test("未配对或 401 后，离线期间装入新凭据仍欠一次上线验权", async () => {
  for (const rejectedFirst of [false, true]) {
    const h = harness({ paired: rejectedFirst });
    if (rejectedFirst) {
      h.requests[0].respond({ code: "device_capability_expired" }, 401);
      await settle();
    }
    const before = h.requests.length;
    h.offline();
    h.rotate("offline-replacement");
    await h.advance(15000);
    assert.equal(h.requests.length, before);
    h.online();
    assert.equal(h.requests.length, before + 1);
    assert.equal(h.requests.at(-1)?.headers["X-Device-Capability"], "offline-replacement");
    h.requests.at(-1)!.respond(snapshot(1, "S2"));
    await settle();
    assert.equal(h.state.connection, "connected");
    assert.equal(h.state.session?.sessionId, "S2");
    h.unmount();
  }
});

test("离线换凭据后旧请求 finally 收尾也不能消费尚未发出的验权", async () => {
  const h = harness();
  const body = h.requests[0].headersThenBody();
  await settle();
  h.offline();
  h.rotate("offline-replacement");
  body(snapshot());
  await settle();
  await h.advance(15000);
  assert.equal(h.requests.length, 1);
  h.online();
  assert.equal(h.requests.length, 2);
  assert.equal(h.requests[1].headers["X-Device-Capability"], "offline-replacement");
  h.requests[1].respond(snapshot(1, "S2"));
  await settle();
  assert.equal(h.state.session?.sessionId, "S2");
  h.unmount();
});

test("普通瞬时网络失败仍保留原 4.5 秒缓冲", async () => {
  const h = harness();
  h.requests[0].fail();
  await settle();
  await h.advance(4499);
  assert.equal(h.state.connection, "connecting");
  await h.advance(1);
  assert.equal(h.state.connection, "reconnecting");
  h.unmount();
});

test("offline、轮换或卸载后的迟到 401 正文不能触碰凭据", async () => {
  for (const invalidation of ["offline", "rotate", "unmount"] as const) {
    const h = harness();
    const body = h.requests[0].headersThenBody(401);
    await settle();
    if (invalidation === "rotate") h.rotate("replacement-cap");
    else h[invalidation]();
    body({ code: "device_capability_expired" });
    await settle();
    assert.equal(h.authorizationChecks, 0, invalidation);
    assert.equal(h.state.session, undefined);
    h.unmount();
  }
});
