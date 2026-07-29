import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ApiError } from "../apiResponse.ts";
import { parseAutopilotStatusReceipt } from "../autopilot/startControl.ts";
import { planAutopilotProbeFailure } from "../patient/autopilotProbePolicy.ts";
import {
  autopilotWakeToken,
  liveSnapshotWake,
  nextServerOwnershipWake,
  parseAutopilotWake,
  PatientProbeWakeCoordinator,
  type AutopilotWakeMsg,
  type PatientWakeMode,
} from "./autopilotWake.ts";

const SESSION_ID = "S-a1b2c3d4";

function notActive(): ApiError {
  return new ApiError(
    409, "probe", { code: "autopilot_not_active", message: "probe" }, "nested-detail");
}

function ownedReceipt(stateRevision: number, status = "waiting_tts", kind: string | null = "tts") {
  return parseAutopilotStatusReceipt({
    scope_key: "p0a_sim_first_single_v1",
    mode: "autonomous",
    status,
    state_revision: stateRevision,
    server_owned: true,
    current_command_kind: kind,
    last_error_code: null,
  });
}

test("wake payloads are strict: exact keys, bounded id, positive integer revision", () => {
  assert.deepEqual(
    parseAutopilotWake({ sessionId: SESSION_ID, stateRevision: 3 }),
    { sessionId: SESSION_ID, stateRevision: 3 });
  for (const bad of [
    null, [], "S-1", { sessionId: SESSION_ID }, { stateRevision: 1 },
    { sessionId: SESSION_ID, stateRevision: 1, extra: true },
    { sessionId: "", stateRevision: 1 },
    { sessionId: "a\u0000b", stateRevision: 1 },
    { sessionId: "x".repeat(129), stateRevision: 1 },
    { sessionId: SESSION_ID, stateRevision: 0 },
    { sessionId: SESSION_ID, stateRevision: -2 },
    { sessionId: SESSION_ID, stateRevision: 1.5 },
    { sessionId: SESSION_ID, stateRevision: "1" },
  ]) {
    assert.equal(parseAutopilotWake(bad), null);
  }
});

test("only an owner-proving receipt emits a wake, once per authoritative revision", () => {
  const receipt = ownedReceipt(3);
  const first = nextServerOwnershipWake(null, SESSION_ID, receipt);
  assert.deepEqual(first, { sessionId: SESSION_ID, stateRevision: 3 });
  const token = autopilotWakeToken(first as AutopilotWakeMsg);
  // 相同 session+相同权威版本的重复轮询:不反复唤醒。
  assert.equal(nextServerOwnershipWake(token, SESSION_ID, receipt), null);
  // 版本推进才可能再唤醒(患者端 server 在场时会标记完成、不重探)。
  assert.notEqual(nextServerOwnershipWake(token, SESSION_ID, ownedReceipt(4)), null);
  // serverOwned=false 绝不唤醒;checking/rejected/uncertain 无权威回执,结构上发不出。
  assert.equal(nextServerOwnershipWake(null, SESSION_ID, {
    serverOwned: false, stateRevision: 9,
  }), null);
});

test("patient coordinator refuses foreign, stale, empty, malformed and repeated wakes", () => {
  const coordinator = new PatientProbeWakeCoordinator();
  assert.equal(coordinator.receive({ sessionId: "S-old", stateRevision: 1 }, SESSION_ID), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1 }, null), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1 }, ""), false);
  assert.equal(coordinator.receive("garbage", SESSION_ID), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1, x: 1 }, SESSION_ID), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1 }, SESSION_ID), true);
  // 同 token 重复事件:已待命,不再翻新。
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1 }, SESSION_ID), false);
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: true }), true);
  // 一次性:同 token 消费后既不能再消费,也不能再锁存。
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: true }), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 1 }, SESSION_ID), false);
});

test("a wake latched during an in-flight probe releases exactly one re-probe on legacy resolve", () => {
  const coordinator = new PatientProbeWakeCoordinator();
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 5 }, SESSION_ID), true);
  // 探测在途:持有不丢弃、不放行(闭合 start 与初次探测的竞态)。
  assert.equal(coordinator.consume({ mode: "probing", probeResolved: false }), false);
  assert.equal(coordinator.consume({ mode: "blocked", probeResolved: true }), false);
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: false }), false);
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: true }), true);
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: true }), false);
});

test("a wake that finds the server runner already mounted is served without side effects", () => {
  const coordinator = new PatientProbeWakeCoordinator();
  coordinator.receive({ sessionId: SESSION_ID, stateRevision: 7 }, SESSION_ID);
  assert.equal(coordinator.consume({ mode: "server", probeResolved: true }), false);
  // 已达成目的的 token 之后再也不会触发探测——包括 runner 稍后回到 legacy 时。
  assert.equal(coordinator.consume({ mode: "legacy", probeResolved: true }), false);
  assert.equal(coordinator.receive({ sessionId: SESSION_ID, stateRevision: 7 }, SESSION_ID), false);
});

// 复现真实接线顺序的最小患者端状态机:字段与 usePatientAutopilot 一一对应
// (probeEpoch/probeKey/resolvedProbeKey/visibleMode/唤醒协调器),探测失败判定
// 与 console 回执解析均使用生产同款函数,不是纯 predicate 摆拍。
class PatientHarness {
  probeEpoch = 0;
  probeRequests = 0;
  mode: PatientWakeMode = "probing";
  probeKey = `${SESSION_ID}\u0000cap-a`;
  resolvedProbeKey = "";
  coordinator = new PatientProbeWakeCoordinator();
  serverActive = false;

  // 对应所有权探测 effect 的一次执行(probeEpoch/probeKey 变化才会发生)。
  probeOnce(): void {
    this.probeRequests += 1;
    if (this.serverActive) {
      this.mode = "server";
      this.resolvedProbeKey = this.probeKey;
      this.evaluate();
      return;
    }
    const plan = planAutopilotProbeFailure(notActive(), 0);
    assert.equal(plan.action, "stop-legacy");
    this.mode = "legacy";
    this.resolvedProbeKey = this.probeKey;
    this.evaluate();
  }

  // 对应窗内事件监听:只锁存,消费交给状态评估。
  deliverWake(detail: unknown): void {
    this.coordinator.receive(detail, SESSION_ID);
    this.evaluate();
  }

  // 对应消费 effect:恰好一次 probeEpoch+1 → 重新探测。
  evaluate(): void {
    const probeResolved = this.probeKey !== "" && this.probeKey === this.resolvedProbeKey;
    if (this.coordinator.consume({ mode: this.mode, probeResolved })) {
      this.probeEpoch += 1;
      this.resolvedProbeKey = "";
      this.mode = "probing";
      this.probeOnce();
    }
  }
}

test("wiring order: initial /next inactive → activation → owner receipt → one re-probe → server", () => {
  const patient = new PatientHarness();
  patient.probeOnce();
  assert.equal(patient.mode, "legacy");
  assert.equal(patient.probeRequests, 1);
  patient.evaluate();
  patient.evaluate();
  // 闩住即停:没有唤醒就没有后续 /next 请求(无后台 standby 轮询)。
  assert.equal(patient.probeRequests, 1);

  // 老人激活 → console 启动成功,权威回执 owner=true → 一次性唤醒。
  patient.serverActive = true;
  let lastToken: string | null = null;
  const consoleEmit = (revision: number) => {
    const wake = nextServerOwnershipWake(lastToken, SESSION_ID, ownedReceipt(revision));
    if (wake) {
      lastToken = autopilotWakeToken(wake);
      patient.deliverWake({ ...wake });
    }
  };
  consoleEmit(1);
  assert.equal(patient.mode, "server");
  assert.equal(patient.probeEpoch, 1);
  assert.equal(patient.probeRequests, 2);

  // 同一 serverOwned 回执被 2.5 秒轮询反复接收:console 去重,零新请求。
  consoleEmit(1);
  consoleEmit(1);
  assert.equal(patient.probeEpoch, 1);
  assert.equal(patient.probeRequests, 2);

  // 新 stateRevision(命令推进)会再发唤醒,但 server runner 在场:
  // 标记完成、不重建媒体 runner、不再探测。
  consoleEmit(2);
  assert.equal(patient.mode, "server");
  assert.equal(patient.probeEpoch, 1);
  assert.equal(patient.probeRequests, 2);
});

test("a lost start response wakes only after the status reconciliation proves ownership", () => {
  const patient = new PatientHarness();
  patient.probeOnce();
  assert.equal(patient.mode, "legacy");

  // start 已在服务端提交但响应丢失:console 处于 uncertain——没有权威回执可传,
  // 结构上发不出唤醒;patient 不动。
  assert.equal(patient.probeRequests, 1);

  // 2.5 秒权威 /status 对账证明 owner → 同一 acceptReceipt 收口发出唤醒。
  patient.serverActive = true;
  const reconciled = nextServerOwnershipWake(null, SESSION_ID, ownedReceipt(1));
  assert.notEqual(reconciled, null);
  patient.deliverWake({ ...(reconciled as AutopilotWakeMsg) });
  assert.equal(patient.mode, "server");
  assert.equal(patient.probeEpoch, 1);
  assert.equal(patient.probeRequests, 2);
});

test("foreign-session and malformed wakes leave the latched legacy runner untouched", () => {
  const patient = new PatientHarness();
  patient.probeOnce();
  patient.serverActive = true;
  patient.deliverWake({ sessionId: "S-other", stateRevision: 1 });
  patient.deliverWake({ sessionId: SESSION_ID, stateRevision: 0 });
  patient.deliverWake("garbage");
  assert.equal(patient.mode, "legacy");
  assert.equal(patient.probeEpoch, 0);
  assert.equal(patient.probeRequests, 1);
});

test("console ownership receipts are actually wired to the patient probe epoch", () => {
  // 无法在本测试环境完整 mount React,以接线守卫锁住关键连接:
  // console 权威回执收口 → 唤醒事件 → 患者 hook 协调器 → probeEpoch。
  const consoleSource = readFileSync(
    new URL("../console/scoring/ServerAutopilotControl.tsx", import.meta.url), "utf8");
  assert.match(consoleSource, /nextServerOwnershipWake\(lastWakeToken\.current, session\.session_id, receipt\)/);
  assert.match(consoleSource, /PATIENT_AUTOPILOT_WAKE_EVENT, \{ detail: wake \}/);
  const hookSource = readFileSync(
    new URL("../patient/usePatientAutopilot.ts", import.meta.url), "utf8");
  assert.match(hookSource, /addEventListener\(PATIENT_AUTOPILOT_WAKE_EVENT/);
  assert.match(hookSource, /wakeCoordinatorRef\.current\?\.receive\(detail, sessionId\)/);
  assert.match(hookSource, /wakeCoordinatorRef\.current\?\.consume\(\{ mode: visibleMode, probeResolved \}\)/);
  assert.match(hookSource, /setProbeEpoch\(\(value\) => value \+ 1\)/);
  // 场次切换必须清空协调器(旧场 token 不得跨场存活)。
  assert.match(hookSource, /wakeCoordinatorRef\.current\?\.reset\(\)/);
});

// ---------------------------------------------------------------------------
// 跨设备路径：console 与患者端在两台设备/两个独立浏览器上，同窗 CustomEvent 到不了。
// ---------------------------------------------------------------------------
test("live-snapshot wakes are strict and must match the session in the same snapshot", () => {
  assert.deepEqual(
    liveSnapshotWake({ sessionId: SESSION_ID, stateRevision: 2 }, SESSION_ID),
    { sessionId: SESSION_ID, stateRevision: 2 });
  // 旧后端没有这个字段；缺字段不是唤醒，也不能变成异常。
  assert.equal(liveSnapshotWake(undefined, SESSION_ID), null);
  assert.equal(liveSnapshotWake(null, SESSION_ID), null);
  // 畸形一律拒绝。
  for (const bad of [
    "garbage", [], { sessionId: SESSION_ID },
    { sessionId: SESSION_ID, stateRevision: 0 },
    { sessionId: SESSION_ID, stateRevision: 1, extra: 1 },
  ]) {
    assert.equal(liveSnapshotWake(bad, SESSION_ID), null);
  }
  // 串场：唤醒指向的不是这次快照里的 session。
  assert.equal(
    liveSnapshotWake({ sessionId: "S-other", stateRevision: 1 }, SESSION_ID), null);
  // 快照里根本没有 session 时不发唤醒。
  assert.equal(
    liveSnapshotWake({ sessionId: SESSION_ID, stateRevision: 1 }, null), null);
  assert.equal(
    liveSnapshotWake({ sessionId: SESSION_ID, stateRevision: 1 }, ""), null);
});

test("cross-device: first /next 409 → no window event → live snapshot → exactly one re-probe", () => {
  const patient = new PatientHarness();
  // 1. 患者端在独立浏览器里配对激活后首探：权威 409 autopilot_not_active，闩住。
  patient.probeOnce();
  assert.equal(patient.mode, "legacy");
  assert.equal(patient.probeRequests, 1);

  // 2. console 在另一台设备上 start 成功。同窗事件不存在，患者端什么也收不到。
  assert.equal(patient.probeEpoch, 0);
  assert.equal(patient.probeRequests, 1);

  // 3. 下一次 1.5 秒 /live/state 快照带回服务端权威所有权投影。
  patient.serverActive = true;
  const snapshot = { seq: 7, autopilotWake: { sessionId: SESSION_ID, stateRevision: 1 } };
  const wake = liveSnapshotWake(snapshot.autopilotWake, SESSION_ID);
  assert.notEqual(wake, null);
  patient.deliverWake(wake);
  assert.equal(patient.mode, "server");
  assert.equal(patient.probeEpoch, 1);
  assert.equal(patient.probeRequests, 2);   // 恰好一次重探测

  // 4. 同一 token 的重复快照不再产生任何请求——轮询继续，探测不叠加。
  for (let i = 0; i < 5; i += 1) {
    patient.deliverWake(liveSnapshotWake(snapshot.autopilotWake, SESSION_ID));
  }
  assert.equal(patient.probeRequests, 2);
  assert.equal(patient.probeEpoch, 1);

  // 5. server 已在场时，更高的 revision 也不得重建媒体 runner。
  patient.deliverWake(
    liveSnapshotWake({ sessionId: SESSION_ID, stateRevision: 9 }, SESSION_ID));
  assert.equal(patient.mode, "server");
  assert.equal(patient.probeRequests, 2);
});

test("a still-inactive session never turns live polling into a /next standby loop", () => {
  const patient = new PatientHarness();
  patient.probeOnce();
  // 服务端没有所有权 → 每次快照的投影都是 null → 一次请求都不新增。
  for (let i = 0; i < 10; i += 1) {
    patient.deliverWake(liveSnapshotWake(null, SESSION_ID));
  }
  assert.equal(patient.mode, "legacy");
  assert.equal(patient.probeEpoch, 0);
  assert.equal(patient.probeRequests, 1);
});

test("live poll emitter is wired before the unchanged-seq early return, and probing drops legacy", () => {
  const liveSource = readFileSync(
    new URL("./useLiveCursor.ts", import.meta.url), "utf8");
  const wakeAt = liveSource.indexOf("liveSnapshotWake(d.autopilotWake");
  const earlyReturnAt = liveSource.indexOf("if (!firstServerSnapshot && seq === lastSeq.current) return;");
  assert.ok(wakeAt > 0, "useLiveCursor 必须消费 autopilotWake 投影");
  assert.ok(earlyReturnAt > 0);
  // start 不推进 LiveState.seq：放在 early-return 之后就永远收不到跨设备唤醒。
  assert.ok(wakeAt < earlyReturnAt, "唤醒必须在 seq 未变的 early-return 之前处理");
  assert.match(liveSource, /PATIENT_AUTOPILOT_WAKE_EVENT, \{ detail: wake \}/);
  // 复用既有一次性 probeEpoch 路径，不建第二套 runner。
  assert.doesNotMatch(liveSource, /autopilot\/next/);

  // 唤醒被接受后可视态转 probing；PatientShell 在同一 render 边界撤下 legacy 子树，
  // 老人端不会继续走 generic /tts/speak。
  const shellSource = readFileSync(
    new URL("../patient/PatientShell.tsx", import.meta.url), "utf8");
  assert.match(shellSource, /autopilot\.mode === "legacy"/);
  assert.match(shellSource, /autopilot\.mode !== "legacy"/);
});
