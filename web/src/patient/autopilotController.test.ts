import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  AUTOPILOT_CONTROLLER_AUTHORITY_TEST_ONLY,
  AutopilotAckPersistenceError,
  PatientAutopilotController,
  deviceCapabilityAllowsAutopilot,
  type AutopilotAckDelivery,
  type AutopilotAckPersistenceIdentity,
  type AutopilotRecordingExecutor,
  type AutopilotSpeechExecutor,
  type AutopilotTransport,
} from "./autopilotController.ts";
import {
  DurableAutopilotAckDelivery,
  type AutopilotAckStore,
} from "./autopilotAckDelivery.ts";
import {
  nextAutopilotAckCheckpoint,
  type AutopilotAckCheckpoint,
  type AutopilotAckEnvelope,
} from "./autopilotAckOutbox.ts";
import {
  buildAutopilotAck,
  type AutopilotAck,
  type AutopilotMimeType,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import { AutopilotMediaError } from "./autopilotMediaError.ts";
import {
  admitCaptureBeforeDeadline,
  answerWindowMs,
  armCaptureDeadline,
  assertCaptureDurationWithinCommandLimit,
} from "./autopilotCaptureWindow.ts";
import {
  AUTOPILOT_IDLE_TICK_MS,
  autopilotNextTickDelayMs,
  canOpenAutopilotMicrophone,
  type AutopilotRuntimeState,
} from "./autopilotRuntime.ts";
import {
  canProbePatientAutopilot,
  canRunPatientAutopilotMedia,
  canSignalBedsideActivation,
  resolvePatientAutopilotVisibleMode,
} from "./autopilotAdmission.ts";

const CHECKSUM = "b".repeat(64);

test("media admission requires tap, explicit TTS, connection, and server ownership", () => {
  const ready = {
    serverOwned: true,
    hasDurableDelivery: true,
    activated: true,
    ttsOn: true,
    connectionReady: true,
    sessionPaused: false,
    sessionTerminal: false,
  };
  assert.equal(canRunPatientAutopilotMedia(ready), true);
  for (const key of [
    "serverOwned", "hasDurableDelivery", "activated", "ttsOn", "connectionReady",
  ] as const) {
    assert.equal(canRunPatientAutopilotMedia({ ...ready, [key]: false }), false);
  }
  assert.equal(canRunPatientAutopilotMedia({ ...ready, sessionPaused: true }), false);
  assert.equal(canRunPatientAutopilotMedia({ ...ready, sessionTerminal: true }), false);
});

test("bedside activation signal needs an explicit tap, tts on and a ready connection", () => {
  const ready = {
    sessionId: "S-1",
    sessionMode: "task" as const,
    sessionTerminal: false,
    activatedForSessionId: "S-1",
    ttsOn: true,
    connectionReady: true,
    alreadySignaledSessionId: null,
  };
  assert.equal(canSignalBedsideActivation(ready), true);
  // 未点击激活(null)与上一场遗留的激活("S-0")都发不出本场信号。
  assert.equal(canSignalBedsideActivation({ ...ready, activatedForSessionId: null }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, activatedForSessionId: "S-0" }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, ttsOn: false }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, connectionReady: false }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, sessionMode: "rapport" as const }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, sessionMode: null }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, sessionTerminal: true }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, sessionId: null }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, sessionId: "" }), false);
  // 同一场次同一激活生命周期只发一次;只有换到新场次(新的激活周期)才会再发。
  assert.equal(canSignalBedsideActivation({ ...ready, alreadySignaledSessionId: "S-1" }), false);
  assert.equal(canSignalBedsideActivation({ ...ready, alreadySignaledSessionId: "S-0" }), true);
});

test("a fresh unpaired tab never guesses that a non-rapport session is legacy", () => {
  const baseline = {
    hasSession: true,
    hasExactCapability: false,
    probeKey: "",
    resolvedProbeKey: "",
    resolvedMode: "legacy" as const,
  };
  assert.equal(resolvePatientAutopilotVisibleMode(baseline), "probing");
  assert.equal(resolvePatientAutopilotVisibleMode({
    ...baseline,
    hasExactCapability: true,
    probeKey: "S-ONE\u0000capability-a",
  }), "probing");
  assert.equal(resolvePatientAutopilotVisibleMode({
    ...baseline,
    hasExactCapability: true,
    probeKey: "S-ONE\u0000capability-a",
    resolvedProbeKey: "S-ONE\u0000capability-a",
  }), "legacy");
  assert.equal(resolvePatientAutopilotVisibleMode({
    ...baseline,
    hasSession: false,
  }), "legacy");
});

test("autopilot polling requires a paired non-terminal session", () => {
  assert.equal(canProbePatientAutopilot({
    hasSession: true, hasExactCapability: true, sessionTerminal: false,
  }), true);
  assert.equal(canProbePatientAutopilot({
    hasSession: true, hasExactCapability: false, sessionTerminal: false,
  }), false);
  assert.equal(canProbePatientAutopilot({
    hasSession: true, hasExactCapability: true, sessionTerminal: true,
  }), false);
  assert.equal(canProbePatientAutopilot({
    hasSession: false, hasExactCapability: true, sessionTerminal: false,
  }), false);
});

function questionCommand(state: "pending" | "started" = "pending"): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-question-controller-0001",
    command_seq: 1,
    kind: "tts",
    state,
    command_revision: state === "pending" ? 0 : 1,
    control_generation: 3,
    runner_generation: 7,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      speech_key: "wk2.01.question",
      speech_text: "请说出图片中的物品。",
      purpose: "question",
    },
  };
}

function recordCommand(state: "pending" | "started" = "pending"): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-record-controller-0002",
    command_seq: 2,
    kind: "record",
    state,
    command_revision: state === "pending" ? 0 : 1,
    control_generation: 3,
    runner_generation: 7,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      raw_audio_id: "raw-controller-issued-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 12,
      contains_direct_identifier: false,
      presentation_speech_key: "controller.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

function transportQueue(
  responses: Array<unknown | null>,
  events: string[],
  acks: AutopilotAck[],
): AutopilotTransport {
  return {
    next: async () => {
      events.push("next");
      if (responses.length === 0) throw new Error("测试命令队列已空");
      return responses.shift() ?? null;
    },
    ack: async (_sessionId, _commandKey, ack) => {
      events.push(`ack:${ack.ack_type}:r${ack.command_revision}:s${ack.device_event_seq}`);
      acks.push(ack);
      return { accepted: true };
    },
  };
}

function fixedAckKey(
  _command: NextCommandProjection,
  ackType: AutopilotAck["ack_type"],
  eventSeq: number,
): string {
  return `test:${ackType}:${eventSeq}:00000001`;
}

test("only an exact unexpired active-session capability admits autopilot", () => {
  const now = Date.parse("2026-07-18T10:00:00Z");
  const capability: DeviceCapabilityRecord = {
    capability: "x".repeat(43),
    sessionId: "S-ONE",
    expiresAt: "2026-07-18T11:00:00Z",
  };
  assert.equal(deviceCapabilityAllowsAutopilot(capability, "S-ONE", now), true);
  assert.equal(deviceCapabilityAllowsAutopilot(capability, "S-TWO", now), false);
  assert.equal(deviceCapabilityAllowsAutopilot(
    { ...capability, expiresAt: "2026-07-18T09:59:59Z" }, "S-ONE", now,
  ), false);
  assert.equal(deviceCapabilityAllowsAutopilot(null, "S-ONE", now), false);
});

test("question playback ACKs real start then server revision then real end; it never opens the mic", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let recordStarts = 0;
  const speech: AutopilotSpeechExecutor = {
    start: (command) => {
      events.push(`speech:${command.payload.speech_text}`);
      return {
        started: Promise.resolve({ media_duration_ms: 900 }),
        ended: Promise.resolve({ media_duration_ms: 900 }),
        closed: Promise.resolve(),
        cancel: () => { events.push("speech:cancel"); },
      };
    },
  };
  const recording: AutopilotRecordingExecutor = {
    start: () => {
      recordStarts += 1;
      throw new Error("本轮不应请求麦克风");
    },
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: transportQueue(
      [questionCommand(), questionCommand("started"), recordCommand()], events, acks,
    ),
    speech,
    recording,
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();

  assert.deepEqual(events, [
    "next",
    "speech:请说出图片中的物品。",
    "ack:tts_started:r0:s1",
    "next",
    "ack:tts_ended:r1:s2",
    "next",
  ]);
  assert.equal(recordStarts, 0);
  assert.equal(state.phase, "record_ready");
  assert.equal(canOpenAutopilotMicrophone(state), true);
  assert.deepEqual(acks.map((ack) => ack.ack_type), ["tts_started", "tts_ended"]);
});

test("提问播完、tts_ended 已签名之后，收麦不再多等一个空闲节拍", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  const speech: AutopilotSpeechExecutor = {
    start: () => ({
      started: Promise.resolve({ media_duration_ms: 900 }),
      // 真实 ended 仍然先于开麦：这个 promise 落地之前 runSpeech 不会返回。
      ended: Promise.resolve({ media_duration_ms: 900 }),
      closed: Promise.resolve(),
      cancel: () => { events.push("speech:cancel"); },
    }),
  };
  const recording: AutopilotRecordingExecutor = {
    start: (command) => {
      events.push("record:getUserMedia");
      return {
        started: Promise.resolve({ mime_type: "audio/webm", channels: 1 }),
        stopped: Promise.resolve({
          stop_reason: "max_duration",
          raw_audio_id: command.payload.raw_audio_id,
          receipt_server_seq: 44,
          checksum: CHECKSUM,
          byte_count: 4_096,
          duration_seconds: 14.02,
        }),
        closed: Promise.resolve(),
        cancel: () => { events.push("record:cancel"); },
      };
    },
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: transportQueue(
      [
        questionCommand(), questionCommand("started"), recordCommand(),
        recordCommand(), recordCommand("started"), null,
      ],
      events,
      acks,
    ),
    speech,
    recording,
    idempotencyKey: fixedAckKey,
  });

  // 复刻 usePatientAutopilot 的 runner tick：每轮 pollOnce 之后按运行时状态
  // 决定下一轮的间隔。
  const scheduledDelays: number[] = [];
  for (let round = 0; round < 2; round += 1) {
    const runtime = await controller.pollOnce();
    if (runtime.phase === "paused" || runtime.phase === "scope_completed") break;
    scheduledDelays.push(autopilotNextTickDelayMs(runtime));
  }

  // 第一轮结束时服务器已经签发本轮录音命令 → 立即进入下一轮，0ms。
  // 第二轮已交回服务器等待下一条命令 → 回到有界空闲节拍。
  assert.deepEqual(scheduledDelays, [0, AUTOPILOT_IDLE_TICK_MS]);
  assert.equal(AUTOPILOT_IDLE_TICK_MS, 900);
  // 开麦仍然排在真实 ended 与持久化 tts_ended 之后，一步都没提前。
  assert.deepEqual(events, [
    "next",
    "ack:tts_started:r0:s1",
    "next",
    "ack:tts_ended:r1:s2",
    "next",
    "next",
    "record:getUserMedia",
    "ack:record_started:r0:s3",
    "next",
    "ack:record_stopped:r1:s4",
    "next",
  ]);
  assert.equal(events.indexOf("record:getUserMedia") > events.indexOf("ack:tts_ended:r1:s2"), true);
  assert.deepEqual(acks.map((ack) => ack.ack_type),
    ["tts_started", "tts_ended", "record_started", "record_stopped"]);
});

test("the recorder is requested only after an exact record command and returns the server receipt tuple", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let speechStarts = 0;
  let recordedRawId: string | null = null;
  const speech: AutopilotSpeechExecutor = {
    start: () => {
      speechStarts += 1;
      throw new Error("录音命令不应启动 TTS");
    },
  };
  const recording: AutopilotRecordingExecutor = {
    start: (command) => {
      recordedRawId = command.payload.raw_audio_id;
      events.push(`record:${recordedRawId}`);
      return {
        started: Promise.resolve({ mime_type: "audio/webm", channels: 1 }),
        stopped: Promise.resolve({
          stop_reason: "silence",
          raw_audio_id: command.payload.raw_audio_id,
          receipt_server_seq: 31,
          checksum: CHECKSUM,
          byte_count: 8_192,
          duration_seconds: 3.25,
        }),
        closed: Promise.resolve(),
        cancel: () => { events.push("record:cancel"); },
      };
    },
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: transportQueue(
      [recordCommand(), recordCommand("started"), null], events, acks,
    ),
    speech,
    recording,
    initialDeviceEventSeq: 2,
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();

  assert.equal(speechStarts, 0);
  assert.equal(recordedRawId, "raw-controller-issued-0001");
  assert.deepEqual(events, [
    "next",
    "record:raw-controller-issued-0001",
    "ack:record_started:r0:s3",
    "next",
    "ack:record_stopped:r1:s4",
    "next",
  ]);
  assert.equal(state.phase, "waiting_server_after_record");
  const stopped = acks[1];
  assert.equal(stopped?.ack_type, "record_stopped");
  if (stopped?.ack_type === "record_stopped") {
    assert.equal(stopped.receipt_server_seq, 31);
    assert.equal(stopped.raw_audio_id, "raw-controller-issued-0001");
    assert.equal(stopped.checksum, CHECKSUM);
  }
});

/**
 * 真控制器 + 真 admitCaptureBeforeDeadline + 假麦克风/假时钟。
 * 用来证明"后置授权卡过截止时刻"这条路径的编排结果，不是算术。
 */
function deadlineRaceHarness(
  authorization: Promise<"authorized">,
  options: { durationSeconds?: number } = {},
) {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  const uploaded: string[] = [];
  const pendingTimers = new Map<number, () => void>();
  let nextHandle = 1;
  let nowMs = 5_000;
  let commandLimitSeconds = 0;
  const recordingStartedAt = nowMs - 20;   // 真实录音起点到武装之间过了 20ms

  const recording: AutopilotRecordingExecutor = {
    start: (command) => {
      events.push("record:getUserMedia");
      commandLimitSeconds = command.payload.max_duration_seconds;
      let resolveStarted!: (value: { mime_type: "audio/webm" }) => void;
      let rejectStarted!: (error: unknown) => void;
      let resolveStopped!: (value: never) => void;
      let rejectStopped!: (error: unknown) => void;
      let resolveClosed!: () => void;
      const started = new Promise<{ mime_type: "audio/webm" }>((resolve, reject) => {
        resolveStarted = resolve;
        rejectStarted = reject;
      });
      const stopped = new Promise<never>((resolve, reject) => {
        resolveStopped = resolve as (value: never) => void;
        rejectStopped = reject;
      });
      const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });
      void (async () => {
        try {
          const deadline = armCaptureDeadline({
            deadlineAt: recordingStartedAt
              + answerWindowMs(command.payload.max_duration_seconds),
            now: () => nowMs,
            setTimer: (callback) => {
              const handle = nextHandle;
              nextHandle += 1;
              pendingTimers.set(handle, callback);
              return handle;
            },
            clearTimer: (handle) => { pendingTimers.delete(handle); },
            failClosed: () => { events.push("record:mic-physically-stopped"); },
          });
          try {
            await admitCaptureBeforeDeadline(authorization, deadline);
          } finally {
            deadline.clear();
          }
          resolveStarted({ mime_type: "audio/webm" });
          // 生产顺序：Recorder.stop 读数 → 上限闸 → 才 stage/上传。
          const durationSeconds = options.durationSeconds ?? 10.98;
          assertCaptureDurationWithinCommandLimit(
            durationSeconds, command.payload.max_duration_seconds);
          uploaded.push(command.payload.raw_audio_id);
          resolveStopped({
            stop_reason: "max_duration",
            raw_audio_id: command.payload.raw_audio_id,
            receipt_server_seq: 77,
            checksum: CHECKSUM,
            byte_count: 2_048,
            duration_seconds: durationSeconds,
          } as never);
        } catch (error) {
          rejectStarted(error);
          rejectStopped(error);
        } finally {
          resolveClosed();
        }
      })();
      return {
        started,
        stopped: stopped as unknown as AutopilotRecordingCaptureStopped,
        closed,
        cancel: () => { events.push("record:cancel"); },
      };
    },
  };

  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: transportQueue([recordCommand(), recordCommand("started"), null], events, acks),
    speech: { start: () => { throw new Error("录音命令不应启动 TTS"); } },
    recording,
    initialDeviceEventSeq: 2,
    idempotencyKey: fixedAckKey,
  });

  return {
    events,
    acks,
    uploaded,
    controller,
    get pendingTimerCount() { return pendingTimers.size; },
    fireTimers() {
      // 定时器烧掉的同时把单调时钟推到截止时刻，与生产一致。
      nowMs = recordingStartedAt + answerWindowMs(commandLimitSeconds);
      for (const [handle, callback] of [...pendingTimers]) {
        pendingTimers.delete(handle);
        callback();
      }
    },
  };
}

type AutopilotRecordingCaptureStopped = AutopilotRecordingExecutor extends never ? never
  : ReturnType<AutopilotRecordingExecutor["start"]>["stopped"];

test("后置授权卡过截止时刻：按截止时刻关麦，不上传、不产生成功 record ACK", async () => {
  const harness = deadlineRaceHarness(new Promise<"authorized">(() => {}));

  const polled = harness.controller.pollOnce();
  for (let turn = 0; turn < 50 && harness.pendingTimerCount === 0; turn += 1) {
    await Promise.resolve();
  }
  assert.equal(harness.pendingTimerCount, 1);
  harness.fireTimers();
  const state = await polled;

  // 麦克风被物理关掉，而且是在任何持久化之前。
  assert.ok(harness.events.includes("record:mic-physically-stopped"));
  assert.deepEqual(harness.uploaded, []);
  // 只有一个 record_failed，没有 record_started / record_stopped。
  assert.deepEqual(harness.acks.map((ack) => ack.ack_type), ["record_failed"]);
  const failed = harness.acks[0];
  assert.equal(failed?.ack_type, "record_failed");
  if (failed?.ack_type === "record_failed") {
    assert.equal(failed.error_code, "device_command_timeout");
  }
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "record_failed");
});

test("后置授权按时返回：照常进入可持久 capture 并回执真实时长", async () => {
  const harness = deadlineRaceHarness(Promise.resolve("authorized"));

  const state = await harness.controller.pollOnce();

  assert.equal(harness.pendingTimerCount, 0);            // 准入定时器已撤
  assert.ok(!harness.events.includes("record:mic-physically-stopped"));
  assert.deepEqual(harness.uploaded, ["raw-controller-issued-0001"]);
  assert.deepEqual(harness.acks.map((ack) => ack.ack_type),
    ["record_started", "record_stopped"]);
  const stopped = harness.acks[1];
  if (stopped?.ack_type === "record_stopped") {
    // 真实时长照实上报，落在这条命令的服务器上限（12 秒）之内。
    assert.equal(stopped.duration_seconds, 10.98);
    assert.ok(stopped.duration_seconds <= 12);
    assert.equal(stopped.stop_reason, "max_duration");
  }
  assert.equal(state.phase, "waiting_server_after_record");
});

test("真实时长顶穿服务器上限时不进持久化，照实变成 record_failed", async () => {
  // recordCommand 的上限是 12 秒；停麦读到 12.6 秒。
  const harness = deadlineRaceHarness(Promise.resolve("authorized"),
    { durationSeconds: 12.6 });

  const state = await harness.controller.pollOnce();

  assert.deepEqual(harness.uploaded, []);                // 一个字节都没 stage/上传
  assert.deepEqual(harness.acks.map((ack) => ack.ack_type),
    ["record_started", "record_failed"]);
  const failed = harness.acks[1];
  if (failed?.ack_type === "record_failed") {
    assert.equal(failed.error_code, "recording_runtime_failed");
    // record_failed 必须回执 started 那一版本，否则服务器认不出这条命令。
    assert.equal(failed.command_revision, 1);
  }
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "record_failed");
});

test("autopilot emits no media start or ACK before the exact current image gate resolves", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let releaseImage!: () => void;
  const imageReady = new Promise<void>((resolve) => { releaseImage = resolve; });
  let speechStarts = 0;
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: transportQueue(
      [questionCommand(), questionCommand("started"), null],
      events,
      acks,
    ),
    speech: {
      start: () => {
        speechStarts += 1;
        return {
          started: Promise.resolve({}),
          ended: Promise.resolve({}),
          closed: Promise.resolve(),
          cancel: () => {},
        };
      },
    },
    recording: { start: () => { throw new Error("不应开麦"); } },
    waitForPresentation: async (command, signal) => {
      assert.equal(command.command_key, "cmd-question-controller-0001");
      assert.equal(signal.aborted, false);
      await imageReady;
    },
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(speechStarts, 0);
  assert.equal(acks.length, 0);
  assert.deepEqual(events, ["next"]);

  releaseImage();
  await polling;
  assert.equal(speechStarts, 1);
  assert.deepEqual(acks.map((ack) => ack.ack_type), ["tts_started", "tts_ended"]);
});

test("image decode gate failure pauses autopilot without opening the microphone or ACKing", async () => {
  let recordStarts = 0;
  let ackCount = 0;
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { ackCount += 1; return {}; },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => {
        recordStarts += 1;
        throw new Error("图片失败后不应开麦");
      },
    },
    waitForPresentation: async () => { throw new Error("图片解码失败"); },
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();
  assert.equal(recordStarts, 0);
  assert.equal(ackCount, 0);
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "technical_failure");
});

test("null or an in-flight refresh command never starts either media executor", async () => {
  for (const response of [null, questionCommand("started"), recordCommand("started")]) {
    let speechStarts = 0;
    let recordStarts = 0;
    const controller = new PatientAutopilotController({
      sessionId: "S-ONE",
      transport: { next: async () => response, ack: async () => ({}) },
      speech: {
        start: () => {
          speechStarts += 1;
          throw new Error("不应启动语音");
        },
      },
      recording: {
        start: () => {
          recordStarts += 1;
          throw new Error("不应请求麦克风");
        },
      },
      idempotencyKey: fixedAckKey,
    });
    const state = await controller.pollOnce();
    assert.equal(speechStarts, 0);
    assert.equal(recordStarts, 0);
    assert.equal(state.phase, response === null ? "waiting_command" : "paused");
  }
});

test("concurrent polls share one media owner", async () => {
  let nextCalls = 0;
  let resolveNext: ((value: unknown | null) => void) | null = null;
  const nextPromise = new Promise<unknown | null>((resolve) => { resolveNext = resolve; });
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: {
      next: async () => { nextCalls += 1; return nextPromise; },
      ack: async () => ({}),
    },
    speech: { start: () => { throw new Error("不应启动"); } },
    recording: { start: () => { throw new Error("不应启动"); } },
    idempotencyKey: fixedAckKey,
  });
  const first = controller.pollOnce();
  const second = controller.pollOnce();
  assert.equal(first, second);
  assert.equal(nextCalls, 1);
  resolveNext?.(null);
  await first;
});

test("a real playback failure is ACKed and remains distinguishable from a transport failure", async () => {
  const acks: AutopilotAck[] = [];
  let cancels = 0;
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: {
      next: async () => questionCommand(),
      ack: async (_sessionId, _commandKey, ack) => { acks.push(ack); return {}; },
    },
    speech: {
      start: () => ({
        started: Promise.reject(new Error("本机无法播放")),
        ended: Promise.resolve({}),
        closed: Promise.resolve(),
        cancel: () => { cancels += 1; },
      }),
    },
    recording: { start: () => { throw new Error("不应开麦"); } },
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "tts_failed");
  assert.equal(acks.length, 1);
  assert.equal(acks[0]?.ack_type, "tts_failed");
  assert.equal(cancels, 1);
});

test("stopAndWait never treats a cancelled pending microphone as physically closed", async () => {
  let rejectStarted!: (error: unknown) => void;
  const started = new Promise<{ mime_type: "audio/webm" }>((_resolve, reject) => {
    rejectStarted = reject;
  });
  let rejectStopped!: (error: unknown) => void;
  const stopped = new Promise<never>((_resolve, reject) => { rejectStopped = reject; });
  let resolveClosed!: () => void;
  const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });
  let captureCreated!: () => void;
  const created = new Promise<void>((resolve) => { captureCreated = resolve; });
  let cancels = 0;
  let ackCount = 0;
  const controller = new PatientAutopilotController({
    sessionId: "S-ONE",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { ackCount += 1; return {}; },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => {
        captureCreated();
        return {
          started,
          stopped,
          closed,
          cancel: () => {
            cancels += 1;
            const error = new DOMException("权限请求已取消", "AbortError");
            rejectStarted(error);
            rejectStopped(error);
          },
        };
      },
    },
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  await created;
  let shutdownSettled = false;
  const shutdown = controller.stopAndWait().then(() => { shutdownSettled = true; });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(shutdownSettled, false);
  assert.equal(ackCount, 0);
  assert.ok(cancels >= 1);

  resolveClosed();
  await shutdown;
  await polling;
  assert.equal(shutdownSettled, true);
  assert.equal(ackCount, 0);
});

// ---------------- initialRuntime seed ----------------

function inertControllerDeps() {
  return {
    transport: { next: async () => null, ack: async () => ({}) } as AutopilotTransport,
    speech: { start: () => { throw new Error("不应播放"); } } as AutopilotSpeechExecutor,
    recording: { start: () => { throw new Error("不应开麦"); } } as AutopilotRecordingExecutor,
  };
}

test("initialRuntime：完整 paused runtime 原样保留，不被 restoreAutopilotRuntime 从 command 重建", () => {
  const command = questionCommand("pending");
  const pausedRuntime: AutopilotRuntimeState = {
    phase: "paused",
    command,
    last_device_event_seq: 5,
    last_ack: null,
    pause_reason: "tts_failed",
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-INIT-RUNTIME",
    ...inertControllerDeps(),
    initialDeviceEventSeq: 5,
    initialRuntime: pausedRuntime,
  });
  // 只传 initialCommand 会让构造器自己用 restoreAutopilotRuntime(command) 重建，
  // 那样 pending 命令会变成 tts_ready，而不是这里要的 paused。
  assert.equal(controller.state.phase, "paused");
  assert.equal(controller.state.pause_reason, "tts_failed");
  assert.deepEqual(controller.state, pausedRuntime);
});

test("initialRuntime 与 initialCommand 同时提供时拒绝构造", () => {
  const runtime: AutopilotRuntimeState = {
    phase: "waiting_command", command: null, last_device_event_seq: 0, last_ack: null, pause_reason: null,
  };
  assert.throws(() => new PatientAutopilotController({
    sessionId: "S-INIT-RUNTIME",
    ...inertControllerDeps(),
    initialRuntime: runtime,
    initialCommand: null,
  }), /不得同时提供/);
});

test("initialRuntime.last_device_event_seq 与控制器序号起点不一致时拒绝构造", () => {
  const runtime: AutopilotRuntimeState = {
    phase: "waiting_command", command: null, last_device_event_seq: 3, last_ack: null, pause_reason: null,
  };
  assert.throws(() => new PatientAutopilotController({
    sessionId: "S-INIT-RUNTIME",
    ...inertControllerDeps(),
    initialDeviceEventSeq: 5,
    initialRuntime: runtime,
  }), /序号起点不一致/);
});

test("initialRuntime 与 ackDelivery.initialDeviceEventSeq 不一致时同样拒绝构造", () => {
  const runtime: AutopilotRuntimeState = {
    phase: "waiting_command", command: null, last_device_event_seq: 3, last_ack: null, pause_reason: null,
  };
  assert.throws(() => new PatientAutopilotController({
    sessionId: "S-INIT-RUNTIME",
    ...inertControllerDeps(),
    ackDelivery: { initialDeviceEventSeq: 5, send: async () => { throw new Error("不应发送"); } },
    initialRuntime: runtime,
  }), /序号起点不一致/);
});

test("旧 API：只传 initialCommand 时行为不变（向后兼容）", () => {
  const controller = new PatientAutopilotController({
    sessionId: "S-INIT-RUNTIME",
    ...inertControllerDeps(),
    initialCommand: questionCommand("pending"),
  });
  assert.equal(controller.state.phase, "tts_ready");
  assert.equal(controller.state.pause_reason, null);
});

test("stopRecordingNow：服务器还没进 recording，点击照样命中当前 capture", () => {
  const requestStopCalls: Array<"user_done" | undefined> = [];
  const capture = {
    started: Promise.resolve({ mime_type: "audio/webm" as const }),
    stopped: new Promise<never>(() => {}),
    closed: new Promise<void>(() => {}),
    locallyStarted: true,
    requestStop: (reason?: "user_done") => { requestStopCalls.push(reason); },
    cancel: () => { throw new Error("stopRecordingNow 不应触发 cancel"); },
  };
  // 关键：runtime 停在 waiting_recording——record_started 的 ACK 还在路上。
  // 旧实现在这里直接 return，老人点了按钮什么都不会发生。
  const pendingAckRuntime: AutopilotRuntimeState = {
    phase: "waiting_recording",
    command: recordCommand("started"),
    last_device_event_seq: 3,
    last_ack: null,
    pause_reason: null,
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-STOP-NOW",
    ...inertControllerDeps(),
    initialDeviceEventSeq: 3,
    initialRuntime: pendingAckRuntime,
  });
  // 直接注入 activeRecording：这是控制器在 capture.started 落地之后才会设置的
  // 私有字段，也就是"本地已真实开录"这个事实本身。这里只钉住 stopRecordingNow
  // 的判定边界，不重放 getUserMedia → audioSaved → record_stopped 整条链
  // （该链在 BrowserAutopilotCapture，未被这份测试覆盖，见下方 gap 说明）。
  (controller as unknown as { activeRecording: unknown }).activeRecording = capture;

  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, ["user_done"]);
  assert.equal(controller.state.phase, "waiting_recording");

  // 重复点击/重复 render：controller 这一层逐次原样转发，不去重——
  // “最终只生效一次”的幂等保证在 BrowserAutopilotCapture 的 stopLatch 里
  // （先到者赢，之后同一实例上谁再 signalStop 都是纯 no-op），不是这个方法的
  // 契约，也未被这份测试直接验证（见下方 gap）。
  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, ["user_done", "user_done"]);
});

test("stopRecordingNow：本地尚未真实开录时点击无效，不向 capture 转发", () => {
  const requestStopCalls: string[] = [];
  const controller = new PatientAutopilotController({
    sessionId: "S-STOP-NOW-EARLY",
    ...inertControllerDeps(),
  });
  (controller as unknown as { activeRecording: unknown }).activeRecording = {
    started: new Promise(() => {}),
    stopped: new Promise(() => {}),
    closed: new Promise(() => {}),
    locallyStarted: false,
    requestStop: () => { requestStopCalls.push("user_done"); },
    cancel: () => {},
  };
  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, []);
});

test("stopRecordingNow：没有活动 capture 时静默无效，不抛异常", () => {
  const controller = new PatientAutopilotController({
    sessionId: "S-STOP-NOW-IDLE",
    ...inertControllerDeps(),
  });
  assert.equal(controller.state.phase, "waiting_command");
  controller.stopRecordingNow();
  assert.equal(controller.state.phase, "waiting_command");
});

// ---------------- 生命周期中断 ----------------

test("生命周期中断走 interrupt，绝不走 cancel；真实开录后由失败分支发 record_failed", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let interruptCalls = 0;
  let rejectStopped!: (error: unknown) => void;
  let resolveClosed!: () => void;
  const capture = {
    started: Promise.resolve({ mime_type: "audio/webm" as const }),
    stopped: new Promise<never>((_resolve, reject) => { rejectStopped = reject; }),
    closed: new Promise<void>((resolve) => { resolveClosed = resolve; }),
    locallyStarted: true,
    requestStop: () => { events.push("record:requestStop"); },
    interrupt: () => {
      interruptCalls += 1;
      // 生产实现在这一步已经同步物理关麦并丢弃了半段音频。
      events.push("record:mic-physically-stopped");
      rejectStopped(new AutopilotMediaError(
        "device_runtime_failed", "页面已离开前台，本次录音已被丢弃"));
      resolveClosed();
      return "after_start" as const;
    },
    cancel: () => { events.push("record:cancel"); },
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-LIFECYCLE",
    transport: transportQueue(
      [recordCommand(), recordCommand("started"), null], events, acks),
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => capture },
    initialDeviceEventSeq: 2,
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  // 等 record_started 的 ACK 发出去、controller 记下 activeRecording。
  for (let turn = 0; turn < 50 && acks.length === 0; turn += 1) await Promise.resolve();
  const shutdown = controller.interruptRecordingForLifecycleAndWait();
  await polling;
  await shutdown;

  assert.equal(interruptCalls, 1);
  assert.equal(events.includes("record:cancel"), false);   // 绝不复用成功链
  assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started", "record_failed"]);
  const failed = acks[1];
  if (failed?.ack_type === "record_failed") {
    assert.equal(failed.error_code, "device_runtime_failed");
  }
});

test("重复的生命周期事件复用同一条 shutdown，不并起第二条", async () => {
  let interruptCalls = 0;
  const controller = new PatientAutopilotController({
    sessionId: "S-LIFECYCLE-REPEAT",
    ...inertControllerDeps(),
  });
  (controller as unknown as { activeRecording: unknown }).activeRecording = {
    started: Promise.resolve({ mime_type: "audio/webm" as const }),
    stopped: Promise.reject(new Error("已丢弃")).catch(() => undefined),
    closed: Promise.resolve(),
    locallyStarted: true,
    interrupt: () => { interruptCalls += 1; return "after_start" as const; },
    cancel: () => { throw new Error("生命周期不得走 cancel"); },
  };

  const first = controller.interruptRecordingForLifecycleAndWait();
  const second = controller.interruptRecordingForLifecycleAndWait();
  assert.equal(first, second);
  await first;
  assert.equal(interruptCalls, 1);
});

// ---------------- 持久回执权威 / 无刷新 / 生命周期矩阵 ----------------

/**
 * 权威必须由**实际返回的那条 ACK**动态绑定。
 *
 * 三件事必须分清、绝不能拿同一个硬编码对象同时假装：服务器返回的 ACK 身份
 * （幂等键 / MIME）、回执权威（命令键 / 类型 / revision / 投影），以及本机观察到的
 * 开录 MIME。`drift` 只用来在负例里精确破坏其中一项。
 */
interface AuthorityDrift {
  ackType?: AutopilotAck["ack_type"];
  ackIdempotencyKey?: string;
  commandKey?: string;
  commandRevision?: number;
  omitRevision?: boolean;
  projected?: NextCommandProjection | null;
  /** 服务器返回的 record_started ACK 自称的编码，用来做 MIME 失配负例。 */
  returnedMimeType?: string;
  /** 服务器完全没有留下回执权威。 */
  suppressAuthority?: boolean;
}

type ReceiptAuthority = NonNullable<
  NonNullable<
    ConstructorParameters<typeof PatientAutopilotController>[0]["ackDelivery"]
  >["lastReceiptAuthority"]
>;

function durableAuthorityDelivery(
  events: string[],
  acks: AutopilotAck[],
  drift: AuthorityDrift = {},
) {
  let authority: ReceiptAuthority | null = null;
  return {
    get authority() { return authority; },
    delivery: {
      initialDeviceEventSeq: 2,
      get lastReceiptAuthority() { return authority; },
      send: async (
        command: NextCommandProjection,
        seq: number,
        facts: Parameters<typeof buildAutopilotAck>[3],
      ) => {
        events.push(`delivery:${facts.ack_type}:r${command.command_revision}`);
        const returned = facts.ack_type === "record_started" && drift.returnedMimeType
          ? { ...facts, mime_type: drift.returnedMimeType } as unknown as typeof facts
          : facts;
        const ack = buildAutopilotAck(
          command, seq, `k:${facts.ack_type}:${seq}`, returned);
        acks.push(ack);
        if (facts.ack_type === "record_started" && !drift.suppressAuthority) {
          // 服务器随这条 ACK 一起返回的权威：投影 revision 与收据 revision 是
          // 同一个数，幂等键就是刚刚真的发出去的那条 ACK 的键。
          const projected = drift.projected !== undefined
            ? drift.projected : recordCommand("started");
          authority = {
            commandKey: drift.commandKey ?? command.command_key,
            ackType: drift.ackType ?? "record_started",
            ackIdempotencyKey: drift.ackIdempotencyKey ?? ack.idempotency_key,
            commandRevision: drift.omitRevision
              ? undefined
              : drift.commandRevision ?? projected?.command_revision,
            command: projected,
          };
        }
        return ack;
      },
    },
  };
}

/** 一条已经真实开录、随后成功持久化的 capture 替身。 */
function settledCapture(events: string[], options: {
  stopped?: Promise<never>;
  closed?: Promise<void>;
} = {}) {
  return {
    started: Promise.resolve({ mime_type: "audio/webm" as const }),
    stopped: options.stopped ?? Promise.resolve({
      stop_reason: "user_done" as const,
      raw_audio_id: "raw-controller-issued-0001",
      receipt_server_seq: 12,
      checksum: CHECKSUM,
      byte_count: 1_024,
      duration_seconds: 2.5,
    }),
    closed: options.closed ?? Promise.resolve(),
    locallyStarted: true,
    cancel: () => { events.push("record:cancel"); },
  };
}

test("record_started 已确认但 /next 未回来时，失败 ACK 用服务器权威 command", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let refreshCalls = 0;
  const authority = durableAuthorityDelivery(events, acks);
  const controller = new PatientAutopilotController({
    sessionId: "S-AUTHORITY",
    transport: {
      next: async () => {
        refreshCalls += 1;
        if (refreshCalls === 1) return recordCommand();
        // 任何"本该发生"的刷新在这里都会炸。持久 delivery 路径根本不该调它。
        throw new Error("刷新失败");
      },
      ack: async () => { throw new Error("持久 delivery 路径不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => settledCapture(events) },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();

  // /next 只在开头取一次待办命令；started 确认之后与终态之后都不再刷新。
  assert.equal(refreshCalls, 1);
  // record_stopped 绑在服务器自己签发的 started revision 上，客户端一次都没有
  // 自行递增/拼装 revision。
  assert.deepEqual(events.filter((row) => row.startsWith("delivery:")), [
    "delivery:record_started:r0",
    "delivery:record_stopped:r1",
  ]);
  // 权威是动态绑上去的：幂等键就是那条实际返回的 record_started ACK 的键。
  assert.equal(authority.authority?.ackIdempotencyKey, acks[0]?.idempotency_key);
  assert.equal(authority.authority?.commandRevision, 1);
  assert.equal(events.includes("record:cancel"), false);
  assert.equal(state.phase, "waiting_server_after_record");
  assert.equal(state.last_device_event_seq, 4);
});

// §7-9 九条权威负例：任何一项证明不成立都只能安全暂停，零第二/终态 ACK。
for (const negative of [
  { name: "authority 整体为 null", drift: { suppressAuthority: true } as AuthorityDrift },
  { name: "exact 重放未投影命令", drift: { projected: null } },
  { name: "ACK 类型不是 record_started", drift: { ackType: "record_stopped" } },
  { name: "ACK 幂等键对不上", drift: { ackIdempotencyKey: "k:forged:9" } },
  { name: "命令键对不上", drift: { commandKey: "cmd-record-controller-9999" } },
  { name: "缺 revision", drift: { omitRevision: true } },
  { name: "revision 与投影不一致", drift: { commandRevision: 7 } },
  {
    name: "投影 state 不是 started",
    drift: { projected: recordCommand("pending") },
  },
  {
    name: "投影 kind 不是 record",
    drift: { projected: questionCommand("started") },
  },
  { name: "返回 ACK 的 MIME 与本机观察不符", drift: { returnedMimeType: "audio/mp4" } },
] as Array<{ name: string; drift: AuthorityDrift }>) {
  test(`权威不足（${negative.name}）：安全暂停，零终态 ACK、零本地 +1`, async () => {
    const events: string[] = [];
    const acks: AutopilotAck[] = [];
    const authority = durableAuthorityDelivery(events, acks, negative.drift);
    let interrupts = 0;
    const controller = new PatientAutopilotController({
      sessionId: "S-AUTHORITY-NEG",
      transport: {
        next: async () => recordCommand(),
        ack: async () => { throw new Error("不应直连 transport.ack"); },
      },
      speech: { start: () => { throw new Error("不应播放"); } },
      recording: {
        start: () => ({
          ...settledCapture(events),
          interrupt: () => { interrupts += 1; return "after_start" as const; },
        }),
      },
      ackDelivery: authority.delivery,
      idempotencyKey: fixedAckKey,
    });

    const state = await controller.pollOnce();

    assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started"]);
    assert.deepEqual(events.filter((row) => row.startsWith("delivery:")),
      ["delivery:record_started:r0"]);
    // 权威不足走的是物理丢弃，不是 cancel（真实开录后 cancel 会上传半段音频）。
    assert.equal(events.includes("record:cancel"), false);
    assert.equal(interrupts, 1);
    assert.equal(state.phase, "paused");
    // 只有那条已确认的 record_started 推进过序号，没有任何本地合成的 +1。
    assert.equal(state.last_device_event_seq, 3);
  });
}

test("不可变字段漂移：reducer 落 protocol_violation，零终态 ACK", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  // command_key 相同（所以走 acceptSameCommand），但 turn_ref 这个不可变载荷漂移。
  const drifted = recordCommand("started");
  const authority = durableAuthorityDelivery(events, acks, {
    projected: {
      ...drifted,
      payload: { ...drifted.payload, turn_ref: "itm-0001#9" },
    } as NextCommandProjection,
  });
  const controller = new PatientAutopilotController({
    sessionId: "S-AUTHORITY-DRIFT",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => ({ ...settledCapture(events), interrupt: () => "after_start" as const }),
    },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();

  assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started"]);
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "protocol_violation");
  assert.equal(events.includes("record:cancel"), false);
});

test("before_start 生命周期：零 cancel、零 ACK，服务器命令仍留给重新激活的页面", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  let resolveClosed!: () => void;
  const closed = new Promise<void>((resolve) => { resolveClosed = resolve; });
  let rejectStarted!: (error: unknown) => void;
  const started = new Promise<{ mime_type: "audio/webm" }>((_r, reject) => {
    rejectStarted = reject;
  });
  let rejectStopped!: (error: unknown) => void;
  const stopped = new Promise<never>((_r, reject) => { rejectStopped = reject; });
  const capture = {
    started,
    stopped,
    closed,
    locallyStarted: false,
    interrupt: () => {
      events.push("record:mic-physically-stopped");
      const error = new AutopilotMediaError(
        "device_runtime_failed", "页面已离开前台，本次录音已被丢弃");
      rejectStarted(error);
      // stopped 也必须 settle：controller 为它挂了一个有界观察，永不 settle 会
      // 把那个观察定时器一直吊着。
      rejectStopped(error);
      resolveClosed();
      return "before_start" as const;
    },
    cancel: () => { events.push("record:cancel"); },
  };
  const authority = durableAuthorityDelivery(events, acks);
  const controller = new PatientAutopilotController({
    sessionId: "S-LIFECYCLE-BEFORE",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => capture },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  await Promise.resolve();
  await Promise.resolve();
  const shutdown = controller.interruptRecordingForLifecycleAndWait();
  await polling;
  await shutdown;

  assert.deepEqual(acks, []);
  assert.equal(events.includes("record:cancel"), false);
  assert.ok(events.includes("record:mic-physically-stopped"));
});

test("too_late 成功：零 cancel，照常发出真实的 record_stopped", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  const authority = durableAuthorityDelivery(events, acks);
  const controller = new PatientAutopilotController({
    sessionId: "S-TOO-LATE-OK",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => ({
        ...settledCapture(events),
        // 成功停止已经同步 claim 了这条捕获：生命周期只能 join。
        interrupt: () => "too_late" as const,
      }),
    },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  for (let turn = 0; turn < 50 && acks.length === 0; turn += 1) await Promise.resolve();
  const shutdown = controller.interruptRecordingForLifecycleAndWait();
  const state = await polling;
  await shutdown;

  assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started", "record_stopped"]);
  assert.equal(events.includes("record:cancel"), false);
  assert.equal(state.phase, "waiting_server_after_record");
});

test("too_late 持久化失败：零 cancel，保留真实失败码，绝不改标 device_runtime_failed", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  const authority = durableAuthorityDelivery(events, acks);
  const controller = new PatientAutopilotController({
    sessionId: "S-TOO-LATE-FAIL",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => ({
        ...settledCapture(events, {
          stopped: Promise.reject(new AutopilotMediaError(
            "recording_upload_failed", "录音上传或采集收据未完成")) as Promise<never>,
        }),
        interrupt: () => "too_late" as const,
      }),
    },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const polling = controller.pollOnce();
  for (let turn = 0; turn < 50 && acks.length === 0; turn += 1) await Promise.resolve();
  const shutdown = controller.interruptRecordingForLifecycleAndWait();
  await polling;
  await shutdown;

  assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started", "record_failed"]);
  const failed = acks[1];
  if (failed?.ack_type === "record_failed") {
    // 一次真实的上传失败，不是生命周期失败。
    assert.equal(failed.error_code, "recording_upload_failed");
    assert.equal(failed.command_revision, 1);
  }
  assert.equal(events.includes("record:cancel"), false);
});

// ---------------- stage 证明为空 → 唯一同 seq replacement ----------------

const REPLACEMENT_SESSION = "S-REPLACE";
const REPLACEMENT_CAPABILITY: DeviceCapabilityRecord = {
  capability: "r".repeat(43),
  sessionId: REPLACEMENT_SESSION,
  expiresAt: "2027-07-19T12:00:00Z",
};

function deepClone<T>(value: T): T {
  return value === null || value === undefined
    ? value : JSON.parse(JSON.stringify(value)) as T;
}

/** 可控 store：按 ACK 类型决定哪一次 stage 抛错，snapshot 永远交回深拷贝。 */
class ReplacementStore implements AutopilotAckStore {
  pending: AutopilotAckEnvelope | null = null;
  checkpoint: AutopilotAckCheckpoint | null = null;
  staged: AutopilotAckEnvelope[] = [];
  events: string[] = [];
  failStageFor: Array<"record_started" | "record_failed"> = ["record_started"];
  /** 事务其实提交了、随后才抛错：snapshot 会读到逐字相同的 pending。 */
  commitThenThrowFor: Array<"record_started" | "record_failed"> = [];
  snapshotThrows = false;
  onStage: ((entry: AutopilotAckEnvelope) => void) | null = null;
  /**
   * 拿到 stage **活引用**、且排在既有失败路径抛错之前的钩子。
   *
   * 与 `onStage` 分开是故意的：`onStage` 那条给生命周期用，这一条给"在真事务里
   * 只改一个字段"用，混成一件事就看不清究竟是哪一项让替换失去授权。
   */
  onStageMutate: ((entry: AutopilotAckEnvelope) => void) | null = null;
  /** 让用例自持一个确切的哨兵，而不是每次都拿一个新的匿名 Error。 */
  stageError: unknown = null;

  async snapshot(): Promise<{
    pending: AutopilotAckEnvelope | null; checkpoint: AutopilotAckCheckpoint | null;
  }> {
    this.events.push("snapshot");
    if (this.snapshotThrows) throw new Error("本机 ACK 存储不可读");
    return { pending: deepClone(this.pending), checkpoint: deepClone(this.checkpoint) };
  }

  async stage(entry: AutopilotAckEnvelope): Promise<void> {
    this.events.push(`stage:${entry.ack.ack_type}`);
    this.staged.push(deepClone(entry));
    this.onStage?.(entry);
    this.onStageMutate?.(entry);
    if ((this.failStageFor as string[]).includes(entry.ack.ack_type)) {
      // 事务未提交：pending 一个字节都没落下。
      throw this.stageError ?? new Error("暂存事务中止");
    }
    this.pending = deepClone(entry);
    if ((this.commitThenThrowFor as string[]).includes(entry.ack.ack_type)) {
      throw new Error("暂存已提交但随后中止");
    }
  }

  async stageRecordStoppedAndCompleteAudio(entry: AutopilotAckEnvelope): Promise<void> {
    this.events.push("stage-record");
    this.staged.push(deepClone(entry));
    this.pending = deepClone(entry);
  }

  async complete(entry: AutopilotAckEnvelope): Promise<void> {
    this.events.push("complete");
    this.checkpoint = deepClone(nextAutopilotAckCheckpoint(entry, 1));
    this.pending = null;
  }
}

/**
 * 已真实开录、随后被生命周期同步丢弃的 capture。
 *
 * `preSettled` 用于"根本没有生命周期中断"那一类用例：那里没有人会去 settle
 * stopped/closed，留着会让 runRecording 的 finally 永远等下去。
 */
function interruptibleCapture(
  events: string[],
  disposition: "before_start" | "after_start" | "too_late" | "already_interrupted"
    = "after_start",
  preSettled = false,
  options: { startedFacts?: ObservedStartFacts; manualClosed?: boolean } = {},
) {
  let rejectStopped: ((error: unknown) => void) | null = null;
  let resolveClosed: (() => void) | null = null;
  const discarded = new AutopilotMediaError(
    "device_runtime_failed", "页面已离开前台，本次录音已被丢弃");
  const counters = { interrupts: 0, cancels: 0 };
  const startedFacts: ObservedStartFacts = options.startedFacts ?? { mime_type: "audio/webm" };
  const capture = {
    started: Promise.resolve(startedFacts),
    stopped: preSettled
      ? Promise.reject(discarded) as Promise<never>
      : new Promise<never>((_resolve, reject) => { rejectStopped = reject; }),
    closed: preSettled
      ? Promise.resolve()
      : new Promise<void>((resolve) => { resolveClosed = resolve; }),
    locallyStarted: true,
    interrupt: () => {
      counters.interrupts += 1;
      events.push("record:mic-physically-stopped");
      rejectStopped?.(discarded);
      // manualClosed：物理丢弃已经认领，但 closed 的 resolve 权留在测试手里，
      // 用来钉住"替换必须排在 await capture.closed 之后"。
      if (!options.manualClosed) resolveClosed?.();
      return disposition;
    },
    cancel: () => {
      counters.cancels += 1;
      events.push("record:cancel");
      // 普通失败链也要让 closed 收口，否则 runRecording 的 finally 永远等在那里。
      rejectStopped?.(new AutopilotMediaError(
        "recording_runtime_failed", "录音过程未正常结束"));
      resolveClosed?.();
    },
  };
  capture.stopped.catch(() => {});
  return { capture, counters, releaseClosed: () => { resolveClosed?.(); } };
}

/** 本机真实观察到的开录事实；可选两项的"在场与否"本身就是授权判据的一部分。 */
type ObservedStartFacts = {
  mime_type: AutopilotMimeType;
  sample_rate_hz?: number;
  channels?: 1 | 2;
};

function recordStartedReceipt(ack: AutopilotAck): unknown {
  return {
    scope_key: "p0a_sim_first_single_v1",
    ack_idempotency_key: ack.idempotency_key,
    ack_type: ack.ack_type,
    replayed: false,
    command_state: "started",
    command_revision: 1,
    status: "waiting_recording",
    state_revision: 2,
    command: recordCommand("started"),
  };
}

function terminalReceipt(ack: AutopilotAck): unknown {
  return {
    scope_key: "p0a_sim_first_single_v1",
    ack_idempotency_key: ack.idempotency_key,
    ack_type: ack.ack_type,
    replayed: false,
    command_state: "failed",
    command_revision: ack.command_revision + 1,
    status: "paused",
    state_revision: 3,
    command: null,
  };
}

test("after_start + stage 证明为空：唯一同 seq 的 record_failed replacement，候选 transport 0", async () => {
  const events: string[] = [];
  const acked: AutopilotAck[] = [];
  const store = new ReplacementStore();
  const seqs: number[] = [];
  const delivery = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        acked.push(ack);
        return terminalReceipt(ack);
      },
    },
  });
  const media = interruptibleCapture(events);
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("持久 delivery 路径不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: delivery,
    onDeviceEventSeq: (seq) => { seqs.push(seq); },
    idempotencyKey: fixedAckKey,
  });
  // pagehide 正好落在 record_started 的 durable stage 上：同步物理关麦，
  // 判据是 after_start，而那条候选此刻还没上过网。
  store.onStage = (entry) => {
    if (entry.ack.ack_type === "record_started") {
      controller.handleRecordingLifecycleInterruption();
    }
  };

  const state = await controller.pollOnce();

  // 候选与替换：同一个 device event seq，不同幂等键，只有替换成为事件。
  assert.deepEqual(store.staged.map((entry) => entry.ack.ack_type),
    ["record_started", "record_failed"]);
  assert.deepEqual(store.staged.map((entry) => entry.ack.device_event_seq), [1, 1]);
  assert.notEqual(store.staged[0]?.ack.idempotency_key, store.staged[1]?.ack.idempotency_key);
  // 被丢弃的 started 候选一次都没有 transport；网络上只有那一条 record_failed。
  assert.deepEqual(acked.map((ack) => ack.ack_type), ["record_failed"]);
  const failed = acked[0];
  if (failed?.ack_type === "record_failed") {
    assert.equal(failed.error_code, "device_runtime_failed");
    assert.equal(failed.device_event_seq, 1);
  }
  // 序号只推进一次；interrupt 一次；cancel 与音频原子接管都为 0。
  assert.deepEqual(seqs, [1]);
  assert.equal(state.last_device_event_seq, 1);
  assert.equal(media.counters.interrupts, 1);
  assert.equal(media.counters.cancels, 0);
  assert.equal(store.events.includes("stage-record"), false);
  await media.capture.closed;
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "record_failed");

  // 用同一个 store 新建 delivery：那条 exact 失败 latch 跨重载仍在。
  const reloaded = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async () => { throw new Error("重载后不应再发送"); },
    },
  });
  assert.equal(reloaded.terminalFailureLatch?.ack.ack_type, "record_failed");
  assert.equal(reloaded.terminalFailureLatch?.commandKey, recordCommand().command_key);
  assert.equal(reloaded.initialDeviceEventSeq, 1);
});

test("replacement 自己也持久化失败：不产生第三条 envelope", async () => {
  const events: string[] = [];
  const store = new ReplacementStore();
  store.failStageFor = ["record_started", "record_failed"];
  let httpCalls = 0;
  const delivery = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => { httpCalls += 1; return terminalReceipt(ack); },
    },
  });
  const media = interruptibleCapture(events);
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: delivery,
    idempotencyKey: fixedAckKey,
  });
  store.onStage = (entry) => {
    if (entry.ack.ack_type === "record_started") {
      controller.handleRecordingLifecycleInterruption();
    }
  };

  const state = await controller.pollOnce();

  // 恰好两条：被丢弃的候选与那条替换。绝不递归造第三条。
  assert.deepEqual(store.staged.map((entry) => entry.ack.ack_type),
    ["record_started", "record_failed"]);
  assert.equal(httpCalls, 0);
  assert.equal(media.counters.cancels, 0);
  assert.equal(state.phase, "paused");
});

test("stage matching：继续原 started ACK，零同 seq 替换；已确认 N 之后仍允许既有 N+1 生命周期失败", async () => {
  const events: string[] = [];
  const acked: AutopilotAck[] = [];
  const store = new ReplacementStore();
  // 事务其实提交了、随后才抛错：snapshot 读回逐字相同的 pending，走 matching。
  store.failStageFor = [];
  store.commitThenThrowFor = ["record_started"];
  const delivery = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        acked.push(ack);
        return ack.ack_type === "record_started"
          ? recordStartedReceipt(ack) : terminalReceipt(ack);
      },
    },
  });
  const media = interruptibleCapture(events);
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: delivery,
    idempotencyKey: fixedAckKey,
  });
  const polling = controller.pollOnce();
  for (let turn = 0; turn < 50 && acked.length === 0; turn += 1) await Promise.resolve();
  controller.handleRecordingLifecycleInterruption();
  const state = await polling;

  // started 在 N 确认，生命周期失败落在 N+1：这不是被丢弃候选的同 seq 替换。
  assert.deepEqual(acked.map((ack) => ack.ack_type), ["record_started", "record_failed"]);
  assert.deepEqual(acked.map((ack) => ack.device_event_seq), [1, 2]);
  assert.equal(store.staged.filter((entry) => entry.ack.device_event_seq === 1).length, 1);
  assert.equal(media.counters.cancels, 0);
  assert.equal(state.pause_reason, "record_failed");
});

test("stage unknown：零替换、零网络，安全暂停", async () => {
  const events: string[] = [];
  const store = new ReplacementStore();
  let httpCalls = 0;
  // 真构造器必须先读到一份正常的深拷贝初始 snapshot；`snapshotThrows` 提前置位
  // 会让 create() 当场 reject，控制器那条 stage-unknown 分支一次都走不到。
  const delivery = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => { httpCalls += 1; return terminalReceipt(ack); },
    },
  });
  const media = interruptibleCapture(events);
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: delivery,
    idempotencyKey: fixedAckKey,
  });
  store.onStage = (entry) => {
    if (entry.ack.ack_type === "record_started") {
      controller.handleRecordingLifecycleInterruption();
    }
  };
  // stage 抛错之后 readOwnerSnapshot 才失败，正好落进 retainUnresolved 的
  // unknown 分支——即本用例本来要证的那条路径。不加 try/catch、不换假 delivery。
  store.snapshotThrows = true;

  const state = await controller.pollOnce();

  assert.deepEqual(store.staged.map((entry) => entry.ack.ack_type), ["record_started"]);
  assert.equal(httpCalls, 0);
  assert.equal(media.counters.cancels, 0);
  assert.equal(state.phase, "paused");
  assert.equal(state.last_device_event_seq, 0);
});

/**
 * 替换判定的共享账本。
 *
 * `delivery.send` 的调用次数与实际被构造/暂存的幂等键是两件不同的事实，必须分开
 * 记：只断言"零 HTTP"、或者拿 send 次数冒充 key 观察，都会在这里被单独抓住。
 */
interface ReplacementLedger {
  /** 每一次 delivery.send 调用，只记请求的 ACK 类型。 */
  sendCalls: Array<{ ackType: string }>;
  /** 实际被构造/暂存的 envelope 所携带的幂等键。 */
  keys: string[];
  envelopes: Array<{ ackType: string; deviceEventSeq: number; idempotencyKey: string }>;
  stage: string[];
  transport: string[];
  complete: string[];
  seqAdvances: number[];
  ownershipChecks: Array<{ error: unknown; result: boolean }>;
  /** delegate 捕获并原样重抛的那个对象。 */
  failure: unknown;
}

function newReplacementLedger(): ReplacementLedger {
  return {
    sendCalls: [], keys: [], envelopes: [], stage: [], transport: [],
    complete: [], seqAdvances: [], ownershipChecks: [], failure: null,
  };
}

/** 候选 record_started 的 ACK：identity 夹具与各组变异都从这一处取基线。 */
function candidateStartedAck(facts: ObservedStartFacts): AutopilotAck {
  return buildAutopilotAck(recordCommand(), 0, "k:record_started:1", {
    ack_type: "record_started", ...facts,
  });
}

/**
 * 契约忠实的 delivery 替身：只用来精确破坏 typed 拒绝的某一项，证明控制器逐项
 * 核对。归属由 `ownsPersistenceError` 单独裁决，不是拿错误自己的字段互证。
 *
 * 相对旧的 `rejectingDelivery`，失败语义一字不变（同一条预建的
 * `AutopilotAckPersistenceError`、同一个归属裁决），只是每次调用都把 send 调用、
 * 实际构造出来的 envelope 与幂等键分开记进共享账本。
 */
function instrumentedRejectingDelivery(input: {
  ledger: ReplacementLedger;
  sends: AutopilotAck[];
  identity?: Partial<AutopilotAckPersistenceIdentity>;
  phase?: "stage" | "complete";
  outcome?: "confirmed_empty" | "matching_pending" | "unknown";
  owns?: boolean;
  /** 在 record_started 的 durable stage 那一刻触发页面生命周期中断。 */
  onSend?: () => void;
}): AutopilotAckDelivery {
  const command = recordCommand();
  const base: AutopilotAckPersistenceIdentity = {
    ownerKey: "d".repeat(64),
    sessionId: REPLACEMENT_SESSION,
    commandKey: command.command_key,
    command,
    ack: candidateStartedAck({ mime_type: "audio/webm" }),
    ackType: "record_started",
    idempotencyKey: "k:record_started:1",
    deviceEventSeq: 1,
    commandRevision: command.command_revision,
    controlGeneration: command.control_generation,
    runnerGeneration: command.runner_generation,
    mimeType: "audio/webm",
  };
  const failure = new AutopilotAckPersistenceError({
    phase: input.phase ?? "stage",
    outcome: input.outcome ?? "confirmed_empty",
    identity: { ...base, ...input.identity },
    message: "暂存事务未提交",
  });
  const ledger = input.ledger;
  return {
    initialDeviceEventSeq: 0,
    lastReceiptAuthority: null,
    ownsPersistenceError: (error): error is AutopilotAckPersistenceError => {
      const result = (input.owns ?? true) && error === failure;
      ledger.ownershipChecks.push({ error, result });
      return result;
    },
    send: async (sentCommand, seq, facts) => {
      // 调用前恰好记一行，之后不再补。
      ledger.sendCalls.push({ ackType: facts.ack_type });
      const ack = buildAutopilotAck(sentCommand, seq, `k:${facts.ack_type}:${seq}`, facts);
      ledger.envelopes.push({
        ackType: ack.ack_type,
        deviceEventSeq: ack.device_event_seq,
        idempotencyKey: ack.idempotency_key,
      });
      ledger.keys.push(ack.idempotency_key);
      ledger.stage.push(`stage:${ack.ack_type}`);
      if (facts.ack_type === "record_started") {
        // 生产里 stage 先于 transport：候选卡在 stage 上，transport / complete
        // 因此必须保持为空。
        input.onSend?.();
        ledger.failure = failure;
        throw failure;
      }
      ledger.transport.push(`http:${ack.ack_type}`);
      ledger.complete.push(`complete:${ack.ack_type}`);
      input.sends.push(ack);
      return ack;
    },
  };
}

/**
 * 只做记账的透传委托，给真 `DurableAutopilotAckDelivery` 用。
 *
 * 两个只读属性都是 getter，每次读都从 `real` 上取当下值；`ownsPersistenceError`
 * 与 `send` 都以 `real.method(...)` 的形式调用，接收者就是 `real`，所以
 * `this.ownedFailures` / `this.ownerKey` / `this.sessionId` 全部指向真实例。
 * 它一个类方法都不复制、不解绑，也不合成、包装或替换任何 Error。
 */
function recordingDurableDelegate(
  real: DurableAutopilotAckDelivery,
  ledger: ReplacementLedger,
): AutopilotAckDelivery {
  return {
    get initialDeviceEventSeq() { return real.initialDeviceEventSeq; },
    get lastReceiptAuthority() { return real.lastReceiptAuthority; },
    ownsPersistenceError: (error): error is AutopilotAckPersistenceError => {
      const result = real.ownsPersistenceError(error);
      ledger.ownershipChecks.push({ error, result });
      return result;
    },
    send: async (command, lastSeq, facts) => {
      ledger.sendCalls.push({ ackType: facts.ack_type });
      try {
        return await real.send(command, lastSeq, facts);
      } catch (error) {
        ledger.failure = error;      // 存下那个确切对象
        throw error;                 // 原样重抛同一个对象
      }
    },
  };
}

type ReplacementMedia = { counters: { interrupts: number; cancels: number } };

/**
 * 负例的唯一断言入口，每一行独立执行、逐项分开取证。
 *
 * 零 HTTP 只是其中一项，绝不替代 sendCalls / key / envelope / stage / complete
 * 任何一项的取证。
 */
function assertZeroSameSeqReplacement(
  ledger: ReplacementLedger,
  media: ReplacementMedia,
  state: AutopilotRuntimeState,
  expected: { cancels: number },
): void {
  assert.equal(ledger.sendCalls.length, 1);
  assert.equal(ledger.sendCalls[0]?.ackType, "record_started");
  assert.equal(ledger.envelopes.length, 1);
  assert.equal(ledger.keys.length, 1);
  assert.equal(ledger.stage.filter((row) => row.endsWith("record_failed")).length, 0);
  assert.equal(ledger.transport.length, 0);
  assert.equal(ledger.complete.length, 0);
  assert.deepEqual(ledger.seqAdvances, []);
  assert.equal(state.last_device_event_seq, 0);
  assert.equal(state.phase, "paused");
  assert.equal(media.counters.cancels, expected.cancels);
}

/** 镜像正例入口：恰好一条同 seq、不同幂等键的 record_failed 走完 stage/HTTP/complete。 */
function assertSingleSameSeqReplacement(
  ledger: ReplacementLedger,
  media: ReplacementMedia,
  state: AutopilotRuntimeState,
): void {
  assert.equal(ledger.sendCalls.length, 2);
  assert.equal(ledger.sendCalls[0]?.ackType, "record_started");
  assert.equal(ledger.sendCalls[1]?.ackType, "record_failed");
  assert.equal(ledger.envelopes.length, 2);
  assert.deepEqual(ledger.envelopes.map((row) => row.deviceEventSeq), [1, 1]);
  assert.notEqual(ledger.envelopes[0]?.idempotencyKey, ledger.envelopes[1]?.idempotencyKey);
  assert.equal(ledger.keys.length, 2);
  assert.notEqual(ledger.keys[0], ledger.keys[1]);
  assert.equal(ledger.stage.filter((row) => row.endsWith("record_failed")).length, 1);
  assert.equal(ledger.transport.length, 1);
  assert.equal(ledger.complete.length, 1);
  assert.deepEqual(ledger.seqAdvances, [1]);
  assert.equal(state.last_device_event_seq, 1);
  assert.equal(state.phase, "paused");
  assert.equal(media.counters.cancels, 0);
}

/**
 * G0-G5、G7 与具名负例 2/3 共用的一次替换判定编排。
 *
 * 每一行只破坏一件预期的授权事实，其余全部事实保持精确；`onSend` 让 pagehide
 * 正好落在 durable stage 那一刻，于是同一条 capture 保留下来的 `after_start`
 * 拥有物理丢弃。
 */
async function runReplacementAuthorityCase(input: {
  identity?: Partial<AutopilotAckPersistenceIdentity>;
  phase?: "stage" | "complete";
  outcome?: "confirmed_empty" | "matching_pending" | "unknown";
  owns?: boolean;
  disposition?: "before_start" | "after_start" | "too_late" | "already_interrupted";
  startedFacts?: ObservedStartFacts;
  interrupted?: boolean;
  preSettled?: boolean;
}): Promise<{
  events: string[];
  sends: AutopilotAck[];
  ledger: ReplacementLedger;
  media: ReturnType<typeof interruptibleCapture>;
  state: AutopilotRuntimeState;
}> {
  const events: string[] = [];
  const sends: AutopilotAck[] = [];
  const ledger = newReplacementLedger();
  const media = interruptibleCapture(
    events,
    input.disposition ?? "after_start",
    input.preSettled ?? false,
    { startedFacts: input.startedFacts },
  );
  const holder: { controller: PatientAutopilotController | null } = { controller: null };
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: instrumentedRejectingDelivery({
      ledger,
      sends,
      identity: input.identity,
      phase: input.phase,
      outcome: input.outcome,
      owns: input.owns,
      onSend: (input.interrupted ?? true)
        ? () => holder.controller?.handleRecordingLifecycleInterruption()
        : undefined,
    }),
    onDeviceEventSeq: (seq) => { ledger.seqAdvances.push(seq); },
    idempotencyKey: fixedAckKey,
  });
  holder.controller = controller;

  const state = await controller.pollOnce();
  return { events, sends, ledger, media, state };
}

for (const negative of [
  { name: "归属认领不通过（字段全等、只是别人造的）", input: { owns: false } },
  { name: "阶段是 complete", input: { phase: "complete" as const } },
  { name: "结果是 matching_pending", input: { outcome: "matching_pending" as const } },
  { name: "结果是 unknown", input: { outcome: "unknown" as const } },
  { name: "session 不是本控制器的", input: { identity: { sessionId: "S-OTHER" } } },
  { name: "ACK 类型不是 record_started", input: { identity: { ackType: "record_stopped" as const } } },
  { name: "命令键漂移", input: { identity: { commandKey: "cmd-record-controller-9999" } } },
  { name: "device event seq 不是下一个", input: { identity: { deviceEventSeq: 5 } } },
  { name: "command revision 漂移", input: { identity: { commandRevision: 3 } } },
  { name: "control generation 漂移", input: { identity: { controlGeneration: 9 } } },
  { name: "runner generation 漂移", input: { identity: { runnerGeneration: 9 } } },
  { name: "MIME 与本机观察不符", input: { identity: { mimeType: "audio/mp4" as const } } },
  {
    name: "投影 payload 漂移",
    input: {
      identity: {
        command: {
          ...recordCommand(),
          payload: { ...recordCommand().payload, turn_ref: "itm-0001#9" },
        } as NextCommandProjection,
      },
    },
  },
  {
    name: "投影 kind 不是 record",
    input: { identity: { command: questionCommand("pending") } },
  },
  {
    name: "外层 idempotencyKey 与内层 ACK 不一致",
    input: { identity: { idempotencyKey: "k:record_started:9" } },
  },
] as Array<{ name: string; input: Parameters<typeof runReplacementAuthorityCase>[0] }>) {
  test(`零替换（${negative.name}）：不发 record_failed，落回既有 fail-closed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase(negative.input);

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

for (const disposition of ["too_late", "before_start", "already_interrupted"] as const) {
  test(`零替换（生命周期判据是 ${disposition}）：不发 record_failed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({ disposition });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

test("零替换（根本没有生命周期中断）：stage 证明为空也不许替换", async () => {
  const { events, sends, ledger, media, state } = await runReplacementAuthorityCase({
    preSettled: true, interrupted: false,
  });

  assert.deepEqual(sends, []);
  assert.equal(media.counters.interrupts, 0);
  // 这条路的物理收口归 runRecording 的 finally 所有，不归 cancel()：typed 拒绝
  // 从 try 里抛出时 finally 先 `await capture.closed` 并把 activeMedia 置空，
  // 于是 runOnce 的 catch 拿到的 media 已经是 null，那一次 cancel 不会执行。
  // 「普通 fail-closed 恰好一次 cancel」由未被改动的
  // 「普通非生命周期失败：物理 cancel 恰好一次，失败码照实上报」独立证明——
  // 那条路是 observedStop 失败分支里的 capture.cancel()，不是这一条。
  assert.equal(events.includes("record:cancel"), false);
  assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
});

test("普通非生命周期失败：物理 cancel 恰好一次，失败码照实上报", async () => {
  const events: string[] = [];
  const acks: AutopilotAck[] = [];
  const authority = durableAuthorityDelivery(events, acks);
  const controller = new PatientAutopilotController({
    sessionId: "S-ORDINARY-FAIL",
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: {
      start: () => settledCapture(events, {
        stopped: Promise.reject(new AutopilotMediaError(
          "recording_upload_failed", "录音上传或采集收据未完成")) as Promise<never>,
      }),
    },
    ackDelivery: authority.delivery,
    idempotencyKey: fixedAckKey,
  });

  const state = await controller.pollOnce();

  // 没有生命周期判据时才允许 cancel，而且只此一次。
  assert.deepEqual(events.filter((row) => row === "record:cancel"), ["record:cancel"]);
  assert.deepEqual(acks.map((ack) => ack.ack_type), ["record_started", "record_failed"]);
  const failed = acks[1];
  if (failed?.ack_type === "record_failed") {
    assert.equal(failed.error_code, "recording_upload_failed");
  }
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "record_failed");
});

// ---------------- G1：command 顶层字段逐个一字段变异 ----------------

const COMMAND_FIELD_DRIFT: Record<string, unknown> = {
  schema_version: 2,
  kind: "tts",
  command_key: "cmd-record-controller-8888",
  command_seq: 99,
  state: "started",
  command_revision: 5,
  control_generation: 9,
  runner_generation: 11,
  item_ref: "itm-0009",
  turn_seq: 4,
  attempt_seq: 2,
  prompt_level: 1,
};

function driftedCommandField(field: string): NextCommandProjection {
  const command = recordCommand() as unknown as Record<string, unknown>;
  command[field] = COMMAND_FIELD_DRIFT[field];
  return command as unknown as NextCommandProjection;
}

for (const field of [
  "schema_version", "kind", "command_key", "command_seq", "state", "command_revision",
  "control_generation", "runner_generation", "item_ref", "turn_seq", "attempt_seq",
  "prompt_level",
] as const) {
  test(`零替换（command 顶层字段 ${field} 漂移）：不发 record_failed，落回既有 fail-closed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { command: driftedCommandField(field) },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

// ---------------- G2：record payload 字段逐个一字段变异 ----------------

const PAYLOAD_FIELD_DRIFT: Record<string, unknown> = {
  raw_audio_id: "raw-controller-issued-9999",
  turn_ref: "itm-0001#9",
  max_duration_seconds: 20,
  contains_direct_identifier: true,
  presentation_speech_key: "controller.other",
  presentation_speech_text: "另一段话术。",
  presentation_purpose: "cue",
};

function driftedPayloadField(field: string): NextCommandProjection {
  const command = recordCommand() as unknown as Record<string, unknown>;
  const payload = command.payload as Record<string, unknown>;
  payload[field] = PAYLOAD_FIELD_DRIFT[field];
  return command as unknown as NextCommandProjection;
}

for (const field of [
  "raw_audio_id", "turn_ref", "max_duration_seconds", "contains_direct_identifier",
  "presentation_speech_key", "presentation_speech_text", "presentation_purpose",
] as const) {
  test(`零替换（record payload 字段 ${field} 漂移）：不发 record_failed，落回既有 fail-closed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { command: driftedPayloadField(field) },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

// ---------------- G3：键形（缺键 / 自有 undefined 键 / 额外键） ----------------

/**
 * 缺键、自有 `undefined` 键、额外键是三件互相独立的事实。
 *
 * canonical record 命令目前没有任何可选 command/payload 字段，所以这里不臆造
 * 可选项：额外键在这一组里始终是 fail-closed 负例。
 */
function mutableRecordCommand(): Record<string, unknown> {
  return recordCommand() as unknown as Record<string, unknown>;
}

for (const shape of [
  {
    name: "command 缺键 turn_seq",
    build: () => {
      const command = mutableRecordCommand();
      delete command.turn_seq;
      return command;
    },
  },
  {
    name: "command 自有 undefined 键 turn_seq",
    build: () => {
      const command = mutableRecordCommand();
      Object.defineProperty(command, "turn_seq", {
        value: undefined, writable: true, enumerable: true, configurable: true,
      });
      return command;
    },
  },
  {
    name: "command 额外键 __extra__",
    build: () => {
      const command = mutableRecordCommand();
      Object.defineProperty(command, "__extra__", {
        value: 1, writable: true, enumerable: true, configurable: true,
      });
      return command;
    },
  },
  {
    name: "payload 缺键 turn_ref",
    build: () => {
      const command = mutableRecordCommand();
      delete (command.payload as Record<string, unknown>).turn_ref;
      return command;
    },
  },
  {
    name: "payload 自有 undefined 键 turn_ref",
    build: () => {
      const command = mutableRecordCommand();
      Object.defineProperty(command.payload as Record<string, unknown>, "turn_ref", {
        value: undefined, writable: true, enumerable: true, configurable: true,
      });
      return command;
    },
  },
  {
    name: "payload 额外键 __extra__",
    build: () => {
      const command = mutableRecordCommand();
      Object.defineProperty(command.payload as Record<string, unknown>, "__extra__", {
        value: 1, writable: true, enumerable: true, configurable: true,
      });
      return command;
    },
  },
]) {
  test(`零替换（键形 ${shape.name}）：不发 record_failed，落回既有 fail-closed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { command: shape.build() as unknown as NextCommandProjection },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

// ---------------- G4：内层 ACK 字段逐个一字段变异 ----------------

function driftedCandidateAck(apply: (ack: Record<string, unknown>) => void): AutopilotAck {
  const ack = {
    ...candidateStartedAck({ mime_type: "audio/webm" }),
  } as unknown as Record<string, unknown>;
  apply(ack);
  return ack as unknown as AutopilotAck;
}

for (const drift of [
  { field: "ack_type", apply: (ack: Record<string, unknown>) => { ack.ack_type = "record_stopped"; } },
  { field: "idempotency_key", apply: (ack: Record<string, unknown>) => { ack.idempotency_key = "k:record_started:2"; } },
  { field: "device_event_seq", apply: (ack: Record<string, unknown>) => { ack.device_event_seq = 5; } },
  { field: "command_revision", apply: (ack: Record<string, unknown>) => { ack.command_revision = 4; } },
  { field: "control_generation", apply: (ack: Record<string, unknown>) => { ack.control_generation = 8; } },
  { field: "runner_generation", apply: (ack: Record<string, unknown>) => { ack.runner_generation = 12; } },
  { field: "mime_type", apply: (ack: Record<string, unknown>) => { ack.mime_type = "audio/mp4"; } },
  {
    field: "额外键 __extra__",
    apply: (ack: Record<string, unknown>) => {
      Object.defineProperty(ack, "__extra__", {
        value: 1, writable: true, enumerable: true, configurable: true,
      });
    },
  },
]) {
  test(`零替换（内层 ACK 字段 ${drift.field} 漂移）：不发 record_failed，落回既有 fail-closed`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { ack: driftedCandidateAck(drift.apply) },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

// ---------------- G5a/G5b/G5c：可选事实的在场与否本身就是判据 ----------------

const OPTIONAL_PRESENT_FACTS: ObservedStartFacts = {
  mime_type: "audio/webm", sample_rate_hz: 48_000, channels: 1,
};

function ackWithOwnUndefined(key: string): AutopilotAck {
  return driftedCandidateAck((ack) => {
    Object.defineProperty(ack, key, {
      value: undefined, writable: true, enumerable: true, configurable: true,
    });
  });
}

for (const row of [
  {
    name: "完全一致时替换成立",
    ack: () => candidateStartedAck(OPTIONAL_PRESENT_FACTS),
    expect: "single" as const,
  },
  {
    name: "ACK 缺 sample_rate_hz 判负",
    ack: () => candidateStartedAck({ mime_type: "audio/webm", channels: 1 }),
    expect: "zero" as const,
  },
  {
    name: "ACK 缺 channels 判负",
    ack: () => candidateStartedAck({ mime_type: "audio/webm", sample_rate_hz: 48_000 }),
    expect: "zero" as const,
  },
  {
    name: "ACK sample_rate_hz 换成另一个合法值判负",
    ack: () => candidateStartedAck({
      mime_type: "audio/webm", sample_rate_hz: 44_100, channels: 1,
    }),
    expect: "zero" as const,
  },
  {
    name: "ACK channels 换成另一个合法值判负",
    ack: () => candidateStartedAck({
      mime_type: "audio/webm", sample_rate_hz: 48_000, channels: 2,
    }),
    expect: "zero" as const,
  },
]) {
  test(`可选事实（观察到 sample_rate_hz+channels）：${row.name}`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { ack: row.ack() },
      startedFacts: OPTIONAL_PRESENT_FACTS,
    });

    assert.equal(media.counters.interrupts, 1);
    if (row.expect === "single") {
      assert.deepEqual(sends.map((ack) => ack.ack_type), ["record_failed"]);
      assertSingleSameSeqReplacement(ledger, media, state);
      assert.equal(state.pause_reason, "record_failed");
      return;
    }
    assert.deepEqual(sends, []);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

for (const row of [
  {
    name: "ACK 多出合法 sample_rate_hz 判负",
    ack: () => candidateStartedAck({ mime_type: "audio/webm", sample_rate_hz: 48_000 }),
  },
  {
    name: "ACK 多出合法 channels 判负",
    ack: () => candidateStartedAck({ mime_type: "audio/webm", channels: 1 }),
  },
  {
    name: "ACK 多出自有 undefined 的 sample_rate_hz 判负",
    ack: () => ackWithOwnUndefined("sample_rate_hz"),
  },
  {
    name: "ACK 多出自有 undefined 的 channels 判负",
    ack: () => ackWithOwnUndefined("channels"),
  },
]) {
  test(`可选事实（观察到的开录不带可选字段）：${row.name}`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { ack: row.ack() },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

for (const row of [
  {
    name: "ACK 多出合法 client_observed_ms 判负",
    ack: () => driftedCandidateAck((ack) => { ack.client_observed_ms = 1_234; }),
  },
  {
    name: "ACK 多出自有 undefined 的 client_observed_ms 判负",
    ack: () => ackWithOwnUndefined("client_observed_ms"),
  },
]) {
  test(`可选事实（生产候选不传 clientObservedMs）：${row.name}`, async () => {
    const { sends, ledger, media, state } = await runReplacementAuthorityCase({
      identity: { ack: row.ack() },
    });

    assert.deepEqual(sends, []);
    assert.equal(media.counters.interrupts, 1);
    assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
  });
}

// ---------------- G6：生产比较器的数组变异 oracle（唯一的纯比较器例外） ----------------

function sparseHoleArray(): unknown[] {
  const value = new Array(3) as unknown[];
  value[0] = 1;
  value[2] = 3;                       // index 1 从不被赋值，恒为洞
  return value;
}

function ownUndefinedIndexArray(): unknown[] {
  const value = new Array(3) as unknown[];
  value[0] = 1;
  value[2] = 3;
  Object.defineProperty(value, "1", {
    value: undefined, writable: true, enumerable: true, configurable: true,
  });
  return value;
}

function enumerableExtraKeyArray(): unknown[] {
  const value = [1, 2, 3] as unknown[];
  Object.defineProperty(value, "extra", {
    value: 9, writable: true, enumerable: true, configurable: true,
  });
  return value;
}

for (const row of [
  {
    name: "两侧逐字相同返回 true",
    left: (): unknown => [1, { a: 2 }, 3],
    right: (): unknown => [1, { a: 2 }, 3],
    expected: true,
  },
  {
    name: "长度不同返回 false",
    left: (): unknown => [1, 2, 3],
    right: (): unknown => [1, 2],
    expected: false,
  },
  {
    name: "左侧稀疏洞对右侧自有 undefined 索引返回 false",
    left: (): unknown => sparseHoleArray(),
    right: (): unknown => ownUndefinedIndexArray(),
    expected: false,
  },
  {
    name: "左侧自有 undefined 索引对右侧稀疏洞返回 false",
    left: (): unknown => ownUndefinedIndexArray(),
    right: (): unknown => sparseHoleArray(),
    expected: false,
  },
  {
    name: "一侧多出可枚举数组额外键返回 false",
    left: (): unknown => enumerableExtraKeyArray(),
    right: (): unknown => [1, 2, 3],
    expected: false,
  },
]) {
  test(`生产比较器数组规则：${row.name}`, () => {
    // 直接调冻结生产里那一个函数：测试不重写比较器、不导出第二份实现，也不调
    // 私有的 replaceDiscardedStartedCandidate。今天的生产授权图里没有数组字段，
    // 数组分支只能这样被直接钉死。
    assert.equal(
      AUTOPILOT_CONTROLLER_AUTHORITY_TEST_ONLY.sameExactAuthorityGraph(row.left(), row.right()),
      row.expected,
    );
  });
}

// ---------------- 具名用例：可控闸正例、基线正例、-0 负例、真 owner 变异负例 ----------------

test("after_start + stage 证明为空（closed 由测试手动释放）：释放前替换 stage/HTTP/complete/seq 全 0 且 pollOnce 未定，释放后恰好一条同 seq record_failed", async () => {
  const events: string[] = [];
  const ledger = newReplacementLedger();
  const store = new ReplacementStore();
  const real = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        ledger.transport.push(`http:${ack.ack_type}`);
        return terminalReceipt(ack);
      },
    },
  });
  // interrupt() 记录 after_start、物理关麦、reject stopped，但**不** resolve
  // closed —— resolve 权由测试自己持有。
  const media = interruptibleCapture(events, "after_start", false, { manualClosed: true });
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("持久 delivery 路径不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: recordingDurableDelegate(real, ledger),
    onDeviceEventSeq: (seq) => { ledger.seqAdvances.push(seq); },
    idempotencyKey: fixedAckKey,
  });
  // 真 envelope 与真 key 由真 store 的 stage 钩子观察；delegate 自己不写这两项。
  store.onStageMutate = (entry) => {
    ledger.envelopes.push({
      ackType: entry.ack.ack_type,
      deviceEventSeq: entry.ack.device_event_seq,
      idempotencyKey: entry.ack.idempotency_key,
    });
    ledger.keys.push(entry.ack.idempotency_key);
  };
  store.onStage = (entry) => {
    if (entry.ack.ack_type === "record_started") {
      controller.handleRecordingLifecycleInterruption();
    }
  };

  let settled = false;
  const polling = controller.pollOnce().then((value) => { settled = true; return value; });
  for (let turn = 0; turn < 60; turn += 1) await Promise.resolve();

  // 释放 closed 之前：替换的 stage / HTTP / complete / seq 一步都不许走。
  assert.equal(settled, false);
  assert.deepEqual(store.staged.map((entry) => entry.ack.ack_type), ["record_started"]);
  assert.equal(store.events.filter((row) => row === "stage:record_failed").length, 0);
  assert.equal(store.events.filter((row) => row === "complete").length, 0);
  assert.deepEqual(ledger.transport, []);
  assert.deepEqual(ledger.seqAdvances, []);
  assert.equal(controller.state.last_device_event_seq, 0);
  assert.equal(media.counters.interrupts, 1);
  assert.equal(media.counters.cancels, 0);
  assert.equal(store.events.includes("stage-record"), false);
  // 归属由真 recognizer 以真接收者裁决；未变异的同形态对照必须返回 true。
  assert.equal(ledger.ownershipChecks.length, 1);
  assert.equal(ledger.ownershipChecks[0]?.result, true);
  assert.equal(ledger.ownershipChecks[0]?.error, ledger.failure);

  media.releaseClosed();
  const state = await polling;

  ledger.stage.push(...store.events.filter((row) => row.startsWith("stage:")));
  ledger.complete.push(...store.events.filter((row) => row === "complete"));

  assert.equal(settled, true);
  assert.deepEqual(store.staged.map((entry) => entry.ack.ack_type),
    ["record_started", "record_failed"]);
  assert.deepEqual(store.staged.map((entry) => entry.ack.device_event_seq), [1, 1]);
  assert.notEqual(store.staged[0]?.ack.idempotency_key, store.staged[1]?.ack.idempotency_key);
  assert.equal(store.staged.length, 2);            // 没有第三条 envelope
  assert.equal(store.events.includes("stage-record"), false);
  assertSingleSameSeqReplacement(ledger, media, state);
  assert.equal(state.pause_reason, "record_failed");
});

test("替换授权成立（instrumentedRejectingDelivery 基线未变异）：恰好发出一条同 seq record_failed，正例对照", async () => {
  const { sends, ledger, media, state } = await runReplacementAuthorityCase({});

  assert.deepEqual(sends.map((ack) => ack.ack_type), ["record_failed"]);
  const replacement = sends[0];
  if (replacement?.ack_type !== "record_failed") throw new Error("正例必须发出 record_failed");
  assert.equal(replacement.error_code, "device_runtime_failed");
  assert.equal(replacement.device_event_seq, 1);
  assert.equal(media.counters.interrupts, 1);
  assertSingleSameSeqReplacement(ledger, media, state);
  assert.equal(state.pause_reason, "record_failed");
});

test("零替换（prompt_level 由 +0 漂移成 -0）：Object.is 拒绝，=== 的比较器会漏判", async () => {
  const drifted = mutableRecordCommand();
  drifted.prompt_level = -0;
  // 夹具自证：pending 侧仍是走过 reducer 的 +0，候选这一侧是 -0。
  assert.equal(Object.is(drifted.prompt_level, -0), true);
  assert.equal(Object.is(recordCommand().prompt_level, 0), true);

  const { sends, ledger, media, state } = await runReplacementAuthorityCase({
    identity: { command: drifted as unknown as NextCommandProjection },
  });

  assert.deepEqual(sends, []);
  assert.equal(media.counters.interrupts, 1);
  assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
});

test("零替换（真 delivery 的 WeakSet 认领成立、但候选 entry.ownerKey 被单独篡改）：delivery 自持 owner 独立于 error.identity.ownerKey，替换为 0", async () => {
  const events: string[] = [];
  const ledger = newReplacementLedger();
  const store = new ReplacementStore();
  const stageFailure = new Error("暂存事务中止（具名负例 4 哨兵）");
  const MUTATED_OWNER = "e".repeat(64);
  store.stageError = stageFailure;
  store.failStageFor = ["record_started"];
  const real = await DurableAutopilotAckDelivery.create({
    capability: REPLACEMENT_CAPABILITY,
    store,
    transport: {
      next: async () => null,
      ack: async (_sid, _key, ack) => {
        ledger.transport.push(`http:${ack.ack_type}`);
        return terminalReceipt(ack);
      },
    },
  });
  const media = interruptibleCapture(events);
  const controller = new PatientAutopilotController({
    sessionId: REPLACEMENT_SESSION,
    transport: {
      next: async () => recordCommand(),
      ack: async () => { throw new Error("持久 delivery 路径不应直连 transport.ack"); },
    },
    speech: { start: () => { throw new Error("不应播放"); } },
    recording: { start: () => media.capture },
    ackDelivery: recordingDurableDelegate(real, ledger),
    onDeviceEventSeq: (seq) => { ledger.seqAdvances.push(seq); },
    idempotencyKey: fixedAckKey,
  });
  let capturedEntry: AutopilotAckEnvelope | null = null;
  store.onStageMutate = (entry) => {
    if (entry.ack.ack_type !== "record_started") return;
    // 1. 变异前的精确候选快照，同时把真 envelope 与真 key 各记恰好一次。
    const before = deepClone(entry);
    capturedEntry = before;
    ledger.envelopes.push({
      ackType: before.ack.ack_type,
      deviceEventSeq: before.ack.device_event_seq,
      idempotencyKey: before.ack.idempotency_key,
    });
    ledger.keys.push(before.ack.idempotency_key);
    // 2. 只改这一个字段：session / command / ACK 与其余字段一律不动。
    entry.ownerKey = MUTATED_OWNER;
    // 3. 让同一条 capture 保留下来的 after_start 拥有物理丢弃。
    controller.handleRecordingLifecycleInterruption();
    // 4. 交回既有 stage 路径抛出那个确切的哨兵；事务未提交，snapshot 仍证明为空。
  };

  const state = await controller.pollOnce();
  ledger.stage.push(...store.events.filter((row) => row.startsWith("stage:")));
  ledger.complete.push(...store.events.filter((row) => row === "complete"));

  const before = capturedEntry as AutopilotAckEnvelope | null;
  assert.ok(before);
  const beforeAck = before.ack;
  if (beforeAck.ack_type !== "record_started") throw new Error("候选必须是 record_started");
  const failure = ledger.failure;
  if (!(failure instanceof AutopilotAckPersistenceError)) {
    throw new Error("delegate 必须原样重抛真 delivery 造的 typed 拒绝");
  }
  assert.ok(failure instanceof Error);
  assert.equal(failure.phase, "stage");
  assert.equal(failure.outcome, "confirmed_empty");
  assert.equal(failure.cause, stageFailure);          // 同一个哨兵对象，不是同形态副本
  // 控制器把同一个对象交给了恰好一次归属检查，结果为 false。
  assert.equal(ledger.ownershipChecks.length, 1);
  assert.equal(ledger.ownershipChecks[0]?.error, failure);
  assert.equal(ledger.ownershipChecks[0]?.result, false);
  assert.notEqual(MUTATED_OWNER, before.ownerKey);
  // 除那一个确定性改动的 owner 外，identity 逐字等于变异前的候选事实。
  assert.deepEqual(failure.identity, {
    ownerKey: MUTATED_OWNER,
    sessionId: before.sessionId,
    commandKey: before.commandKey,
    command: before.command,
    ack: before.ack,
    ackType: beforeAck.ack_type,
    idempotencyKey: beforeAck.idempotency_key,
    deviceEventSeq: beforeAck.device_event_seq,
    commandRevision: beforeAck.command_revision,
    controlGeneration: beforeAck.control_generation,
    runnerGeneration: beforeAck.runner_generation,
    mimeType: beforeAck.mime_type,
  });
  assert.equal(media.counters.interrupts, 1);
  assertZeroSameSeqReplacement(ledger, media, state, { cancels: 0 });
});
