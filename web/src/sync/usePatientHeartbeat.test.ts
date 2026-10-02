import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { ApiError } from "../apiResponse.ts";

// 执行生产 hook 源码本身(api.ts 在模块作用域读 localStorage,Node 里不能直接 import);
// React 只换成最小的 useRef/useEffect 帧,网络换成可控的 deferred。
const BUILD = "4205954952" + "1".repeat(67);
const source = readFileSync(new URL("./usePatientHeartbeat.ts", import.meta.url), "utf8");

function hookSource(): string {
  const sf = ts.createSourceFile("usePatientHeartbeat.ts", source, ts.ScriptTarget.Latest, true);
  let found: string | null = null;
  const visit = (node: ts.Node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === "usePatientHeartbeat") {
      found = node.getText(sf).replace(/^export /, "");
    }
    if (!found) ts.forEachChild(node, visit);
  };
  visit(sf);
  assert.ok(found, "production hook must exist");
  return found;
}

type Deferred = { resolve(value: unknown): void; reject(error: unknown): void };

function harness() {
  const slots: { current?: unknown; deps?: unknown[]; cleanup?: (() => void) | void }[] = [];
  let cursor = 0;
  let pending: (() => void)[] = [];
  const calls: Record<string, unknown>[] = [];
  const responses: Deferred[] = [];
  const intervals: (() => void)[] = [];
  const globals = {
    HEARTBEAT_MS: 5000,
    ApiError,
    api: {
      patientHeartbeat: (body: Record<string, unknown>) => {
        calls.push({ ...body });
        return new Promise((resolve, reject) => responses.push({ resolve, reject }));
      },
    },
    window: {
      setInterval: (callback: () => void) => intervals.push(callback),
    },
    clearInterval: () => {},
    useRef(value: unknown) {
      const index = cursor++;
      slots[index] ??= { current: value };
      return slots[index];
    },
    useEffect(effect: () => (() => void) | void, deps?: unknown[]) {
      const index = cursor++;
      const previous = slots[index];
      if (!previous || !deps || deps.some((value, i) => value !== previous.deps?.[i])) {
        const next = { deps, cleanup: previous?.cleanup };
        slots[index] = next;
        pending.push(() => {
          if (typeof next.cleanup === "function") next.cleanup();
          next.cleanup = effect();
        });
      }
    },
  };
  const context = vm.createContext({ ...globals, module: { exports: {} } });
  const compiled = ts.transpileModule(`module.exports = ${hookSource()}`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  vm.runInContext(compiled, context);
  const hook = (context.module as { exports: (...args: unknown[]) => void }).exports;
  return {
    calls,
    responses,
    render(...args: unknown[]) {
      cursor = 0;
      hook(...args);
      const work = pending;
      pending = [];
      work.forEach((run) => run());
    },
    tickInterval() { intervals.at(-1)?.(); },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("回滚到不认识 page_build_id 的旧后端:422 后本页不再带它,并立刻补发一拍", async () => {
  const h = harness();
  h.render("S1", "waiting", undefined, BUILD);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].page_build_id, BUILD);

  h.responses[0].reject(new ApiError(422, "extra_forbidden"));
  await settle();
  // 在场不断档:不等下一个 5 s 节拍。
  assert.equal(h.calls.length, 2);
  assert.equal("page_build_id" in h.calls[1], false);
  h.responses[1].resolve({});
  await settle();

  h.tickInterval();
  assert.equal(h.calls.length, 3);
  assert.equal("page_build_id" in h.calls[2], false);
  h.render("S1", "present", 7, BUILD);
  await settle();
  assert.ok(h.calls.slice(3).every((body) => !("page_build_id" in body)));
});

test("其它失败不闩住构建号;没带构建号的 422 也不会无限补发", async () => {
  const h = harness();
  h.render("S1", "waiting", undefined, BUILD);
  h.responses[0].reject(new ApiError(500, "server error"));
  await settle();
  assert.equal(h.calls.length, 1);
  h.tickInterval();
  assert.equal(h.calls[1].page_build_id, BUILD);

  const legacy = harness();
  legacy.render("S1", "waiting", undefined, undefined);
  assert.equal("page_build_id" in legacy.calls[0], false);
  legacy.responses[0].reject(new ApiError(422, "invalid screen"));
  await settle();
  assert.equal(legacy.calls.length, 1);
});

test("老人端心跳带的是本页编译时的构建号", () => {
  const shell = readFileSync(new URL("../patient/PatientShell.tsx", import.meta.url), "utf8");
  assert.match(shell,
    /usePatientHeartbeat\(session\?\.sessionId, currentScreen, commandSeq \?\? session\?\.wseq, __BUILD_ID__\);/);
});
