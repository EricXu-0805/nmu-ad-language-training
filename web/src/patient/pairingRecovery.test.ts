import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { ApiError, apiNetworkError, decodeJsonApiResponse } from "../apiResponse.ts";
import {
  createDeviceCapabilityStore, parseDevicePairResponse,
  DEVICE_CAPABILITY_STORAGE_KEY, DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY,
  type DeviceStorageLike,
} from "../security/deviceCapability.ts";
import { createPatientBindingStore, parsePatientBindingRecord } from "../security/patientBinding.ts";
import { attachHintFor, classifyAttachOutcome, shouldAttemptAttach } from "./bindingAttachPolicy.ts";

// P0-6 回归:「暂不配对」→「点一下,开始」之后不许死屏。
// 问候页必须有常驻找回入口重新弹配对框,且不依赖整页刷新。
const shell = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
const pinPrompt = readFileSync(
  new URL("../components/PinPrompt.tsx", import.meta.url), "utf8");

test("未配对问候页有常驻「连接这台平板」入口,点击走手动配对事件", () => {
  assert.match(shell, /\{!devicePaired && \(/);
  assert.match(shell, /className="patient-pair-entry"/);
  assert.match(shell, /new Event\(PIN_PROMPT_MANUAL_OPEN_EVENT\)/);
  assert.match(shell, /连接这台平板/);
  // 入口与老人主线区分:说明这是给工作人员的。
  assert.match(shell, /请工作人员点这里完成配对/);
});

test("配对状态来自绑定或设备能力任一,并跟随 capabilityEpoch 重新计算", () => {
  assert.match(shell,
    /const devicePaired = patientBindingActive \|\| getDeviceCapability\(\) !== null;/);
  // capabilityEpoch 变化触发重渲染,使 getDeviceCapability() 的读取保持新鲜。
  assert.match(shell, /void capabilityEpoch;/);
});

test("PinPrompt 的手动打开路径绕过 getPatientBinding 守卫(人明确要求配对)", () => {
  assert.match(pinPrompt, /export const PIN_PROMPT_MANUAL_OPEN_EVENT/);
  
  assert.match(pinPrompt,
    /window\.addEventListener\(PIN_PROMPT_MANUAL_OPEN_EVENT, openNow\)/);
  // 自动事件仍有绑定守卫;手动事件直接 openNow,不经过 getPatientBinding。
  const showBody = pinPrompt.slice(
    pinPrompt.indexOf("const show = () => {"),
    pinPrompt.indexOf("window.addEventListener(DEVICE_PAIR_REQUIRED_EVENT"));
  assert.match(showBody, /getPatientBinding\(\)/);
  const openNowBody = pinPrompt.slice(
    pinPrompt.indexOf("const openNow = () => {"),
    pinPrompt.indexOf("const show = () => {"));
  assert.doesNotMatch(openNowBody, /getPatientBinding/);
});

// Execute the real API boundary, including both actual credential stores. Only
// fetch/timers/storage are synthetic; delayed responses exercise commit order.
class MemoryStorage implements DeviceStorageLike {
  values = new Map<string, string>();
  failWriteKey: string | null = null;
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) {
    if (this.failWriteKey === key) {
      this.failWriteKey = null;
      throw new Error("synthetic storage failure");
    }
    this.values.set(key, value);
  }
  removeItem(key: string) { this.values.delete(key); }
}
const DEVICE_ID = "synthetic-device-0001";
const bindingRecord = (name: string) => ({ binding: `pb1.${name}.SIG`, deviceId: DEVICE_ID });
const capabilityRecord = (name: string) => ({
  capability: name.repeat(40), sessionId: `SIM-${name}`,
  expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
});
function pairingHarness() {
  const local = new MemoryStorage(), session = new MemoryStorage();
  const capabilities = createDeviceCapabilityStore(local, session, Date.now, () => DEVICE_ID);
  const bindings = createPatientBindingStore(local);
  const requests: { url: string; resolve: (response: Response) => void }[] = [];
  const events: string[] = [];
  const timers = new Map<number, () => void>();
  let nextTimer = 0;
  const source = readFileSync(new URL("../api.ts", import.meta.url), "utf8");
  const start = source.indexOf("const deviceStore =");
  const end = source.indexOf("const DEFAULT_REQUEST_TIMEOUT_MS = 12_000;")
    + "const DEFAULT_REQUEST_TIMEOUT_MS = 12_000;".length;
  assert(start >= 0 && end > start, "production pairing boundary must be present");
  const compiled = ts.transpileModule(source.slice(start, end) + `
    globalThis.testApi = {pairDevice, pairDeviceWithCode, attachPatientDevice,
      clearPatientBinding, clearDeviceCapability};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
  }).outputText;
  const context = vm.createContext({
    exports: {}, ApiError, apiNetworkError, decodeJsonApiResponse,
    createDeviceCapabilityStore: () => capabilities,
    createPatientBindingStore: () => bindings,
    parseDevicePairResponse, parsePatientBindingRecord, attachHintFor, classifyAttachOutcome,
    localStorage: local, sessionStorage: session, AbortController, Event, queueMicrotask,
    window: {
      setTimeout(fn: () => void) { timers.set(++nextTimer, fn); return nextTimer; },
      clearTimeout(id: number) { timers.delete(id); },
      dispatchEvent(event: Event) { events.push(event.type); },
    },
    fetch: (url: string) => new Promise<Response>(resolve => requests.push({ url, resolve })),
  });
  vm.runInContext(compiled, context);
  const api = context.testApi as {
    pairDevice: (pin: string) => Promise<unknown>;
    pairDeviceWithCode: (pin: string) => Promise<{ kind: string }>;
    attachPatientDevice: () => Promise<{ disposition: string; hint: string | null }>;
    clearPatientBinding: () => void;
    clearDeviceCapability: () => void;
  };
  return {
    api, bindings, capabilities, requests, events, local, session,
    respond(index: number, status: number, value: unknown) {
      requests[index].resolve(new Response(JSON.stringify(value), { status }));
    },
    expire() { for (const callback of [...timers.values()]) callback(); },
  };
}

for (const status of [200, 401]) for (const afterPair of [false, true]) {
  test(`old attach ${status} cannot mutate a newer manual pair ${afterPair ? "after completion" : "while pending"}`, async () => {
    const h = pairingHarness();
    h.bindings.save(bindingRecord("OLD"));
    const attach = h.api.attachPatientDevice();
    const pair = h.api.pairDeviceWithCode("123456");
    const newCapability = capabilityRecord("B");
    if (afterPair) {
      h.respond(1, 200, { ...newCapability, binding: bindingRecord("NEW").binding });
      await pair;
    }
    h.respond(0, status, status === 200 ? capabilityRecord("A")
      : { detail: { code: "device_binding_revoked" } });
    const result = await attach;
    assert.equal(result.disposition, "quiet_retry");
    assert.equal(h.bindings.get()?.binding, bindingRecord(afterPair ? "NEW" : "OLD").binding);
    assert.equal(h.capabilities.get()?.sessionId ?? null, afterPair ? "SIM-B" : null);
    if (!afterPair) {
      h.respond(1, 200, { ...newCapability, binding: bindingRecord("NEW").binding });
      await pair;
    }
    assert.equal(h.capabilities.get()?.capability, newCapability.capability);
    assert.deepEqual(h.bindings.get(), bindingRecord("NEW"));
    assert.equal(h.events.includes("nmu:device-pair-required"), false);
  });
}

test("manual pairing suppresses new automatic attaches and duplicate token rotations", async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const first = h.api.pairDeviceWithCode("123456");
  await assert.rejects(h.api.pairDeviceWithCode("123456"), (e: unknown) => e instanceof ApiError && e.status === 409);
  await assert.rejects(h.api.pairDevice("123456"), (e: unknown) => e instanceof ApiError && e.status === 409);
  assert.equal((await h.api.attachPatientDevice()).disposition, "quiet_retry");
  assert.equal(h.requests.length, 1);
  h.respond(0, 401, { detail: "incorrect code" });
  await assert.rejects(first);
  const attach = h.api.attachPatientDevice();
  assert.equal(h.requests.length, 2, "failed manual pairing must release automatic retry");
  h.respond(1, 200, capabilityRecord("A"));
  assert.equal((await attach).disposition, "attached");
});

for (const status of [200, 401]) test(`cross-tab binding replacement fences old attach ${status}`, async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const pending = h.api.attachPatientDevice();
  h.bindings.save(bindingRecord("NEW"));
  h.respond(0, status, status === 200 ? capabilityRecord("A")
    : { detail: { code: "device_binding_invalid" } });
  assert.equal((await pending).disposition, "quiet_retry");
  assert.deepEqual(h.bindings.get(), bindingRecord("NEW"));
  assert.equal(h.capabilities.get(), null);
  assert.deepEqual(h.events, []);
});

test("an independently replaced active capability cannot be overwritten by attach", async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const pending = h.api.attachPatientDevice();
  const replacement = capabilityRecord("B");
  h.capabilities.save(replacement);
  h.respond(0, 200, capabilityRecord("A"));
  assert.equal((await pending).disposition, "quiet_retry");
  assert.deepEqual(h.capabilities.get(), replacement);
});

for (const change of ["clear", "other-tab"] as const) test(`manual response cannot reverse a ${change} binding change`, async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const pending = h.api.pairDeviceWithCode("123456");
  if (change === "clear") h.api.clearPatientBinding();
  else h.bindings.save(bindingRecord("OTHER"));
  h.respond(0, 200, { ...capabilityRecord("B"), binding: bindingRecord("NEW").binding });
  await assert.rejects(pending, (e: unknown) => e instanceof ApiError && e.status === 409);
  assert.equal(h.capabilities.get(), null);
  assert.deepEqual(h.bindings.get(), change === "clear" ? null : bindingRecord("OTHER"));
});

test("binding-only switch retires the old active session but retains exact audio recovery", async () => {
  const h = pairingHarness(), old = capabilityRecord("A"), older = capabilityRecord("C");
  h.bindings.save(bindingRecord("OLD"));
  h.capabilities.save(old);
  h.capabilities.retainForRecovery(older);
  const pending = h.api.pairDeviceWithCode("123456");
  h.respond(0, 200, { binding: bindingRecord("NEW").binding });
  assert.equal((await pending).kind, "binding");
  assert.equal(h.capabilities.get(), null);
  assert.deepEqual(h.capabilities.getRecovery(old.sessionId), old);
  assert.deepEqual(h.capabilities.getRecovery(older.sessionId), older);
  assert.deepEqual(h.bindings.get(), bindingRecord("NEW"));
  assert.equal(shouldAttemptAttach(true, h.capabilities.get() !== null), true);
  assert.deepEqual(h.events, ["nmu:device-capability-updated", "nmu:patient-binding-updated"]);
});

for (const binding of [undefined, bindingRecord("NEW").binding]) test(`session pairing ${binding ? "with binding" : "with one-session code"} preserves foreign recovery`, async () => {
  const h = pairingHarness(), old = capabilityRecord("A"), next = capabilityRecord("B");
  h.bindings.save(bindingRecord("OLD"));
  h.capabilities.save(old);
  const pending = h.api.pairDeviceWithCode("123456");
  h.respond(0, 200, { ...next, ...(binding ? { binding } : {}) });
  await pending;
  assert.deepEqual(h.capabilities.get(), next);
  assert.deepEqual(h.capabilities.getRecovery(old.sessionId), old);
  assert.deepEqual(h.bindings.get(), binding ? bindingRecord("NEW") : null);
});

test("same-session pairing does not resurrect the token revoked by its server rotation", async () => {
  const h = pairingHarness(), old = capabilityRecord("A");
  h.capabilities.save(old);
  h.capabilities.retainForRecovery(old);
  const next = { ...old, capability: "Z".repeat(40) };
  const pending = h.api.pairDevice("123456");
  h.respond(0, 200, next);
  await pending;
  assert.deepEqual(h.capabilities.get(), next);
  assert.equal(h.capabilities.getRecovery(old.sessionId), null);
});

test("invalid capability cannot partially replace a valid patient binding", async () => {
  const h = pairingHarness(), old = capabilityRecord("A");
  h.bindings.save(bindingRecord("OLD"));
  h.capabilities.save(old);
  const pending = h.api.pairDeviceWithCode("123456");
  h.respond(0, 200, { binding: bindingRecord("NEW").binding, ...capabilityRecord("B"), capability: "bad" });
  await assert.rejects(pending);
  assert.deepEqual(h.bindings.get(), bindingRecord("OLD"));
  assert.deepEqual(h.capabilities.get(), old);
  assert.deepEqual(h.events, []);
});

for (const bindingOnly of [false, true]) test(`storage failure restores both old credentials during ${bindingOnly ? "binding-only" : "session"} switch`, async () => {
  const h = pairingHarness(), old = capabilityRecord("A");
  h.bindings.save(bindingRecord("OLD"));
  h.capabilities.save(old);
  h.session.failWriteKey = bindingOnly ? DEVICE_RECOVERY_CAPABILITIES_STORAGE_KEY : DEVICE_CAPABILITY_STORAGE_KEY;
  const pending = h.api.pairDeviceWithCode("123456");
  h.respond(0, 200, { binding: bindingRecord("NEW").binding, ...(bindingOnly ? {} : capabilityRecord("B")) });
  await assert.rejects(pending);
  assert.deepEqual(h.bindings.get(), bindingRecord("OLD"));
  assert.deepEqual(h.capabilities.get(), old);
  assert.deepEqual(h.events, []);
});

for (const manual of [false, true]) test(`an aborted ${manual ? "pair" : "attach"} cannot install its late response`, async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const pending = manual ? h.api.pairDeviceWithCode("123456") : h.api.attachPatientDevice();
  h.expire();
  h.respond(0, 200, capabilityRecord("A"));
  if (manual) await assert.rejects(pending, (e: unknown) => e instanceof ApiError && e.status === 408);
  else assert.equal((await pending as { disposition: string }).disposition, "quiet_retry");
  assert.equal(h.capabilities.get(), null);
  assert.deepEqual(h.bindings.get(), bindingRecord("OLD"));
});

test("a current revoked binding still clears and asks for manual pairing", async () => {
  const h = pairingHarness();
  h.bindings.save(bindingRecord("OLD"));
  const pending = h.api.attachPatientDevice();
  h.respond(0, 401, { detail: { code: "device_binding_revoked" } });
  assert.equal((await pending).disposition, "drop_binding");
  assert.equal(h.bindings.get(), null);
  assert.deepEqual(h.events, ["nmu:patient-binding-updated", "nmu:device-pair-required"]);
});
