import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import test from "node:test";
import {
  deployedBuildIdFrom,
  reloadStalePatientPage,
  shouldOfferStaleReload,
  webLockNames,
  type StaleReloadEntryInput,
  type StaleReloadPorts,
} from "./patientStaleReload.ts";

const PAGE = "2202995401" + "2".repeat(67);
const DEPLOYED = "4205954952" + "1".repeat(67);

const offer = (overrides: Partial<StaleReloadEntryInput> = {}) => shouldOfferStaleReload({
  enabled: true,
  screen: "greeting",
  pageBuildId: PAGE,
  deployedBuildId: DEPLOYED,
  connection: "connected",
  hasActiveCapability: true,
  ...overrides,
});

test("只在问候页与结束页、页面确实旧了才给工作人员刷新入口", () => {
  assert.equal(offer(), true);
  assert.equal(offer({ screen: "complete" }), true);
  // 训练中的任何画面(含暂停、关系建立、加载)都归为 session:永不出现。
  for (const connection of ["connected", "reconnecting", "connecting"] as const) {
    for (const hasActiveCapability of [true, false]) {
      assert.equal(offer({ screen: "session", connection, hasActiveCapability }), false);
    }
  }
  assert.equal(offer({ deployedBuildId: PAGE }), false);
  assert.equal(offer({ deployedBuildId: null }), false);
  // 叠层(与操作端同一页面)和非生产构建从不出现。
  assert.equal(offer({ enabled: false }), false);
});

test("问候页不等于空闲:手里有场次能力却没连上时不给入口", () => {
  // 设备鉴权失败或能力轮换时 useLiveCursor 在同一拍清空场次并进入 reconnecting。
  assert.equal(offer({ connection: "reconnecting", hasActiveCapability: true }), false);
  assert.equal(offer({ connection: "connecting", hasActiveCapability: true }), false);
  assert.equal(offer({ screen: "complete", connection: "reconnecting" }), false);
  // 已绑定、还没接上场次的平板本来就没有能力、也不轮询 live:照常给入口。
  assert.equal(offer({ connection: "reconnecting", hasActiveCapability: false }), true);
  assert.equal(offer({ connection: "connecting", hasActiveCapability: false }), true);
});

test("部署构建号只认纯数字:文件缺失时 SPA 回的 HTML、空串、全角数字都不算", () => {
  assert.equal(deployedBuildIdFrom(`${DEPLOYED}\n`), DEPLOYED);
  assert.equal(deployedBuildIdFrom("1".repeat(10)), "1".repeat(10));
  assert.equal(deployedBuildIdFrom("1".repeat(80)), "1".repeat(80));
  for (const text of [null, undefined, "", "<!doctype html><html></html>", "１２３４５６７８９０",
    "1".repeat(9), "1".repeat(81), "12345 67890"]) {
    assert.equal(deployedBuildIdFrom(text), null, String(text));
  }
});

test("Web Lock 快照:持有中与排队中的名字都算", () => {
  assert.deepEqual(webLockNames({
    held: [{ name: "nmu:audio-device-lease:v1" }, {}],
    pending: [{ name: "nmu-patient-pause-storage:v1" }],
  }), ["nmu:audio-device-lease:v1", "", "nmu-patient-pause-storage:v1"]);
  assert.deepEqual(webLockNames({}), []);
});

function ports(overrides: Partial<StaleReloadPorts> = {}) {
  const events: string[] = [];
  const value: StaleReloadPorts = {
    probeShell: async () => {
      events.push("probe");
      return { status: 200, contentType: "text/html; charset=utf-8" };
    },
    lockNames: async () => { events.push("locks"); return ["other-app-lock"]; },
    deviceTransportBusy: () => { events.push("transport"); return false; },
    stillOffered: () => { events.push("offered"); return true; },
    reload: () => { events.push("reload"); },
    ...overrides,
  };
  return { value, events };
}

test("安全检查全过才刷新:外壳可达、没有 nmu 锁、配对与跟场都不在途、画面没变", async () => {
  const clear = ports();
  assert.equal(await reloadStalePatientPage(clear.value), "reloaded");
  assert.deepEqual(clear.events, ["probe", "locks", "transport", "offered", "reload"]);

  for (const lock of ["nmu:audio-device-lease:v1", "nmu:patient-autopilot-owner:v1:S1",
    "nmu-patient-pause-storage:v1"]) {
    const busy = ports({ lockNames: async () => ["other-app-lock", lock] });
    assert.equal(await reloadStalePatientPage(busy.value), "busy", lock);
    assert.equal(busy.events.includes("reload"), false);
  }
  // 查不了锁(不支持或抛错)就当有人在忙。
  for (const lockNames of [async () => null, async () => { throw new Error("denied"); }]) {
    const unknown = ports({ lockNames });
    assert.equal(await reloadStalePatientPage(unknown.value), "busy");
    assert.equal(unknown.events.includes("reload"), false);
  }
  const transport = ports({ deviceTransportBusy: () => true });
  assert.equal(await reloadStalePatientPage(transport.value), "busy");
  assert.equal(transport.events.includes("reload"), false);
  const moved = ports({ stillOffered: () => false });
  assert.equal(await reloadStalePatientPage(moved.value), "busy");
  assert.equal(moved.events.includes("reload"), false);
});

test("外壳不可达就不刷新:避免整页落在反代错误页上回不来", async () => {
  for (const probeShell of [
    async () => ({ status: 502, contentType: "text/html" }),
    async () => ({ status: 200, contentType: "application/json" }),
    async () => ({ status: 200, contentType: null }),
    async () => { throw new TypeError("network"); },
  ]) {
    const unreachable = ports({ probeShell });
    assert.equal(await reloadStalePatientPage(unreachable.value), "unreachable");
    assert.deepEqual(unreachable.events, []);
  }
});

test("同步闸在所有等待之后才查:等外壳回执期间开始跟场,照样不刷", async () => {
  let attaching = false;
  const late = ports({
    probeShell: async () => { attaching = true; return { status: 200, contentType: "text/html" }; },
    deviceTransportBusy: () => attaching,
  });
  assert.equal(await reloadStalePatientPage(late.value), "busy");
  assert.equal(late.events.includes("reload"), false);
});

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory).flatMap((name) => {
    const entry = new URL(name, directory);
    if (statSync(entry).isDirectory()) return sourceFiles(new URL(`${name}/`, directory));
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx|mjs)$/.test(name) ? [entry] : [];
  });
}

test("老人端源码里整页刷新/跳转只出现在这个模块,而且只有一处", () => {
  const navigation = /\blocation\.(?:reload|replace|assign)\b|\blocation\.href\s*=|\bhistory\.go\(/g;
  const offenders: string[] = [];
  let reloads = 0;
  for (const file of sourceFiles(new URL("./", import.meta.url))) {
    const hits = readFileSync(file, "utf8").match(navigation) ?? [];
    if (!hits.length) continue;
    if (file.pathname.endsWith("/patientStaleReload.ts")) reloads += hits.length;
    else offenders.push(file.pathname);
  }
  assert.deepEqual(offenders, []);
  assert.equal(reloads, 1);
});

test("只有独立 /patient 页签允许刷新;叠层宿主与操作端同一页面,永不出现入口", () => {
  const app = readFileSync(new URL("../App.tsx", import.meta.url), "utf8");
  const host = app.slice(app.indexOf("function PatientViewHost()"), app.indexOf("function HoldToExit("));
  assert.match(host, /<PatientShell \/>/);
  assert.doesNotMatch(host, /allowPageReload/);
  assert.match(app, /<Route path="\/patient" element=\{[\s\S]{0,200}<PatientShell allowPageReload \/>/);
  assert.equal(app.match(/allowPageReload/g)?.length, 1);
});

test("PatientShell 只在问候页与结束页渲染入口,判定与安全检查都走这个模块", () => {
  const shell = readFileSync(new URL("./PatientShell.tsx", import.meta.url), "utf8");
  const greeting = shell.slice(shell.indexOf("if (!session) {"), shell.indexOf("} else if (terminal) {"));
  const ended = shell.slice(shell.indexOf("} else if (terminal) {"),
    shell.indexOf('} else if (session.mode === "rapport") {'));
  const training = shell.slice(shell.indexOf('} else if (session.mode === "rapport") {'));
  const entry = /\{staleReloadOffered && <StaleReloadEntry ports=\{staleReloadPorts\} \/>\}/;
  assert.match(greeting, entry);
  assert.match(ended, entry);
  assert.doesNotMatch(training.slice(0, training.indexOf("return <>")), /StaleReloadEntry/);
  assert.match(shell, /export function PatientShell\(\{ allowPageReload = false \}/);
  assert.match(shell, /useDeployedBuildId\(allowPageReload && import\.meta\.env\.PROD\)/);
  assert.match(shell, /shouldOfferStaleReload\(\{/);
  assert.match(shell, /screen: !session \? "greeting" : terminal \? "complete" : "session"/);
  assert.match(shell, /hasActiveCapability: getDeviceCapability\(\) !== null/);
  assert.match(shell, /deviceTransportBusy: devicePairingOrAttachInFlight/);
  assert.match(shell, /reloadStalePatientPage\(ports\)/);
  // 叠层里的页面刷新会把整个操作端一起重载:入口不允许绕开模块直接刷新。
  assert.doesNotMatch(shell, /location\./);
});
