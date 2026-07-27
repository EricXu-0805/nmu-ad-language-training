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
import { canOpenAutopilotMicrophone } from "./autopilotRuntime.ts";
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
