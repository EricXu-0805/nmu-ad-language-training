import assert from "node:assert/strict";
import test from "node:test";
import type { DeviceCapabilityRecord } from "../security/deviceCapability.ts";
import {
  PatientAutopilotController,
  deviceCapabilityAllowsAutopilot,
  type AutopilotRecordingExecutor,
  type AutopilotSpeechExecutor,
  type AutopilotTransport,
} from "./autopilotController.ts";
import type { AutopilotAck, NextCommandProjection } from "./autopilotProtocol.ts";
import {
  admitCaptureBeforeDeadline,
  answerWindowMs,
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
          await admitCaptureBeforeDeadline(authorization, {
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

test("stopRecordingNow：仅 recording 阶段向当前 capture 转发 user_done", () => {
  const requestStopCalls: Array<"user_done" | undefined> = [];
  const capture = {
    started: Promise.resolve({ mime_type: "audio/webm" as const }),
    stopped: new Promise<never>(() => {}),
    closed: new Promise<void>(() => {}),
    requestStop: (reason?: "user_done") => { requestStopCalls.push(reason); },
    cancel: () => { throw new Error("stopRecordingNow 不应触发 cancel"); },
  };
  const recordingRuntime: AutopilotRuntimeState = {
    phase: "recording",
    command: recordCommand("started"),
    last_device_event_seq: 3,
    last_ack: null,
    pause_reason: null,
  };
  const controller = new PatientAutopilotController({
    sessionId: "S-STOP-NOW",
    ...inertControllerDeps(),
    initialDeviceEventSeq: 3,
    initialRuntime: recordingRuntime,
  });
  // 直接注入 activeMedia：这是控制器自身在真实 record 命令跑完 runOnce 后才会
  // 设置的私有字段。这里只钉住 stopRecordingNow 自己的判定边界（是否转发/
  // 转给谁），不重放 getUserMedia → audioSaved → record_stopped 整条链
  // （该链在 BrowserAutopilotCapture，未被这份测试覆盖，见下方 gap 说明）。
  (controller as unknown as { activeMedia: unknown }).activeMedia = capture;

  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, ["user_done"]);
  // stopRecordingNow 本身不改 phase；真实链路里 phase 要等 record_stopped
  // ACK 经 runOnce 回来才推进到 waiting_server_after_record。
  assert.equal(controller.state.phase, "recording");

  // 仍在 recording 阶段再次点击：controller 这一层逐次原样转发，不去重——
  // “最终只生效一次”的幂等保证在 BrowserAutopilotCapture.signalStop 自己的
  // requestedStop 闩门里（阅读该源码确认：先到者赢，之后同一实例上无论
  // requestStop/cancel/max_duration 定时器谁再调用 signalStop 都是纯 no-op），
  // 不是这个 controller 方法的契约，也未被这份测试直接验证（见下方 gap）。
  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, ["user_done", "user_done"]);

  // 阶段一旦不再是 recording，再点击就是纯无效操作：不再转发给旧 capture。
  (controller as unknown as { stateValue: AutopilotRuntimeState }).stateValue = {
    ...recordingRuntime,
    phase: "waiting_server_after_record",
  };
  controller.stopRecordingNow();
  assert.deepEqual(requestStopCalls, ["user_done", "user_done"]);
});

test("stopRecordingNow：非 recording 阶段（无 activeMedia）静默无效，不抛异常", () => {
  const controller = new PatientAutopilotController({
    sessionId: "S-STOP-NOW-IDLE",
    ...inertControllerDeps(),
  });
  assert.equal(controller.state.phase, "waiting_command");
  controller.stopRecordingNow();
  assert.equal(controller.state.phase, "waiting_command");
});
