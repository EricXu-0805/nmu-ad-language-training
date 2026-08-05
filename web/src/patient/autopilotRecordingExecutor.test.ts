import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  captureIdentityIsNewer,
  capturePhaseMatches,
  createStopLatch,
  finalizeCaptureThenNotify,
  sameCaptureIdentity,
  type LocalAutopilotCapturePhase,
} from "./autopilotCapturePresentation.ts";
import {
  BrowserAutopilotRecordingExecutor,
  finishExactCapture,
  stageAndFinishExactCapture,
  type AutopilotCaptureBootstrapPorts,
  type ExactCaptureUploadPorts,
} from "./autopilotRecordingExecutor.ts";
import { attachAutopilotStopReason, createAudioOutboxEntry } from "../audio/audioOutbox.ts";
import { sha256Blob } from "../audio/audioUploadReceipt.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import { PRE_START_BUDGET_MS } from "./autopilotCaptureWindow.ts";
import type { AudioDeviceLease } from "../audio/audioDeviceLease.ts";
import type { RecordingAuthorization } from "./recordingAuthorization.ts";
import type { AutopilotRecoverySnapshot } from "./autopilotRecordingRecovery.ts";
import type { NextCommandProjection } from "./autopilotProtocol.ts";

const SESSION = "S-CAPTURE-01";
const COMMAND = "cmd-record-persist-0001";
const IDENTITY = {
  sessionId: SESSION,
  commandKey: COMMAND,
  ownerGeneration: 3,
  captureGeneration: 1,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function phase(stopReason: "user_done" | "max_duration"): LocalAutopilotCapturePhase {
  return { ...IDENTITY, stopReason, phase: "persisting" };
}

const OK = () => {};

for (const stopReason of ["max_duration", "user_done"] as const) {
  test(`${stopReason} 收麦：stop 真正 resolve 之前不得报告"已收音"`, async () => {
    const gate = deferred<{ blob: { size: number } }>();
    const seen: LocalAutopilotCapturePhase[] = [];

    const pending = finalizeCaptureThenNotify(
      () => gate.promise, OK, phase(stopReason), (event) => { seen.push(event); });

    // The microphone is still physically open here: nothing may be announced.
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(seen, []);

    const recording = { blob: { size: 128 } };
    gate.resolve(recording);
    const returned = await pending;

    // Exactly one event, carrying the identity the UI matches on.
    assert.deepEqual(seen, [{ ...IDENTITY, stopReason, phase: "persisting" }]);
    // The recording itself is passed through untouched.
    assert.equal(returned, recording);
  });
}

test("合法录音：事件在两项校验之后、staging 之前恰好发生一次", async () => {
  const order: string[] = [];
  const recording = { blob: { size: 96 } };

  const returned = await finalizeCaptureThenNotify(
    async () => { order.push("stop"); return recording; },
    () => { order.push("validate"); },
    phase("user_done"),
    () => { order.push("observe"); },
  );
  // 生产链在本 helper 返回之后才 stage：把它记在这里，顺序即可读。
  order.push("stage");

  assert.deepEqual(order, ["stop", "validate", "observe", "stage"]);
  assert.equal(returned, recording);
});

test("observer 抛错是纯 UI 信号失败，不得改变采集结果", async () => {
  const recording = { blob: { size: 32 } };
  let called = 0;

  const returned = await finalizeCaptureThenNotify(
    async () => recording,
    OK,
    phase("max_duration"),
    () => { called += 1; throw new Error("UI observer exploded"); },
  );

  assert.equal(called, 1);
  assert.equal(returned, recording);
});

test("stop 失败时不得报告已收音", async () => {
  const seen: LocalAutopilotCapturePhase[] = [];
  await assert.rejects(
    () => finalizeCaptureThenNotify(
      async () => { throw new Error("recorder failed"); },
      OK,
      phase("user_done"),
      (event) => { seen.push(event); }),
    /recorder failed/,
  );
  assert.deepEqual(seen, []);
});

test("空字节录音被判死时不得先写一个不真实的保存态", async () => {
  const seen: LocalAutopilotCapturePhase[] = [];
  await assert.rejects(
    () => finalizeCaptureThenNotify(
      async () => ({ blob: { size: 0 } }),
      (recording) => {
        if (recording.blob.size <= 0) throw new Error("录音没有产生可持久化字节");
      },
      phase("user_done"),
      (event) => { seen.push(event); }),
    /录音没有产生可持久化字节/,
  );
  assert.deepEqual(seen, []);
});

test("超过命令时限的录音同样不得先报告已收音", async () => {
  const seen: LocalAutopilotCapturePhase[] = [];
  await assert.rejects(
    () => finalizeCaptureThenNotify(
      async () => ({ blob: { size: 4096 }, durationSeconds: 99 }),
      () => { throw new Error("录音时长超过命令上限"); },
      phase("max_duration"),
      (event) => { seen.push(event); }),
    /录音时长超过命令上限/,
  );
  assert.deepEqual(seen, []);
});

test("没有 observer 时也照常返回录音", async () => {
  const recording = { blob: { size: 16 } };
  assert.equal(
    await finalizeCaptureThenNotify(async () => recording, OK, phase("user_done")),
    recording,
  );
});

test("停止闩门：user_done 先到，迟到的 max_duration 既不改写也不重复 resolve", async () => {
  const latch = createStopLatch<"user_done" | "max_duration">();
  const settled: string[] = [];
  const waiting = latch.wait().then((reason) => { settled.push(reason); return reason; });

  assert.equal(latch.signal("user_done"), true);
  // 迟到的上限定时器：既不能改写胜者，也不能让等待方第二次结算。
  assert.equal(latch.signal("max_duration"), false);
  assert.equal(latch.signal("user_done"), false);

  assert.equal(await waiting, "user_done");
  assert.deepEqual(settled, ["user_done"]);
  assert.equal(latch.reason, "user_done");
  // 闩门已定后再 wait 仍拿到同一个胜者，不会挂住。
  assert.equal(await latch.wait(), "user_done");
});

test("停止闩门：max_duration 先到，重复 user_done 不得夺回", async () => {
  const latch = createStopLatch<"user_done" | "max_duration">();
  assert.equal(latch.signal("max_duration"), true);
  assert.equal(latch.signal("user_done"), false);
  assert.equal(latch.signal("user_done"), false);
  assert.equal(latch.reason, "max_duration");
  assert.equal(await latch.wait(), "max_duration");
});

test("停止闩门：未发信号前 wait 不结算", async () => {
  const latch = createStopLatch<"user_done" | "max_duration">();
  let settled = false;
  void latch.wait().then(() => { settled = true; });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(latch.reason, null);
});

test("身份匹配同时比对 session 与 command，旧事件一律不认", () => {
  const current = phase("user_done");
  assert.equal(capturePhaseMatches(current, SESSION, COMMAND), true);
  assert.equal(capturePhaseMatches(current, "S-OTHER", COMMAND), false);
  assert.equal(capturePhaseMatches(current, SESSION, "cmd-record-persist-0002"), false);
  assert.equal(capturePhaseMatches(null, SESSION, COMMAND), false);
  assert.equal(capturePhaseMatches(current, null, COMMAND), false);
  assert.equal(capturePhaseMatches(current, SESSION, undefined), false);
});

test("四段身份：只有全等的 clear 才算同一次捕获，旧代际一律不认", () => {
  assert.equal(sameCaptureIdentity(IDENTITY, { ...IDENTITY }), true);
  for (const drift of [
    { sessionId: "S-OTHER" },
    { commandKey: "cmd-record-persist-0002" },
    { ownerGeneration: 2 },
    { captureGeneration: 2 },
  ]) {
    assert.equal(sameCaptureIdentity(IDENTITY, { ...IDENTITY, ...drift }), false);
  }
  assert.equal(sameCaptureIdentity(null, IDENTITY), false);
  assert.equal(sameCaptureIdentity(IDENTITY, undefined), false);
});

test("迟到事件不得覆盖新捕获：新 owner 赢，旧 owner 与旧 capture 一律丢弃", () => {
  assert.equal(captureIdentityIsNewer(IDENTITY, null), true);
  assert.equal(captureIdentityIsNewer(IDENTITY, IDENTITY), true);
  assert.equal(captureIdentityIsNewer(
    { ...IDENTITY, captureGeneration: 2 }, IDENTITY), true);
  assert.equal(captureIdentityIsNewer(
    { ...IDENTITY, captureGeneration: 0 }, IDENTITY), false);
  assert.equal(captureIdentityIsNewer(
    { ...IDENTITY, ownerGeneration: 4, captureGeneration: 0 }, IDENTITY), true);
  assert.equal(captureIdentityIsNewer(
    { ...IDENTITY, ownerGeneration: 2, captureGeneration: 99 }, IDENTITY), false);
});

function recordCommand(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: COMMAND,
    command_seq: 2,
    kind: "record",
    state: "pending",
    command_revision: 0,
    control_generation: 1,
    runner_generation: 1,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      raw_audio_id: "raw-capture-persist-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

/** 本文件自己的 track 替身：逐条可计数，不依赖任何别处的测试文件。 */
interface TrackDouble { stopped: number; stop(): void }

function track(): TrackDouble {
  const row: TrackDouble = { stopped: 0, stop() { row.stopped += 1; } };
  return row;
}

/**
 * 记下并原样还原被替身覆盖过的全局描述符。
 *
 * 不还原的话，装过替身的测试会把 navigator/MediaRecorder 留给后面的测试，
 * 谁先跑谁赢——那种绿是假的。
 */
function restoreGlobals(names: readonly string[]): () => void {
  const saved = names.map((name) =>
    [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const);
  return () => {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete (globalThis as Record<string, unknown>)[name];
    }
  };
}

test("页面已隐藏时连 capture 都不造：零 getUserMedia、零代际推进", (context) => {
  context.after(restoreGlobals(["navigator"]));
  let mediaRequests = 0;
  Object.defineProperty(globalThis, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: async () => { mediaRequests += 1; throw new Error("不应请求麦克风"); },
      },
    },
    configurable: true,
    writable: true,
  });
  const seen: unknown[] = [];
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 7,
    isForeground: () => false,
    observe: (event) => { seen.push(event); },
  });

  assert.throws(() => executor.start(recordCommand() as never), /离开前台/);
  assert.equal(mediaRequests, 0);
  assert.deepEqual(seen, []);
});

test("每次开麦推进 captureGeneration，同一 owner 内不复用身份", () => {
  const created: Array<{ ownerGeneration: number; captureGeneration: number }> = [];
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 9,
    isForeground: () => true,
    // 观察者在 run() 的第一次异步失败里就会收到 cleared，足以读出身份。
    observe: (event) => { created.push(event); },
  });
  // 造两次捕获；它们的异步 run() 会各自失败（本测试没有装设备双),
  // 这里只断言身份分配，不断言那条链的结果。
  const first = executor.start(recordCommand() as never);
  const second = executor.start(recordCommand() as never);
  first.stopped.catch(() => {});
  first.started.catch(() => {});
  second.stopped.catch(() => {});
  second.started.catch(() => {});

  assert.notEqual(first, second);
  assert.equal((first as { locallyStarted: boolean }).locallyStarted, false);
});

// ---------------- 持久化链：合法 entry + 真实 Blob，逐阶段行为 ----------------

type ExactRecordCommand = Parameters<typeof finishExactCapture>[0];

/** 冻结的七个持久化边界，顺序就是生产链的顺序。 */
type UploadStage =
  | "stageOutbox" | "createAudio" | "putOutbox:registered" | "uploadAudioBlob"
  | "putOutbox:uploaded" | "putAudioSaved" | "putOutbox:receipt";

const UPLOAD_STAGES: readonly UploadStage[] = [
  "stageOutbox", "createAudio", "putOutbox:registered", "uploadAudioBlob",
  "putOutbox:uploaded", "putAudioSaved", "putOutbox:receipt",
];

function exactRecordCommand(): ExactRecordCommand {
  return recordCommand() as never as ExactRecordCommand;
}

function recordingBlob(): Blob {
  return new Blob([new Uint8Array([1, 2, 3, 4])], { type: "audio/webm" });
}

/** 生产构造器产出的合法 outbox 条目，不是手搓的、过不了 schema 的那种。 */
function validOutboxEntry(blob: Blob) {
  return attachAutopilotStopReason(createAudioOutboxEntry({
    rawAudioId: "raw-capture-persist-0001",
    sessionId: SESSION,
    turnKey: "itm-0001#1",
    containsDirectIdentifier: false,
    durationSeconds: 3,
    blob,
  }), "user_done");
}

/**
 * 逐阶段可注入的上传端口。每一阶段先记账再决定抛不抛，所以"失败的那一步就是
 * 最后一步"这条断言不会被记账顺序糊弄过去。
 */
function uploadPorts(calls: UploadStage[], failAt: UploadStage | null): ExactCaptureUploadPorts {
  let putOutboxSeen = 0;
  const record = (stage: UploadStage) => {
    calls.push(stage);
    if (stage === failAt) throw new Error(`阶段失败:${stage}`);
  };
  return {
    stageOutbox: async () => { record("stageOutbox"); },
    createAudio: async (body) => {
      record("createAudio");
      return { raw_audio_id: body.raw_audio_id, registered: true };
    },
    uploadAudioBlob: async (rawAudioId, blob) => {
      record("uploadAudioBlob");
      return {
        raw_audio_id: rawAudioId,
        bytes: blob.size,
        checksum: await sha256Blob(blob),
        format: "audio/webm",
      };
    },
    putAudioSaved: async (payload) => {
      record("putAudioSaved");
      const row = payload as { rawAudioId: string };
      return {
        seq: 4,
        audioReceipt: { serverSeq: 77, rawAudioId: row.rawAudioId, idempotent: false },
      };
    },
    putOutbox: async () => {
      putOutboxSeen += 1;
      record(putOutboxSeen === 1 ? "putOutbox:registered"
        : putOutboxSeen === 2 ? "putOutbox:uploaded" : "putOutbox:receipt");
    },
  };
}

test("fresh capture：stage → register → upload → audioSaved 顺序 exact，成功返回服务器收据事实", async () => {
  const calls: UploadStage[] = [];
  const blob = recordingBlob();
  const completed = await stageAndFinishExactCapture(
    exactRecordCommand(), validOutboxEntry(blob), blob, uploadPorts(calls, null));

  assert.deepEqual(calls, [...UPLOAD_STAGES]);
  assert.deepEqual(completed.facts, {
    stop_reason: "user_done",
    raw_audio_id: "raw-capture-persist-0001",
    receipt_server_seq: 77,
    checksum: await sha256Blob(blob),
    byte_count: 4,
    duration_seconds: 3,
  });
});

test("上传链的每一个阶段都可以单独注入失败，且失败前不推进到下一阶段", async () => {
  for (const [index, stage] of UPLOAD_STAGES.entries()) {
    const calls: UploadStage[] = [];
    const blob = recordingBlob();

    await assert.rejects(() => stageAndFinishExactCapture(
      exactRecordCommand(), validOutboxEntry(blob), blob, uploadPorts(calls, stage)));

    // 失败的那一步就是最后一步：它之后的阶段一次都没被调用。
    assert.deepEqual(calls, UPLOAD_STAGES.slice(0, index + 1));
  }
});

test("刷新恢复不得 restage：finishExactCapture 自己零 stageOutbox", async () => {
  const calls: UploadStage[] = [];
  const blob = recordingBlob();
  await finishExactCapture(
    exactRecordCommand(), validOutboxEntry(blob), blob, uploadPorts(calls, null));

  assert.equal(calls.includes("stageOutbox"), false);
  assert.deepEqual(calls, UPLOAD_STAGES.slice(1));
  // supplemental：生产恢复依赖走的就是这一条，不是 fresh 专用的合并 helper。
  // 框定必须从**导出声明**开始。从第一处文字提及切起会把上面 helper 文档注释里
  // 那句 `browserAutopilotRecoveryDependencies.finish 因此继续直接调…` 也框进来，
  // 于是切片里混进 `stageAndFinishExactCapture` 的声明，变成一次假阳性。
  const source = readFileSync(
    new URL("./autopilotRecordingExecutor.ts", import.meta.url), "utf8");
  const declaration = "export const browserAutopilotRecoveryDependencies";
  const declaredAt = source.indexOf(declaration);
  assert.ok(declaredAt >= 0, "恢复依赖必须是一个导出的具名对象");
  const recovery = source.slice(declaredAt);
  assert.match(recovery, /finishExactCapture\(command, entry, blob\)/);
  assert.doesNotMatch(recovery, /stageAndFinishExactCapture/);
});

test("辅助断言：生产采集链确实走的是这两个共享 helper", () => {
  // 顺序与闩门语义由上面的行为测试证明；这里只钉住"生产真的在用它们"，
  // 不承担主要证据。
  const source = readFileSync(
    new URL("./autopilotRecordingExecutor.ts", import.meta.url), "utf8");
  assert.match(source, /finalizeCaptureThenNotify\(/);
  assert.match(source, /createStopLatch</);
  assert.doesNotMatch(source, /private requestedStop/);
  // 两阶段的硬约束：prepare 之后才允许 startPrepared，且第二次授权排在它前面。
  assert.match(source, /this\.recorder\.prepare\(\)/);
  assert.match(source, /this\.recorder\.startPrepared\(\)/);
  assert.ok(
    source.indexOf("admitCaptureBeforeDeadline") < source.indexOf("startPrepared()"),
    "第二次授权必须排在 startPrepared 之前",
  );
});

// ---------------- Node 直接 import / 单一绝对预算 / 迟到租约 / onstart 交接 ----------------

async function flushMicrotasks(turns = 40): Promise<void> {
  for (let turn = 0; turn < turns; turn += 1) await Promise.resolve();
}

interface Clock { now(): number; set(value: number): void }

function clockDouble(startMs = 1_000): Clock {
  let nowMs = startMs;
  return { now: () => nowMs, set: (value: number) => { nowMs = value; } };
}

interface LeaseDouble {
  lease: AudioDeviceLease;
  releases: number;
  settleReleased(): void;
}

function leaseDouble(options: { releaseThrows?: boolean; manualRelease?: boolean } = {}): LeaseDouble {
  let acknowledge!: () => void;
  const released = new Promise<void>((resolve) => { acknowledge = resolve; });
  const row: LeaseDouble = {
    releases: 0,
    settleReleased: () => acknowledge(),
    lease: {
      release() {
        row.releases += 1;
        if (!options.manualRelease) acknowledge();
        if (options.releaseThrows) throw new Error("Web Lock 释放失败");
      },
      released,
    },
  };
  return row;
}

const EMPTY_SNAPSHOT: AutopilotRecoverySnapshot = {
  entries: [], legacyOrphans: [], invalidBlobKeyCount: 0,
};
const AUTHORIZED: RecordingAuthorization = {
  allowed: true, runtime_status: "active", is_simulation: true,
};

/** 可控时钟 + 可控定时器 + 可注入 lease/snapshot/authorize 的 pre-start 端口。 */
function bootstrapHarness(clock: Clock, overrides: {
  acquireLease?: (signal: AbortSignal) => Promise<AudioDeviceLease>;
  recoverySnapshot?: () => Promise<AutopilotRecoverySnapshot>;
  authorize?: (call: number, signal: AbortSignal) => Promise<RecordingAuthorization>;
  /** 第 N 次 setTimer 同步抛错。作答窗口那次是第 2 次（第 1 次是 pre-start 预算）。 */
  throwOnTimerCall?: number;
  /**
   * 第 N 次 setTimer 拿到的那个 handle：它**每一次** clear 都失败。
   *
   * 按武装序号而不是 clear 调用序号注入，测试才能提前指名那条定时器；持久失败
   * 才能表达"同一个 handle 被清了两次都没清掉"，只失败一次是另一回事。
   */
  failClearForArm?: number;
} = {}) {
  const calls: string[] = [];
  const timers = new Map<number, { dueAt: number; callback: () => void }>();
  const armedDelays: number[] = [];
  /** 武装顺序 → handle，测试据此指名 pre-start(第1个) 与作答窗口(第2个)。 */
  const armedHandles: number[] = [];
  /** 每一次 clear 尝试，含失败的那些，按发生顺序。 */
  const clearAttempts: number[] = [];
  /** 真正烧掉过的定时器，按发生顺序。 */
  const fired: number[] = [];
  let nextHandle = 1;
  let authorizeCalls = 0;
  let timerCalls = 0;
  let failingHandle: number | null = null;

  const ports: AutopilotCaptureBootstrapPorts = {
    now: () => clock.now(),
    setTimer: (callback, delayMs) => {
      timerCalls += 1;
      if (overrides.throwOnTimerCall === timerCalls) {
        throw new Error("定时器端口不可用");
      }
      armedDelays.push(delayMs);
      const handle = nextHandle;
      nextHandle += 1;
      armedHandles.push(handle);
      if (overrides.failClearForArm === armedHandles.length) failingHandle = handle;
      timers.set(handle, { dueAt: clock.now() + delayMs, callback });
      return handle;
    },
    clearTimer: (handle) => {
      clearAttempts.push(handle);
      // 失败必须发生在删除**之前**：否则就是"已经清掉了再抛"，那条 oracle 会
      // 变成假的——定时器其实已经不在了，却假装它还悬着。
      if (handle === failingHandle) throw new Error("定时器清除端口不可用");
      timers.delete(handle);
    },
    acquireLease: (signal) => {
      calls.push("acquireLease");
      return overrides.acquireLease?.(signal) ?? Promise.resolve(leaseDouble().lease);
    },
    recoverySnapshot: () => {
      calls.push("recoverySnapshot");
      return overrides.recoverySnapshot?.() ?? Promise.resolve(EMPTY_SNAPSHOT);
    },
    authorize: (_sessionId, _commandKey, signal) => {
      authorizeCalls += 1;
      calls.push(`authorize:${authorizeCalls}`);
      return overrides.authorize?.(authorizeCalls, signal) ?? Promise.resolve(AUTHORIZED);
    },
  };

  const fireDue = () => {
    for (const [handle, timer] of [...timers]) {
      if (timer.dueAt <= clock.now()) {
        timers.delete(handle);
        fired.push(handle);
        timer.callback();
      }
    }
  };

  return {
    ports,
    calls,
    armedDelays,
    armedHandles,
    clearAttempts,
    fired,
    /** 这个 handle 到底还挂着没有——"没清掉"必须能被直接看见。 */
    isPending(handle: number) { return timers.has(handle); },
    clearAttemptsFor(handle: number) {
      return clearAttempts.filter((row) => row === handle).length;
    },
    firesFor(handle: number) {
      return fired.filter((row) => row === handle).length;
    },
    get pendingTimers() { return timers.size; },
    advance(ms: number) {
      clock.set(clock.now() + ms);
      fireDue();
    },
  };
}

interface DeviceDouble {
  starts: number;
  stops: number;
  state: string;
  mimeType: string;
  fireStart(): void;
  fireStop(): void;
  pushData(bytes: number): void;
}

/** 可控的 getUserMedia / MediaRecorder / performance.now，事件全部手动触发。 */
function installDeviceDoubles(clock: Clock, options: {
  mimeType?: string;
  /** getUserMedia 悬在权限弹窗上，直到 openUserMedia() 才交出流。 */
  gateUserMedia?: boolean;
} = {}) {
  const tracks = [track(), track()];
  const devices: DeviceDouble[] = [];
  const previousNow = performance.now;
  const restoreOverwritten = restoreGlobals(["navigator", "MediaRecorder"]);
  let userMediaCalls = 0;
  let releaseUserMedia: (() => void) | null = null;
  const userMediaGate = new Promise<void>((resolve) => { releaseUserMedia = resolve; });

  class FakeMediaRecorder {
    state = "inactive";
    mimeType = options.mimeType ?? "audio/webm";
    starts = 0;
    stops = 0;
    onstart: (() => void) | null = null;
    onstop: (() => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    ondataavailable: ((event: { data: Blob }) => void) | null = null;

    constructor() { devices.push(this as unknown as DeviceDouble); }

    static isTypeSupported(): boolean { return true; }

    fireStart(): void { this.onstart?.(); }
    fireStop(): void { this.onstop?.(); }
    pushData(bytes: number): void {
      this.ondataavailable?.({
        data: new Blob([new Uint8Array(bytes)], { type: this.mimeType }),
      });
    }

    start(): void { this.starts += 1; this.state = "recording"; }
    stop(): void { this.stops += 1; this.state = "inactive"; }
  }

  performance.now = () => clock.now();
  Object.defineProperty(globalThis, "navigator", {
    value: {
      mediaDevices: {
        getUserMedia: async () => {
          userMediaCalls += 1;
          if (options.gateUserMedia) await userMediaGate;
          return { getTracks: () => tracks } as unknown as MediaStream;
        },
      },
    },
    configurable: true,
    writable: true,
  });
  (globalThis as Record<string, unknown>).MediaRecorder = FakeMediaRecorder;

  return {
    tracks,
    devices,
    get userMediaCalls() { return userMediaCalls; },
    /** 让悬着的 getUserMedia 现在才交出流。 */
    openUserMedia() { releaseUserMedia?.(); },
    restore() {
      performance.now = previousNow;
      restoreOverwritten();
    },
  };
}

async function pumpUntil(predicate: () => boolean, turns = 400): Promise<void> {
  for (let turn = 0; turn < turns && !predicate(); turn += 1) await Promise.resolve();
}

type TestCapture = ReturnType<BrowserAutopilotRecordingExecutor["start"]> & {
  interrupt(): "before_start" | "after_start" | "too_late" | "already_interrupted";
  requestStop(reason?: "user_done"): void;
};

test("Node 直接 import：模块作用域零 api.ts / 浏览器依赖加载链", async () => {
  // 主证据是行为本身：这份测试文件在没有 DOM、没有 localStorage 的 Node 里
  // 顶层 import 了生产执行器。R1 baseline 在这一步就会因为 api.ts 模块作用域读
  // localStorage 而让整份文件加载失败。
  const module = await import("./autopilotRecordingExecutor.ts");
  assert.equal(typeof module.stageAndFinishExactCapture, "function");
  assert.equal(typeof module.browserAutopilotCaptureBootstrapPorts, "object");

  // supplemental：钉住那两条链只以 dynamic import 的形式出现在调用体内。
  const source = readFileSync(
    new URL("./autopilotRecordingExecutor.ts", import.meta.url), "utf8");
  assert.doesNotMatch(source, /^import[^\n]*from "\.\.\/api\.ts";/m);
  assert.doesNotMatch(source, /^import[^\n]*autopilotBrowserMediaDependencies\.ts";/m);
  assert.match(source, /await import\("\.\.\/api\.ts"\)/);
  assert.match(source, /await import\("\.\/autopilotBrowserMediaDependencies\.ts"\)/);
});

test("绝对预算在第一项 async bootstrap 之前武装：租约悬挂即判死，且 closed 继续等它", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const uploadCalls: UploadStage[] = [];
  const harness = bootstrapHarness(clock, { acquireLease: () => new Promise<never>(() => {}) });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 1,
    isForeground: () => true,
    ports: uploadPorts(uploadCalls, null),
    bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never);
  capture.stopped.catch(() => {});
  await flushMicrotasks();

  // 定时器先于 acquireLease 的结果存在，而且只有这一个，预算就是 20,000 ms。
  assert.deepEqual(harness.armedDelays, [PRE_START_BUDGET_MS]);
  assert.deepEqual(harness.calls, ["acquireLease"]);

  harness.advance(PRE_START_BUDGET_MS);
  const error = await capture.started.then(() => null, (reason: unknown) => reason);
  assert.equal((error as AutopilotMediaError).errorCode, "device_command_timeout");
  // 快照、两次授权、getUserMedia、stage/upload 一次都没发生。
  assert.deepEqual(harness.calls, ["acquireLease"]);
  assert.equal(devices.userMediaCalls, 0);
  assert.deepEqual(uploadCalls, []);

  // acquisition 还没有结论：closed 必须继续等，绝不能提前宣布麦克风已经关掉。
  let closedSettled = false;
  void capture.closed.then(() => { closedSettled = true; });
  await flushMicrotasks();
  assert.equal(closedSettled, false);
});

test("截止时刻先赢、租约迟到：exact-once release，closed 等到 released 才 settle", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const late = leaseDouble({ manualRelease: true });
  let deliver!: (lease: AudioDeviceLease) => void;
  const acquisition = new Promise<AudioDeviceLease>((resolve) => { deliver = resolve; });
  const harness = bootstrapHarness(clock, { acquireLease: () => acquisition });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 2, isForeground: () => true, bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never);
  capture.started.catch(() => {});
  capture.stopped.catch(() => {});
  await flushMicrotasks();
  harness.advance(PRE_START_BUDGET_MS);
  await flushMicrotasks();

  let closedSettled = false;
  void capture.closed.then(() => { closedSettled = true; });
  assert.equal(late.releases, 0);

  // 非合作的 acquisition 现在才交出一把真实的锁。
  deliver(late.lease);
  await flushMicrotasks();
  assert.equal(late.releases, 1);
  assert.equal(closedSettled, false);

  late.settleReleased();
  await flushMicrotasks();
  assert.equal(closedSettled, true);
  assert.equal(late.releases, 1);   // 只释放这一次
});

test("迟到租约的 release 抛错：closed 仍然必须等到 released", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const late = leaseDouble({ manualRelease: true, releaseThrows: true });
  let deliver!: (lease: AudioDeviceLease) => void;
  const acquisition = new Promise<AudioDeviceLease>((resolve) => { deliver = resolve; });
  const harness = bootstrapHarness(clock, { acquireLease: () => acquisition });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 3, isForeground: () => true, bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never);
  capture.started.catch(() => {});
  capture.stopped.catch(() => {});
  await flushMicrotasks();
  harness.advance(PRE_START_BUDGET_MS);
  deliver(late.lease);
  await flushMicrotasks();

  let closedSettled = false;
  void capture.closed.then(() => { closedSettled = true; });
  await flushMicrotasks();
  assert.equal(late.releases, 1);
  assert.equal(closedSettled, false);

  late.settleReleased();
  await flushMicrotasks();
  assert.equal(closedSettled, true);
});

test("恢复快照悬挂：释放已经拿到的租约后 closed，迟到的快照不触发授权或开麦", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const held = leaseDouble();
  let resolveSnapshot!: (value: AutopilotRecoverySnapshot) => void;
  const harness = bootstrapHarness(clock, {
    acquireLease: async () => held.lease,
    recoverySnapshot: () => new Promise((resolve) => { resolveSnapshot = resolve; }),
  });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 4, isForeground: () => true, bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never);
  capture.started.catch(() => {});
  capture.stopped.catch(() => {});
  await flushMicrotasks();
  assert.deepEqual(harness.calls, ["acquireLease", "recoverySnapshot"]);

  harness.advance(PRE_START_BUDGET_MS);
  await capture.closed;
  assert.equal(held.releases, 1);

  // 旧快照现在才落地：不得恢复任何续延。
  resolveSnapshot(EMPTY_SNAPSHOT);
  await flushMicrotasks();
  assert.deepEqual(harness.calls, ["acquireLease", "recoverySnapshot"]);
  assert.equal(devices.userMediaCalls, 0);
  assert.equal(devices.devices.length, 0);
});

test("六阶段共用同一条绝对预算：分段耗时累计到 20,000 ms 判死，定时器只武装一次", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const held = leaseDouble();
  const leaseGate = deferred<AudioDeviceLease>();
  const snapshotGate = deferred<AutopilotRecoverySnapshot>();
  const harness = bootstrapHarness(clock, {
    acquireLease: () => leaseGate.promise,
    recoverySnapshot: () => snapshotGate.promise,
    authorize: () => new Promise<never>(() => {}),
  });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 6, isForeground: () => true, bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never);
  capture.stopped.catch(() => {});
  await flushMicrotasks();

  // 每一段单独看都远没到 20 秒；只有累计才会超。
  harness.advance(8_000);
  leaseGate.resolve(held.lease);
  await flushMicrotasks();
  assert.deepEqual(harness.calls, ["acquireLease", "recoverySnapshot"]);

  harness.advance(8_000);
  snapshotGate.resolve(EMPTY_SNAPSHOT);
  await flushMicrotasks();
  assert.deepEqual(harness.calls, ["acquireLease", "recoverySnapshot", "authorize:1"]);

  harness.advance(8_000);   // 累计 24s：绝对截止时刻在 20s 就该判死
  const error = await capture.started.then(() => null, (reason: unknown) => reason);
  assert.equal((error as AutopilotMediaError).errorCode, "device_command_timeout");
  // 每段各起一个预算的写法会让这里等于 3；共用一条只可能是 1。
  assert.deepEqual(harness.armedDelays, [PRE_START_BUDGET_MS]);
  assert.equal(devices.userMediaCalls, 0);
});

/** 把一条采集推进到"真实 onstart 已触发、续延还没恢复"的那一瞬间。 */
async function captureAtRealOnstart(
  context: { after(fn: () => void): void },
  options: {
    ownerGeneration?: number;
    uploadFailAt?: UploadStage | null;
    throwOnTimerCall?: number;
    failClearForArm?: number;
  } = {},
) {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const held = leaseDouble();
  const uploadCalls: UploadStage[] = [];
  const events: Array<{ phase: string }> = [];
  const harness = bootstrapHarness(clock, {
    acquireLease: async () => held.lease,
    throwOnTimerCall: options.throwOnTimerCall,
    failClearForArm: options.failClearForArm,
  });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: options.ownerGeneration ?? 7,
    isForeground: () => true,
    observe: (event) => { events.push(event); },
    ports: uploadPorts(uploadCalls, options.uploadFailAt ?? null),
    bootstrap: harness.ports,
  });

  const capture = executor.start(recordCommand() as never) as TestCapture;
  capture.stopped.catch(() => {});
  for (let turn = 0; turn < 400 && (devices.devices[0]?.starts ?? 0) === 0; turn += 1) {
    await Promise.resolve();
  }
  const device = devices.devices[0];
  assert.equal(device?.starts, 1, "startPrepared 必须已经同步下达开录指令");
  clock.set(clock.now() + 20);
  return { capture, device, devices, harness, clock, uploadCalls, events, held };
}

test("真实 onstart 之后、续延恢复之前中断：after_start + exact started，零 listening、零上传", async (context) => {
  const run = await captureAtRealOnstart(context);

  // 同一个同步栈：设备已经真的开录，executor 的 await 续延还没跑。
  run.device.fireStart();
  const disposition = run.capture.interrupt();

  assert.equal(disposition, "after_start");
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });
  const stopError = await run.capture.stopped.then(() => null, (reason: unknown) => reason);
  assert.equal((stopError as AutopilotMediaError).errorCode, "device_runtime_failed");
  await run.capture.closed;

  assert.equal(run.events.some((event) => event.phase === "listening"), false);
  // 只武装过 pre-start 那一个定时器：作答窗口一次都没起。
  assert.deepEqual(run.harness.armedDelays, [PRE_START_BUDGET_MS]);
  assert.deepEqual(run.uploadCalls, []);
  assert.equal(run.device.stops, 1);
  for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
  assert.equal(run.held.releases, 1);
});

test("完整成功采集：listening 与 persisting 之后才走 stage/register/upload/audioSaved", async (context) => {
  const run = await captureAtRealOnstart(context, { ownerGeneration: 8 });
  run.device.fireStart();
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });
  assert.equal(run.events.at(-1)?.phase, "listening");

  run.device.pushData(4);
  run.clock.set(run.clock.now() + 3_000);
  run.capture.requestStop("user_done");
  await flushMicrotasks();
  run.device.fireStop();

  const facts = await run.capture.stopped;
  await run.capture.closed;
  assert.deepEqual(run.uploadCalls, [...UPLOAD_STAGES]);
  assert.equal(facts.stop_reason, "user_done");
  assert.equal(facts.receipt_server_seq, 77);
  assert.equal(facts.byte_count, 4);
  assert.equal(facts.duration_seconds, 3);
  assert.equal(run.events.some((event) => event.phase === "persisting"), true);
  assert.equal(run.held.releases, 1);
});

test("真实时长顶穿命令上限：一个字节都不 stage、不上传", async (context) => {
  const run = await captureAtRealOnstart(context, { ownerGeneration: 9 });
  run.device.fireStart();
  await run.capture.started;

  run.device.pushData(4);
  run.clock.set(run.clock.now() + 16_000);   // 这条命令的上限是 15 秒
  run.capture.requestStop("user_done");
  await flushMicrotasks();
  run.device.fireStop();

  const error = await run.capture.stopped.then(() => null, (reason: unknown) => reason);
  assert.equal((error as AutopilotMediaError).errorCode, "recording_runtime_failed");
  assert.deepEqual(run.uploadCalls, []);
  await run.capture.closed;
});

test("被前台闸拒绝的开麦不消耗代际：同一 owner 的前两次合法开麦是 1 与 2", async (context) => {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock);
  context.after(() => devices.restore());
  const identities: Array<{ captureGeneration: number }> = [];
  let foreground = false;
  const harness = bootstrapHarness(clock, {
    acquireLease: () => Promise.reject(new Error("本测试不开麦")),
  });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: 12,
    isForeground: () => foreground,
    observe: (event) => { identities.push(event); },
    bootstrap: harness.ports,
  });

  assert.throws(() => executor.start(recordCommand() as never), /离开前台/);
  foreground = true;
  for (const capture of [
    executor.start(recordCommand() as never),
    executor.start(recordCommand() as never),
  ]) {
    capture.started.catch(() => {});
    capture.stopped.catch(() => {});
  }
  await flushMicrotasks();

  assert.deepEqual(identities.map((row) => row.captureGeneration), [1, 2]);
});

// ---------------- 生命周期闸：六段 pre-start 逐段收口 ----------------

type PreStartStage =
  | "lease" | "snapshot" | "authorize1" | "prepare" | "authorize2" | "onstart";

/** 每一段被卡住时，harness 已经记下的调用序列（prepare/onstart 不经 harness）。 */
const STAGE_CALLS: Record<PreStartStage, string[]> = {
  lease: ["acquireLease"],
  snapshot: ["acquireLease", "recoverySnapshot"],
  authorize1: ["acquireLease", "recoverySnapshot", "authorize:1"],
  prepare: ["acquireLease", "recoverySnapshot", "authorize:1"],
  authorize2: ["acquireLease", "recoverySnapshot", "authorize:1", "authorize:2"],
  onstart: ["acquireLease", "recoverySnapshot", "authorize:1", "authorize:2"],
};

/**
 * 把一条采集推进到指定的 pre-start 段并卡在那里。
 *
 * 卡住的那一段永不 settle；测试自己决定是让截止时刻先赢，还是 pagehide 先赢，
 * 以及那条迟到的底层操作最后 resolve 还是 reject。
 */
function pendingStageHarness(
  context: { after(fn: () => void): void },
  hangAt: PreStartStage,
  options: { ownerGeneration?: number; failClearForArm?: number } = {},
) {
  const clock = clockDouble();
  const devices = installDeviceDoubles(clock, { gateUserMedia: hangAt === "prepare" });
  context.after(() => devices.restore());
  const held = leaseDouble({ manualRelease: true });
  const uploadCalls: UploadStage[] = [];
  const late = {
    lease: deferred<AudioDeviceLease>(),
    snapshot: deferred<AutopilotRecoverySnapshot>(),
    authorize1: deferred<RecordingAuthorization>(),
    authorize2: deferred<RecordingAuthorization>(),
  };
  const harness = bootstrapHarness(clock, {
    acquireLease: () =>
      hangAt === "lease" ? late.lease.promise : Promise.resolve(held.lease),
    recoverySnapshot: () =>
      hangAt === "snapshot" ? late.snapshot.promise : Promise.resolve(EMPTY_SNAPSHOT),
    authorize: (call) => {
      if (call === 1 && hangAt === "authorize1") return late.authorize1.promise;
      if (call === 2 && hangAt === "authorize2") return late.authorize2.promise;
      return Promise.resolve(AUTHORIZED);
    },
    failClearForArm: options.failClearForArm,
  });
  const executor = new BrowserAutopilotRecordingExecutor(SESSION, {
    ownerGeneration: options.ownerGeneration ?? 30,
    isForeground: () => true,
    ports: uploadPorts(uploadCalls, null),
    bootstrap: harness.ports,
  });
  const capture = executor.start(recordCommand() as never) as TestCapture;
  capture.started.catch(() => {});
  capture.stopped.catch(() => {});

  const reached = () => {
    if (hangAt === "prepare") return devices.userMediaCalls === 1;
    if (hangAt === "onstart") return (devices.devices[0]?.starts ?? 0) === 1;
    return harness.calls.length === STAGE_CALLS[hangAt].length;
  };
  return {
    clock, devices, held, harness, capture, late, uploadCalls,
    settleAt: () => pumpUntil(reached),
  };
}

// 六段全覆盖：第一条 lifecycle 拒绝必须当场挡住下一段。
for (const hangAt of [
  "lease", "snapshot", "authorize1", "prepare", "authorize2", "onstart",
] as const) {
  test(`pre-start 第 ${hangAt} 段悬挂时 pagehide：立即判死并挡住下一段`, async (context) => {
    const run = pendingStageHarness(context, hangAt);
    await run.settleAt();
    const callsBefore = [...run.harness.calls];
    const userMediaBefore = run.devices.userMediaCalls;
    assert.deepEqual(callsBefore, STAGE_CALLS[hangAt]);

    // 真实 onstart 还没触发过：六段全部落在 before_start。
    assert.equal(run.capture.interrupt(), "before_start");

    const startError = await run.capture.started.then(() => null, (e: unknown) => e);
    assert.equal((startError as AutopilotMediaError).errorCode, "device_runtime_failed");
    const stopError = await run.capture.stopped.then(() => null, (e: unknown) => e);
    assert.equal((stopError as AutopilotMediaError).errorCode, "device_runtime_failed");

    // 后置闸拦住了下一段：它的 Promise 根本没被创建。
    await flushMicrotasks();
    assert.deepEqual(run.harness.calls, callsBefore);
    assert.equal(run.devices.userMediaCalls, userMediaBefore);
    assert.deepEqual(run.uploadCalls, []);
    // 只有第 6 段之前的段落连 MediaRecorder 都没造出来。
    if (hangAt !== "onstart") {
      assert.equal(
        run.devices.devices.reduce((total, row) => total + row.starts, 0), 0);
    }
  });
}

test("租约悬挂 + pagehide：closed 等 acquisition，迟到的锁 exact-once 释放", async (context) => {
  const run = pendingStageHarness(context, "lease", { ownerGeneration: 31 });
  await run.settleAt();
  run.capture.interrupt();
  await flushMicrotasks();

  let closedSettled = false;
  void run.capture.closed.then(() => { closedSettled = true; });
  const late = leaseDouble({ manualRelease: true });
  await flushMicrotasks();
  assert.equal(closedSettled, false);   // acquisition 还没有结论
  assert.equal(late.releases, 0);

  run.late.lease.resolve(late.lease);
  await flushMicrotasks();
  assert.equal(late.releases, 1);
  assert.equal(closedSettled, false);   // released 还没回来

  late.settleReleased();
  await flushMicrotasks();
  assert.equal(closedSettled, true);
  assert.equal(late.releases, 1);       // 只释放这一次
});

for (const settle of ["resolve", "reject"] as const) {
  test(`恢复快照悬挂 + pagehide：closed 不等它，迟到 ${settle} 零授权零开麦`, async (context) => {
    const run = pendingStageHarness(context, "snapshot", { ownerGeneration: 32 });
    await run.settleAt();
    run.capture.interrupt();

    // 无资源阶段：已知租约释放完就可以 closed，绝不等这条快照。
    run.held.settleReleased();
    await run.capture.closed;
    assert.equal(run.held.releases, 1);

    if (settle === "resolve") run.late.snapshot.resolve(EMPTY_SNAPSHOT);
    else run.late.snapshot.reject(new Error("恢复快照最终失败"));
    await flushMicrotasks();

    assert.deepEqual(run.harness.calls, STAGE_CALLS.snapshot);
    assert.equal(run.devices.userMediaCalls, 0);
    assert.deepEqual(run.uploadCalls, []);
  });
}

for (const settle of ["resolve", "reject"] as const) {
  test(`第一次授权悬挂 + pagehide：迟到 ${settle} 之后仍然零 prepare`, async (context) => {
    const run = pendingStageHarness(context, "authorize1", { ownerGeneration: 33 });
    await run.settleAt();
    run.capture.interrupt();
    run.held.settleReleased();
    await run.capture.closed;            // 无资源授权不得挡住 closed

    if (settle === "resolve") run.late.authorize1.resolve(AUTHORIZED);
    else run.late.authorize1.reject(new Error("授权最终失败"));
    await flushMicrotasks();

    assert.equal(run.devices.userMediaCalls, 0);   // 一次 getUserMedia 都没有
    assert.deepEqual(run.harness.calls, STAGE_CALLS.authorize1);
    assert.deepEqual(run.uploadCalls, []);
  });
}

for (const settle of ["resolve", "reject"] as const) {
  test(`第二次授权悬挂 + pagehide：已准备的流全关，迟到 ${settle} 零开录`, async (context) => {
    const run = pendingStageHarness(context, "authorize2", { ownerGeneration: 34 });
    await run.settleAt();
    assert.equal(run.devices.userMediaCalls, 1);   // 流已经到手，但还没开录

    run.capture.interrupt();
    run.held.settleReleased();
    await run.capture.closed;

    for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
    if (settle === "resolve") run.late.authorize2.resolve(AUTHORIZED);
    else run.late.authorize2.reject(new Error("复核最终失败"));
    await flushMicrotasks();

    assert.equal(
      run.devices.devices.reduce((total, row) => total + row.starts, 0), 0);
    assert.deepEqual(run.uploadCalls, []);
  });
}

for (const loser of ["deadline", "pagehide"] as const) {
  test(`非合作 getUserMedia 被 ${loser} 判死：closed 等到迟到的流全关才 settle`, async (context) => {
    const run = pendingStageHarness(context, "prepare", { ownerGeneration: 35 });
    await run.settleAt();

    if (loser === "deadline") run.harness.advance(PRE_START_BUDGET_MS);
    else run.capture.interrupt();
    await flushMicrotasks();

    let closedSettled = false;
    void run.capture.closed.then(() => { closedSettled = true; });
    await flushMicrotasks();
    // 流还在路上：closed 必须挂着，已知租约一次都不能放。
    assert.equal(closedSettled, false);
    assert.equal(run.held.releases, 0);
    for (const row of run.devices.tracks) assert.equal(row.stopped, 0);

    run.devices.openUserMedia();          // MediaStream 现在才交付
    await flushMicrotasks();
    for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
    assert.equal(run.held.releases, 1);   // 全关之后才放锁
    assert.equal(closedSettled, false);   // released 还没回来

    run.held.settleReleased();
    await flushMicrotasks();
    assert.equal(closedSettled, true);
    assert.equal(
      run.devices.devices.reduce((total, row) => total + row.starts, 0), 0);
    assert.deepEqual(run.uploadCalls, []);
  });
}

test("真实 onstart 之前 pagehide：before_start，迟到的 onstart 不复活这条捕获", async (context) => {
  const run = pendingStageHarness(context, "onstart", { ownerGeneration: 36 });
  await run.settleAt();
  const device = run.devices.devices[0];

  assert.equal(run.capture.interrupt(), "before_start");
  const startError = await run.capture.started.then(() => null, (e: unknown) => e);
  assert.equal((startError as AutopilotMediaError).errorCode, "device_runtime_failed");
  run.held.settleReleased();
  await run.capture.closed;
  for (const row of run.devices.tracks) assert.equal(row.stopped, 1);

  device.fireStart();                     // 设备晚了一步才回调
  await flushMicrotasks();
  assert.equal(run.capture.locallyStarted, false);
  assert.deepEqual(run.uploadCalls, []);
});

for (const stage of ["authorize1", "authorize2"] as const) {
  test(`${stage} 被截止时刻判死：稳定 device_command_timeout，迟到拒绝不改判`, async (context) => {
    const run = pendingStageHarness(context, stage, { ownerGeneration: 37 });
    await run.settleAt();
    run.harness.advance(PRE_START_BUDGET_MS);

    const error = await run.capture.started.then(() => null, (e: unknown) => e);
    assert.equal((error as AutopilotMediaError).errorCode, "device_command_timeout");

    // 判死之后底层请求才带着传输层错误回来：不得盖掉稳定的超时判定。
    const pending = stage === "authorize1" ? run.late.authorize1 : run.late.authorize2;
    pending.reject(new Error("请求已被中止"));
    await flushMicrotasks();
    const stopError = await run.capture.stopped.then(() => null, (e: unknown) => e);
    assert.equal((stopError as AutopilotMediaError).errorCode, "device_command_timeout");
    assert.deepEqual(run.uploadCalls, []);
  });
}

// ---------------- 生命周期中断：定时器清除端口失效仍须 fail closed ----------------

test("pre-start 定时器清不掉：两次 clear 全失败，interrupt 照样收口，句柄真的还挂着", async (context) => {
  // 已经拿到租约、悬在无资源的恢复快照上；pre-start 预算是第 1 个武装的定时器。
  const run = pendingStageHarness(context, "snapshot", {
    ownerGeneration: 40, failClearForArm: 1,
  });
  await run.settleAt();
  const preStart = run.harness.armedHandles[0];
  assert.equal(run.harness.isPending(preStart), true);

  // 端口坏了，但 interrupt() 必须同步返回判据、绝不外抛。
  let disposition: string | null = null;
  assert.doesNotThrow(() => { disposition = run.capture.interrupt(); });
  assert.equal(disposition, "before_start");

  const startError = await run.capture.started.then(() => null, (e: unknown) => e);
  assert.equal((startError as AutopilotMediaError).errorCode, "device_runtime_failed");
  const stopError = await run.capture.stopped.then(() => null, (e: unknown) => e);
  assert.equal((stopError as AutopilotMediaError).errorCode, "device_runtime_failed");

  // 下一段 bootstrap 一步都没开；设备侧一个字节都没产生。
  assert.deepEqual(run.harness.calls, STAGE_CALLS.snapshot);
  assert.equal(run.devices.userMediaCalls, 0);
  assert.equal(run.devices.devices.length, 0);        // 连 MediaRecorder 都没造
  assert.equal(
    run.devices.devices.reduce((total, row) => total + row.starts, 0), 0);
  for (const row of run.devices.tracks) assert.equal(row.stopped, 0);
  assert.deepEqual(run.uploadCalls, []);

  // 已知租约恰好释放一次；released 落地之后 closed 就 settle——绝不等那条永不
  // 返回的恢复快照。
  run.held.settleReleased();
  await run.capture.closed;
  assert.equal(run.held.releases, 1);

  // 同一个 handle 被清了两次：一次来自 interrupt()，一次来自 run() 的 pre-start
  // finally。两次都失败，所以它仍然真的挂在定时器表里。
  assert.equal(run.harness.clearAttemptsFor(preStart), 2);
  assert.equal(run.harness.isPending(preStart), true);
  assert.equal(run.harness.firesFor(preStart), 0);

  // 显式推进时钟：这条没清掉的定时器真的烧一次并离开表。
  run.harness.advance(PRE_START_BUDGET_MS);
  assert.equal(run.harness.firesFor(preStart), 1);
  assert.equal(run.harness.isPending(preStart), false);

  // 烧完之后什么都不能变：不重开、不上传、不重复拆设备、判据与错误码原样。
  await flushMicrotasks();
  assert.deepEqual(run.harness.calls, STAGE_CALLS.snapshot);
  assert.equal(run.devices.userMediaCalls, 0);
  assert.equal(run.devices.devices.length, 0);
  assert.deepEqual(run.uploadCalls, []);
  assert.equal(run.held.releases, 1);
  assert.equal(run.capture.interrupt(), "already_interrupted");
  assert.equal(
    ((await run.capture.stopped.then(() => null, (e: unknown) => e)) as AutopilotMediaError)
      .errorCode, "device_runtime_failed");
});

test("作答窗口定时器清不掉：after_start 收口，句柄恰好被清一次且真的还挂着", async (context) => {
  // pre-start 预算是第 1 个武装的定时器并会被正常清掉；作答窗口是第 2 个。
  const run = await captureAtRealOnstart(context, {
    ownerGeneration: 41, failClearForArm: 2,
  });
  run.device.fireStart();
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });

  const answerTimer = run.harness.armedHandles[1];
  assert.equal(run.harness.armedHandles.length, 2);
  assert.equal(run.harness.isPending(answerTimer), true);

  let disposition: string | null = null;
  assert.doesNotThrow(() => { disposition = run.capture.interrupt(); });
  assert.equal(disposition, "after_start");

  // 已经真实发生的开录不回滚；半段音频物理丢弃、一个字节都不上传。
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });
  const stopError = await run.capture.stopped.then(() => null, (e: unknown) => e);
  assert.equal((stopError as AutopilotMediaError).errorCode, "device_runtime_failed");

  // 重复的生命周期事件只拿到 already_interrupted，且不得重试定时器清理或拆设备。
  const attemptsBeforeRepeat = run.harness.clearAttemptsFor(answerTimer);
  assert.equal(run.capture.interrupt(), "already_interrupted");
  assert.equal(run.harness.clearAttemptsFor(answerTimer), attemptsBeforeRepeat);

  await run.capture.closed;
  assert.equal(run.device.stops, 1);
  for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
  assert.equal(run.held.releases, 1);
  assert.deepEqual(run.uploadCalls, []);

  // closed 之后仍然只有 interrupt() 那一次尝试。生产若"清成功才交出所有权"，
  // 外层 cleanup 会拿着 maxTimer 再清一次，这里就会变成 2。
  assert.equal(run.harness.clearAttemptsFor(answerTimer), 1);
  assert.equal(run.harness.isPending(answerTimer), true);
  assert.equal(run.harness.firesFor(answerTimer), 0);

  // 显式推进时钟：这条没清掉的作答窗口定时器真的烧一次并离开表。
  run.harness.advance(PRE_START_BUDGET_MS);
  assert.equal(run.harness.firesFor(answerTimer), 1);
  assert.equal(run.harness.isPending(answerTimer), false);

  // 它只会 signalStop，而 run() 早已按生命周期判死：不 stage、不上传、不重复
  // 拆设备、判据与错误码原样。
  await flushMicrotasks();
  assert.deepEqual(run.uploadCalls, []);
  assert.equal(run.device.stops, 1);
  for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
  assert.equal(run.held.releases, 1);
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });
  assert.equal(
    ((await run.capture.stopped.then(() => null, (e: unknown) => e)) as AutopilotMediaError)
      .errorCode, "device_runtime_failed");
});

test("作答窗口定时器端口同步抛错：started 仍以 exact MIME 结算，stopped 失败且零上传", async (context) => {
  // 第 1 次 setTimer 是 pre-start 预算，第 2 次才是作答窗口。
  const run = await captureAtRealOnstart(context, {
    ownerGeneration: 38, throwOnTimerCall: 2,
  });
  run.device.fireStart();

  // 真实开录已经发生：这条事实不因定时器端口失败而回滚。
  assert.deepEqual(await run.capture.started, { mime_type: "audio/webm" });
  const stopError = await run.capture.stopped.then(() => null, (e: unknown) => e);
  assert.equal((stopError as AutopilotMediaError).errorCode, "recording_runtime_failed");
  await run.capture.closed;

  assert.deepEqual(run.uploadCalls, []);
  assert.equal(run.device.stops, 1);
  for (const row of run.devices.tracks) assert.equal(row.stopped, 1);
  assert.equal(run.held.releases, 1);
});
