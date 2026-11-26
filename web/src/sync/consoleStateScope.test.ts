import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";
import { deliverAuthoritativeAudioSaved } from "./audioSavedAuthority.ts";
import { parseSyncPayload } from "./messages.ts";
import { readAuthoritativePatientRec } from "./patientRecAuthority.ts";
import { presenceViewFrom } from "./presenceView.ts";

// 两床并行时控制台的三个 console-state 轮询器都只能读本场所在的床旁槽。
// 执行生产源码本身(api.ts 在模块作用域读 localStorage,Node 里不能直接 import);
// React 只换成最小的 useState/useRef/useEffect/useMemo 帧,网络换成可控替身。

function productionNode(file: string, pick: (node: ts.Node, sf: ts.SourceFile) => string | null): string {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
  let found: string | null = null;
  const visit = (node: ts.Node) => {
    found ??= pick(node, sf);
    if (!found) ts.forEachChild(node, visit);
  };
  visit(sf);
  assert.ok(found, `production node must exist in ${file}`);
  return found;
}

const hookSource = (file: string, name: string) => productionNode(file, (node, sf) =>
  ts.isFunctionDeclaration(node) && node.name?.text === name
    ? node.getText(sf).replace(/^export /, "") : null);

const apiMethodSource = (name: string) => productionNode("../api.ts", (node, sf) =>
  ts.isPropertyAssignment(node) && node.name.getText(sf) === name
    ? node.initializer.getText(sf) : null);

function compile<T>(source: string, globals: Record<string, unknown>): T {
  const context = vm.createContext({ ...globals, module: { exports: {} } });
  vm.runInContext(ts.transpileModule(`module.exports = ${source}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText, context);
  return (context.module as { exports: T }).exports;
}

function reactFrame() {
  type Slot = { value?: unknown; current?: unknown; deps?: unknown[]; cleanup?: (() => void) | void };
  const slots: Slot[] = [];
  let cursor = 0;
  let pending: (() => void)[] = [];
  const changed = (deps: unknown[] | undefined, previous?: Slot) =>
    !previous || !deps || deps.some((value, i) => value !== previous.deps?.[i]);
  const react = {
    useState(initial: unknown) {
      const slot = (slots[cursor++] ??= { value: initial });
      return [slot.value, (next: unknown) => {
        slot.value = typeof next === "function" ? (next as (v: unknown) => unknown)(slot.value) : next;
      }];
    },
    useRef(initial: unknown) {
      return (slots[cursor++] ??= { current: initial });
    },
    useEffect(effect: () => (() => void) | void, deps?: unknown[]) {
      const index = cursor++;
      const previous = slots[index];
      if (!changed(deps, previous)) return;
      const next: Slot = { deps, cleanup: previous?.cleanup };
      slots[index] = next;
      pending.push(() => {
        if (typeof next.cleanup === "function") next.cleanup();
        next.cleanup = effect();
      });
    },
    useMemo(factory: () => unknown, deps?: unknown[]) {
      const index = cursor++;
      const previous = slots[index];
      if (!changed(deps, previous)) return previous!.value;
      slots[index] = { deps, value: factory() };
      return slots[index].value;
    },
  };
  return {
    react,
    render<T>(hook: (...args: unknown[]) => T, ...args: unknown[]): T {
      cursor = 0;
      const out = hook(...args);
      const work = pending;
      pending = [];
      for (const run of work) run();
      return out;
    },
  };
}

type Deferred = { resolve(value: unknown): void; reject(error: unknown): void };

function consoleStateStub() {
  const calls: unknown[][] = [];
  const responses: Deferred[] = [];
  return {
    calls,
    responses,
    getConsoleState: (...args: unknown[]) => {
      calls.push(args);
      return new Promise((resolve, reject) => responses.push({ resolve, reject }));
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const timers = { setInterval: () => 0, clearInterval: () => {} };
const quietBus = { subscribe: () => () => {} };

const sessionSlot = (sessionId: string) => ({
  sessionId, weekNo: 2, eventLine: "正式训练", mode: "task", itemBankVersionId: "v1", wseq: 1,
});

test("getConsoleState names the session whose bedside slot the console reads", () => {
  const requests: unknown[][] = [];
  const getConsoleState = compile<(...args: unknown[]) => unknown>(apiMethodSource("getConsoleState"), {
    URLSearchParams,
    req: (...args: unknown[]) => { requests.push(args); return "sent"; },
  });
  assert.equal(getConsoleState("S-1 床/二"), "sent");
  assert.deepEqual(requests, [[
    "GET", "/live/console-state?session_id=S-1+%E5%BA%8A%2F%E4%BA%8C", undefined, 5_000,
  ]]);
  getConsoleState("S-2", 1_234);
  assert.deepEqual(requests[1], ["GET", "/live/console-state?session_id=S-2", undefined, 1_234]);
});

function presenceHarness() {
  const frame = reactFrame();
  const net = consoleStateStub();
  const hook = compile<(...args: unknown[]) => { state: string }>(
    hookSource("./usePatientPresence.ts", "usePatientPresence"),
    {
      ...frame.react, ...timers, window: timers, ApiError, presenceViewFrom,
      api: { getConsoleState: net.getConsoleState },
    },
  );
  return { net, view: (sessionId: string) => frame.render(hook, sessionId) };
}

test("presence polling reads its own session's slot", () => {
  const h = presenceHarness();
  h.view("S-OWN");
  assert.deepEqual(h.net.calls, [["S-OWN"]]);
});

test("presence: only a route-missing 404 means the backend lacks presence support", async () => {
  const old = presenceHarness();
  old.view("S-OWN");
  old.net.responses[0].reject(new ApiError(404, "Not Found"));
  await flush();
  assert.equal(old.view("S-OWN").state, "unsupported");

  // 「场次不存在」的 404:本场此刻不在任何床旁槽,受试者端不可能在本场在线。
  // 不能当成旧后端把录音入口的在场门禁关掉,必须按未连接处理。
  const vacated = presenceHarness();
  vacated.view("S-OWN");
  vacated.net.responses[0].reject(new ApiError(404, "场次不存在"));
  await flush();
  assert.equal(vacated.view("S-OWN").state, "unseen");
});

const ownRec = { active: true, turnKey: "SE_锚#1", sessionId: "S-OWN" };

test("patientRec polling reads its own session's slot; another session's projection clears the last rec", async () => {
  const frame = reactFrame();
  const net = consoleStateStub();
  const polls: (() => void)[] = [];
  const hook = compile<(...args: unknown[]) => { sessionId: string } | null>(
    hookSource("./useCursorWriter.ts", "usePatientRec"),
    {
      ...frame.react, setInterval: (poll: () => void) => { polls.push(poll); return 0; },
      clearInterval: () => {}, bus: quietBus, LIVE_POLL_MS: 1000, readAuthoritativePatientRec,
      api: { getConsoleState: net.getConsoleState },
    },
  );
  frame.render(hook, "S-OWN");
  assert.deepEqual(net.calls, [["S-OWN"]]);
  net.responses[0].resolve({ patientRec: ownRec });
  await flush();
  assert.equal(frame.render(hook, "S-OWN")?.sessionId, "S-OWN");

  // 一份格式不认识的本场投影照旧不动上一条 rec(今天的行为)。
  polls[0]();
  net.responses[1].resolve({ patientRec: { ...ownRec, active: "maybe" } });
  await flush();
  assert.equal(frame.render(hook, "S-OWN")?.sessionId, "S-OWN");

  // 这一槽的 rec 已属于另一场:本场上一条开麦 rec 不能一直挂在控制台上。
  polls[0]();
  assert.deepEqual(net.calls[2], ["S-OWN"]);
  net.responses[2].resolve({ patientRec: { ...ownRec, sessionId: "S-OTHER" } });
  await flush();
  assert.equal(frame.render(hook, "S-OWN"), null);
});

function audioSavedHarness() {
  const frame = reactFrame();
  const net = consoleStateStub();
  const ledgerReads: string[] = [];
  const delivered: unknown[] = [];
  const hook = compile<(...args: unknown[]) => void>(
    hookSource("./useCursorWriter.ts", "useAudioSaved"),
    {
      ...frame.react, ...timers, bus: quietBus, LIVE_POLL_MS: 1000, ApiError,
      parseSyncPayload, deliverAuthoritativeAudioSaved,
      api: {
        getConsoleState: net.getConsoleState,
        audioReceipts: async (sid: string, afterSeq: number) => {
          ledgerReads.push(sid);
          return { session_id: sid, after_seq: afterSeq, last_seq: afterSeq, receipts: [] };
        },
      },
    },
  );
  return {
    net, ledgerReads, delivered,
    mount: (sessionId: string) => frame.render(hook, (m: unknown) => delivered.push(m), sessionId),
  };
}

test("audioSaved polling reads its own session's slot and its receipt ledger", async () => {
  const h = audioSavedHarness();
  h.mount("S-OWN");
  assert.deepEqual(h.net.calls, [["S-OWN"]]);
  h.net.responses[0].resolve({ session: sessionSlot("S-OWN"), audioSaved: null });
  await flush();
  assert.deepEqual(h.ledgerReads, ["S-OWN"]);

  // 同一屏换到下一场时轮询器跟着换场,不能继续读上一场的槽。
  h.mount("S-NEXT");
  assert.deepEqual(h.net.calls[1], ["S-NEXT"]);
});

test("audioSaved never pulls another session's receipt ledger when the slot belongs to it", async () => {
  const h = audioSavedHarness();
  h.mount("S-OWN");
  h.net.responses[0].resolve({ session: sessionSlot("S-OTHER"), audioSaved: null });
  await flush();
  assert.deepEqual(h.ledgerReads, []);
  assert.deepEqual(h.delivered, []);
});
