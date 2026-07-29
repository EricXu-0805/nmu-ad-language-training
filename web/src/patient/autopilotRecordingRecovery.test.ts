import assert from "node:assert/strict";
import test from "node:test";
import type { AudioOutboxEntry } from "../audio/audioOutbox.ts";
import type { AutopilotTerminalFailureLatch } from "./autopilotAckOutbox.ts";
import type { AutopilotAck, NextCommandProjection } from "./autopilotProtocol.ts";
import {
  reconcileAutopilotTerminalLatch,
  restoreAutopilotRuntimeAfterRefresh,
  runBestEffortCleanup,
  settleAutopilotRecordingRecovery,
  settleCaptureCleanup,
  type AutopilotRecoveryDependencies,
} from "./autopilotRecordingRecovery.ts";

const SESSION_ID = "S-ONE";
const RAW_AUDIO_ID = "raw-recovery-0001";
const CHECKSUM = "c".repeat(64);

type RecordCommand = Extract<NextCommandProjection, { kind: "record" }>;

/** 刷新后服务器给的就是 started revision（command_revision = 1）。 */
function startedRecordCommand(): RecordCommand {
  return {
    schema_version: 1,
    command_key: "cmd-record-recovery-0001",
    command_seq: 4,
    kind: "record",
    state: "started",
    command_revision: 1,
    control_generation: 3,
    runner_generation: 7,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      raw_audio_id: RAW_AUDIO_ID,
      turn_ref: "itm-0001#1",
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

function outboxEntry(durationSeconds: number): AudioOutboxEntry {
  return {
    schemaVersion: 1,
    rawAudioId: RAW_AUDIO_ID,
    sessionId: SESSION_ID,
    turnKey: "itm-0001#1",
    containsDirectIdentifier: false,
    durationSeconds,
    blobBytes: 4_096,
    mimeType: "audio/webm",
    phase: "captured",
    autopilotStopReason: "max_duration",
  } as unknown as AudioOutboxEntry;
}

function recoveryHarness(entry: AudioOutboxEntry | null, options: {
  initialDeviceEventSeq?: number;
  snapshotThrows?: boolean;
  invalidBlobKeyCount?: number;
  leaseReleaseThrows?: boolean;
  terminalFailureLatch?: AutopilotTerminalFailureLatch | null;
} = {}) {
  const calls: string[] = [];
  const sent: Array<{
    command: NextCommandProjection;
    seq: number;
    facts: Record<string, unknown>;
  }> = [];
  const initialDeviceEventSeq = options.initialDeviceEventSeq ?? 6;
  const deps: AutopilotRecoveryDependencies = {
    acquireLease: async () => {
      calls.push("lease:acquire");
      return {
        release: () => {
          calls.push("lease:release");
          if (options.leaseReleaseThrows) throw new Error("设备租约释放失败");
        },
        released: Promise.resolve(),
      } as unknown as Awaited<ReturnType<AutopilotRecoveryDependencies["acquireLease"]>>;
    },
    recoverySnapshot: async () => {
      calls.push("snapshot");
      if (options.snapshotThrows) throw new Error("本机恢复快照读不出来");
      return {
        entries: entry ? [entry] : [],
        invalidBlobKeyCount: options.invalidBlobKeyCount ?? 0,
        legacyOrphans: [],
      };
    },
    readBlob: async (rawAudioId) => {
      calls.push(`blob:read:${rawAudioId}`);
      return { size: 4_096, type: "audio/webm" } as unknown as Blob;
    },
    finish: async () => {
      // 这一步是唯一会 register / upload / putAudioSaved 的地方。
      calls.push("finish:register-upload-audioSaved");
      return {
        stop_reason: "max_duration" as const,
        raw_audio_id: RAW_AUDIO_ID,
        receipt_server_seq: 91,
        checksum: CHECKSUM,
        byte_count: 4_096,
        duration_seconds: 14.2,
      };
    },
  };
  const delivery = {
    initialDeviceEventSeq,
    terminalFailureLatch: options.terminalFailureLatch ?? null,
    send: async (
      command: NextCommandProjection,
      lastDeviceEventSeq: number,
      facts: Record<string, unknown>,
    ) => {
      sent.push({ command, seq: lastDeviceEventSeq, facts });
      // 与 DurableAutopilotAckDelivery 一致：回执绑当前命令的三元组，序号 +1。
      return {
        idempotency_key: `ack.recovery.${lastDeviceEventSeq + 1}.0001`,
        control_generation: command.control_generation,
        runner_generation: command.runner_generation,
        command_revision: command.command_revision,
        device_event_seq: lastDeviceEventSeq + 1,
        ...facts,
      } as unknown as AutopilotAck;
    },
  };
  const touchedAudio = () => calls.some((row) =>
    row.startsWith("blob:read") || row === "finish:register-upload-audioSaved");
  return { calls, sent, deps, delivery, touchedAudio };
}

test("恢复期本机时长顶穿命令上限：不读字节、不上传、不 audioSaved", async () => {
  const harness = recoveryHarness(outboxEntry(18.4));   // 上限 15 秒

  const settled = await settleAutopilotRecordingRecovery(
    SESSION_ID, startedRecordCommand(), harness.delivery, harness.deps);

  assert.equal(settled.outcome, "failed");
  // 闸排在读字节之前：blob 没被读，finish（register/upload/audioSaved）没被调。
  assert.deepEqual(harness.calls, ["lease:acquire", "snapshot", "lease:release"]);
  // 只发一条 record_failed，绝不伪造 record_stopped。
  assert.equal(harness.sent.length, 1);
  const [sent] = harness.sent;
  assert.deepEqual(sent.facts, {
    ack_type: "record_failed",
    error_code: "recording_runtime_failed",
  });
  // 必须回执 started 那一版本，否则服务器认不出这条在途命令。
  assert.equal(sent.command.state, "started");
  assert.equal(sent.command.command_revision, 1);
  assert.equal(sent.seq, 6);
});

test("恢复期时长读数是 NaN / Infinity / 负数时同样只发 record_failed", async () => {
  for (const durationSeconds of [
    Number.NaN, Number.POSITIVE_INFINITY, -1, 15.000_001,
  ]) {
    const harness = recoveryHarness(outboxEntry(durationSeconds));
    const settled = await settleAutopilotRecordingRecovery(
      SESSION_ID, startedRecordCommand(), harness.delivery, harness.deps);
    assert.equal(settled.outcome, "failed", `${durationSeconds}`);
    assert.equal(harness.touchedAudio(), false, `${durationSeconds}`);
    assert.equal(harness.sent.length, 1);
  }
});

test("设备租约释放抛错不得盖掉超限判定，record_failed 照样稳定发出", async () => {
  const harness = recoveryHarness(outboxEntry(18.4), { leaseReleaseThrows: true });

  const settled = await settleAutopilotRecordingRecovery(
    SESSION_ID, startedRecordCommand(), harness.delivery, harness.deps);

  assert.equal(settled.outcome, "failed");
  assert.equal(harness.touchedAudio(), false);
  assert.deepEqual(harness.sent[0].facts, {
    ack_type: "record_failed",
    error_code: "recording_runtime_failed",
  });
});

test("恢复期时长合规时照旧补 record_stopped，走完上传与 audioSaved", async () => {
  const harness = recoveryHarness(outboxEntry(14.2));

  const settled = await settleAutopilotRecordingRecovery(
    SESSION_ID, startedRecordCommand(), harness.delivery, harness.deps);

  assert.equal(settled.outcome, "stopped");
  assert.deepEqual(harness.calls, [
    "lease:acquire",
    "snapshot",
    `blob:read:${RAW_AUDIO_ID}`,
    "finish:register-upload-audioSaved",
    "lease:release",
  ]);
  assert.equal(harness.sent.length, 1);
  const facts = harness.sent[0].facts as { ack_type: string; duration_seconds: number };
  assert.equal(facts.ack_type, "record_stopped");
  assert.equal(facts.duration_seconds, 14.2);
});

test("本机没有待恢复录音时既不上传也不发任何回执", async () => {
  const harness = recoveryHarness(null);

  const settled = await settleAutopilotRecordingRecovery(
    SESSION_ID, startedRecordCommand(), harness.delivery, harness.deps);

  assert.equal(settled.outcome, "none");
  assert.deepEqual(harness.sent, []);
  assert.equal(harness.touchedAudio(), false);
});

test("恢复期的其它硬错误照旧上抛，不被当成时长超限吞掉", async () => {
  const harness = recoveryHarness(outboxEntry(14.2));
  const deps: AutopilotRecoveryDependencies = {
    ...harness.deps,
    readBlob: async () => null,          // 本机字节丢了
  };
  await assert.rejects(
    settleAutopilotRecordingRecovery(
      SESSION_ID, startedRecordCommand(), harness.delivery, deps),
    /恢复录音缺少本机原始字节/);
  assert.deepEqual(harness.sent, []);
});

// ---------------- 刷新后的 runtime 合成 ----------------

test("刷新恢复：超限录音发出 record_failed 后，/next=null 冲不掉暂停事实", async () => {
  const harness = recoveryHarness(outboxEntry(18.4));
  const projections: Array<unknown | null> = [startedRecordCommand(), null];

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => projections.shift() ?? null,
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "paused");
  assert.equal(runtime.pause_reason, "record_failed");   // 不是 waiting_command
  assert.equal(runtime.command?.kind, "record");         // 命令证据保留
  assert.equal(runtime.command?.state, "started");
  assert.equal(runtime.last_device_event_seq, 7);
  assert.equal(runtime.last_ack?.ack_type, "record_failed");
  assert.equal(harness.touchedAudio(), false);
});

function failureEnvelope(input: {
  command: NextCommandProjection;
  ackType: "tts_failed" | "record_failed";
  errorCode: string;
  deviceEventSeq: number;
}) {
  return {
    command: input.command,
    ack: {
      idempotency_key: `ack.${input.ackType}.${input.deviceEventSeq}.0001`,
      ack_type: input.ackType,
      error_code: input.errorCode,
      control_generation: input.command.control_generation,
      runner_generation: input.command.runner_generation,
      command_revision: input.command.command_revision,
      device_event_seq: input.deviceEventSeq,
    } as unknown as AutopilotAck,
  };
}

function pendingTtsCommand(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-question-recovery-0003",
    command_seq: 3,
    kind: "tts",
    state: "pending",
    command_revision: 0,
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

function pendingRecordCommand(): NextCommandProjection {
  return { ...startedRecordCommand(), state: "pending", command_revision: 0 };
}

// 响应丢失后重启：drain 掉的可能是任一条持久失败回执。服务器都已经暂停、
// /next 都是 null，而 pending 那两种根本没有音频 outbox。
for (const scenario of [
  {
    name: "tts_failed（pending 版本，播放启动失败）",
    command: pendingTtsCommand(),
    ackType: "tts_failed" as const,
    errorCode: "audio_playback_failed",
    pauseReason: "tts_failed",
  },
  {
    name: "record_failed（pending 版本，麦克风被拒）",
    command: pendingRecordCommand(),
    ackType: "record_failed" as const,
    errorCode: "microphone_denied",
    pauseReason: "record_failed",
  },
  {
    name: "record_failed（started 版本，本机时长超限）",
    command: startedRecordCommand(),
    ackType: "record_failed" as const,
    errorCode: "recording_runtime_failed",
    pauseReason: "record_failed",
  },
  {
    name: "tts_failed（started 版本，播放中途失败）",
    command: { ...pendingTtsCommand(), state: "started" as const, command_revision: 1 },
    ackType: "tts_failed" as const,
    errorCode: "audio_playback_failed",
    pauseReason: "tts_failed",
  },
]) {
  test(`响应丢失后重启：重放 ${scenario.name} 仍然是 paused，不是 waiting_command`, async () => {
    // pending 两种场景、以及 tts 的 started 场景，本机都没有任何待处置录音。
    const hasOutbox = scenario.command.kind === "record" && scenario.command.state === "started";
    const harness = recoveryHarness(
      hasOutbox ? outboxEntry(18.4) : null, { initialDeviceEventSeq: 7 });
    const drained = failureEnvelope({
      command: scenario.command,
      ackType: scenario.ackType,
      errorCode: scenario.errorCode,
      deviceEventSeq: 7,
    });

    const runtime = await restoreAutopilotRuntimeAfterRefresh({
      sessionId: SESSION_ID,
      next: async () => null,
      delivery: harness.delivery,
      deps: harness.deps,
      drained,
    });

    assert.equal(runtime.phase, "paused");
    assert.equal(runtime.pause_reason, scenario.pauseReason);
    assert.equal(runtime.command?.command_key, scenario.command.command_key);
    assert.equal(runtime.command?.state, scenario.command.state);
    assert.equal(runtime.last_device_event_seq, 7);
    assert.equal(runtime.last_ack?.ack_type, scenario.ackType);
    // 重放不得再碰音频，也不得补第二条回执。
    assert.equal(harness.touchedAudio(), false);
    assert.deepEqual(harness.sent, []);
  });
}

test("重放的不是失败回执时走原路径，不被伪造成暂停", async () => {
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 7 });
  const command = pendingTtsCommand();
  const drained = {
    command,
    ack: {
      idempotency_key: "ack.tts-started.7.0001",
      ack_type: "tts_started",
      control_generation: command.control_generation,
      runner_generation: command.runner_generation,
      command_revision: command.command_revision,
      device_event_seq: 7,
    } as unknown as AutopilotAck,
  };

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => null,
    delivery: harness.delivery,
    deps: harness.deps,
    drained,
  });

  assert.equal(runtime.phase, "waiting_command");
  assert.equal(runtime.pause_reason, null);
});

test("失败回执与 envelope 里的命令对不上时不重建暂停", async () => {
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 7 });
  const command = pendingTtsCommand();
  const mismatched = failureEnvelope({
    command, ackType: "record_failed", errorCode: "microphone_denied",
    deviceEventSeq: 7,
  });                                            // record_failed 配 tts 命令

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => null,
    delivery: harness.delivery,
    deps: harness.deps,
    drained: mismatched,
  });

  assert.equal(runtime.phase, "waiting_command");
});

// ---------------- 持久终态失败 latch（ACK 已 complete，checkpoint 里没有 pending） ----------------

function terminalLatch(input: {
  command: NextCommandProjection;
  ackType: "tts_failed" | "record_failed";
  deviceEventSeq: number;
}): AutopilotTerminalFailureLatch {
  return {
    commandKey: input.command.command_key,
    command: input.command,
    ack: {
      idempotency_key: `ack.${input.ackType}.${input.deviceEventSeq}.latch`,
      ack_type: input.ackType,
      error_code: "device_runtime_failed",
      control_generation: input.command.control_generation,
      runner_generation: input.command.runner_generation,
      command_revision: input.command.command_revision,
      device_event_seq: input.deviceEventSeq,
    } as unknown as AutopilotAck,
  };
}

/** 严格新权威：key 不同、seq 严格更大、两代都严格更大。command_revision 不参与比较。 */
function strictlyNewerCommand(base: NextCommandProjection): NextCommandProjection {
  return {
    ...base,
    command_key: `${base.command_key}-newer`,
    command_seq: base.command_seq + 1,
    control_generation: base.control_generation + 1,
    runner_generation: base.runner_generation + 1,
    command_revision: 0,
  };
}

for (const scenario of [
  { name: "pending tts_failed", command: pendingTtsCommand(), ackType: "tts_failed" as const },
  { name: "pending record_failed", command: pendingRecordCommand(), ackType: "record_failed" as const },
  { name: "started record_failed", command: startedRecordCommand(), ackType: "record_failed" as const },
  {
    name: "started tts_failed",
    command: { ...pendingTtsCommand(), state: "started" as const, command_revision: 1 },
    ackType: "tts_failed" as const,
  },
]) {
  test(`reconcileAutopilotTerminalLatch：${scenario.name} 的 latch 在 /next=null 时保持 paused`, () => {
    const latch = terminalLatch({ command: scenario.command, ackType: scenario.ackType, deviceEventSeq: 9 });
    const held = reconcileAutopilotTerminalLatch(latch, null);
    assert.ok(held);
    assert.equal(held.phase, "paused");
    assert.equal(held.pause_reason, scenario.ackType);
    assert.equal(held.command?.command_key, scenario.command.command_key);
    assert.equal(held.last_ack?.ack_type, scenario.ackType);
  });

  test(`二次刷新（无 drained）：${scenario.name} 的 latch 不被无凭据的重取冲成 waiting_command`, async () => {
    const latch = terminalLatch({ command: scenario.command, ackType: scenario.ackType, deviceEventSeq: 9 });
    const harness = recoveryHarness(null, { initialDeviceEventSeq: 9, terminalFailureLatch: latch });

    const runtime = await restoreAutopilotRuntimeAfterRefresh({
      sessionId: SESSION_ID,
      next: async () => null,
      delivery: harness.delivery,
      deps: harness.deps,
    });

    assert.equal(runtime.phase, "paused");
    assert.equal(runtime.pause_reason, scenario.ackType);
    assert.equal(runtime.command?.command_key, scenario.command.command_key);
    assert.deepEqual(harness.sent, []);
    assert.equal(harness.touchedAudio(), false);
  });
}

test("latch 存在时，/next 返回同一条命令（重复投影）不解闩", () => {
  const command = pendingTtsCommand();
  const latch = terminalLatch({ command, ackType: "tts_failed", deviceEventSeq: 9 });
  const held = reconcileAutopilotTerminalLatch(latch, command);
  assert.ok(held);
  assert.equal(held.phase, "paused");
});

test("latch 存在时，/next 返回畸形投影不解闩", () => {
  const latch = terminalLatch({ command: pendingTtsCommand(), ackType: "tts_failed", deviceEventSeq: 9 });
  const held = reconcileAutopilotTerminalLatch(latch, { not: "a command" });
  assert.ok(held);
  assert.equal(held.phase, "paused");
});

test("latch 存在时，seq 相同或回退、或只有一代前进，均不构成新权威", () => {
  const command = pendingTtsCommand();
  const latch = terminalLatch({ command, ackType: "tts_failed", deviceEventSeq: 9 });
  const sameSeq = { ...strictlyNewerCommand(command), command_seq: command.command_seq };
  const regressedSeq = { ...strictlyNewerCommand(command), command_seq: command.command_seq - 1 };
  const onlyControlGenBumped = { ...strictlyNewerCommand(command), runner_generation: command.runner_generation };
  const onlyRunnerGenBumped = { ...strictlyNewerCommand(command), control_generation: command.control_generation };
  for (const candidate of [sameSeq, regressedSeq, onlyControlGenBumped, onlyRunnerGenBumped]) {
    assert.ok(reconcileAutopilotTerminalLatch(latch, candidate), JSON.stringify(candidate));
  }
});

test("latch 存在时，command_revision 差异不参与解闩判据", () => {
  const command = pendingTtsCommand();
  const latch = terminalLatch({ command, ackType: "tts_failed", deviceEventSeq: 9 });
  // 同 key、同代际、同 seq，只有 revision 不同：仍然不是新命令，维持锁存。
  const sameCommandDifferentRevision = { ...command, command_revision: command.command_revision + 5 };
  assert.ok(reconcileAutopilotTerminalLatch(latch, sameCommandDifferentRevision));
});

test("latch 存在但 /next 证明严格新权威：pending 新命令解闩并按新鲜投影恢复", async () => {
  const oldCommand = pendingTtsCommand();
  const newCommand = strictlyNewerCommand(oldCommand);
  const latch = terminalLatch({ command: oldCommand, ackType: "tts_failed", deviceEventSeq: 9 });
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 9, terminalFailureLatch: latch });

  const held = reconcileAutopilotTerminalLatch(latch, newCommand);
  assert.equal(held, null);   // 纯函数层面：确实解闩

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => newCommand,
    delivery: harness.delivery,
    deps: harness.deps,
  });
  assert.equal(runtime.phase, "tts_ready");
  assert.equal(runtime.command?.command_key, newCommand.command_key);
  assert.equal(runtime.pause_reason, null);
});

test("latch 存在但 /next 的新权威命令是 started：只能落 inflight recovery/paused，不开媒体", async () => {
  const oldCommand = pendingRecordCommand();
  const newCommand = { ...strictlyNewerCommand(oldCommand), state: "started" as const };
  const latch = terminalLatch({ command: oldCommand, ackType: "record_failed", deviceEventSeq: 9 });
  // 新命令没有对应的本机 outbox：hydrateIdleRuntime 之外，恢复链会先尝试 settleAutopilotRecordingRecovery，
  // 但 recoverySnapshot 为空 → outcome "none" → 落回 restoreAutopilotRuntime 的 started 分支。
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 9, terminalFailureLatch: latch });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => newCommand,
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "paused");
  assert.equal(runtime.pause_reason, "inflight_command_recovery_required");
  assert.equal(runtime.command?.command_key, newCommand.command_key);
  assert.deepEqual(harness.sent, []);   // 绝不因为"解闩了"就自行开麦/伪造回执
});

test("A：durable latch 存在时，/next 返回畸形投影，完整恢复函数不抛错、保持 exact paused、零音频/ACK", async () => {
  // restoreAutopilotRuntime(current) 一读到畸形投影就会抛错；latch 判定必须排在
  // 它前面，否则这次刷新会直接异常退出（Hook 那边会落进 blocked），而不是保住
  // 已确认的暂停事实。
  const command = pendingTtsCommand();
  const latch = terminalLatch({ command, ackType: "tts_failed", deviceEventSeq: 9 });
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 9, terminalFailureLatch: latch });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => ({ this: "is not a valid projection" }),
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "paused");
  assert.equal(runtime.pause_reason, "tts_failed");
  assert.equal(runtime.command?.command_key, command.command_key);
  assert.deepEqual(harness.sent, []);
  assert.equal(harness.touchedAudio(), false);
});

test("B：同一次刷新里，durable latch 判定严格新权威时，过时的 drained 失败回执被忽略，改用新投影", async () => {
  const oldCommand = pendingTtsCommand();
  const newCommand = strictlyNewerCommand(oldCommand);
  const latch = terminalLatch({ command: oldCommand, ackType: "tts_failed", deviceEventSeq: 9 });
  // drained 代表"这次启动重放了同一条旧失败回执"——生产环境里 drainPending
  // 成功后 delivery.latch 必然已经反映它；这里显式把两者都摆出来，验证 latch
  // 判定为新权威时，drained 不得把恢复结果拉回旧命令的 paused。
  const drained = { command: oldCommand, ack: latch.ack };
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 9, terminalFailureLatch: latch });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => newCommand,
    delivery: harness.delivery,
    deps: harness.deps,
    drained,
  });

  assert.equal(runtime.phase, "tts_ready");
  assert.equal(runtime.command?.command_key, newCommand.command_key);
  assert.equal(runtime.pause_reason, null);
  assert.deepEqual(harness.sent, []);
});

test("二次刷新：ACK 已 complete、服务器 null、outbox 仍在 → 仍然 paused", async () => {
  // 第一次就发成功的 record_failed 会把 ACK envelope complete 掉，但音频 outbox
  // 按设计保留。此时既没有 pending ACK，服务器也没有命令。
  const harness = recoveryHarness(outboxEntry(18.4), { initialDeviceEventSeq: 7 });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => null,
    delivery: harness.delivery,
    deps: harness.deps,
    drained: null,
  });

  assert.equal(runtime.phase, "paused");
  assert.equal(runtime.pause_reason, "inflight_command_recovery_required");
  // 没有 exact ACK 就不伪造 record_failed，也不再开媒体、不上传。
  assert.equal(runtime.last_ack, null);
  assert.deepEqual(harness.sent, []);
  assert.equal(harness.touchedAudio(), false);
  assert.ok(!harness.calls.includes("lease:acquire"));   // 麦克风租约都没碰
});

test("二次刷新：本机恢复状态非法或读不出，一律 paused(technical_failure)", async () => {
  for (const options of [{ invalidBlobKeyCount: 1 }, { snapshotThrows: true }]) {
    const harness = recoveryHarness(null, { ...options, initialDeviceEventSeq: 7 });
    const runtime = await restoreAutopilotRuntimeAfterRefresh({
      sessionId: SESSION_ID,
      next: async () => null,
      delivery: harness.delivery,
      deps: harness.deps,
    });
    assert.equal(runtime.phase, "paused");
    assert.equal(runtime.pause_reason, "technical_failure");
    assert.deepEqual(harness.sent, []);
    assert.equal(harness.touchedAudio(), false);
  }
});

test("本机干净且服务器无命令时，照旧是普通的 waiting_command", async () => {
  const harness = recoveryHarness(null, { initialDeviceEventSeq: 7 });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => null,
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "waiting_command");
  assert.equal(runtime.command, null);
  assert.equal(runtime.last_device_event_seq, 7);
});

test("其它场次遗留的 outbox 不把本场次锁成暂停", async () => {
  const foreign = {
    ...outboxEntry(18.4), sessionId: "S-OTHER",
  } as unknown as AudioOutboxEntry;
  const harness = recoveryHarness(foreign, { initialDeviceEventSeq: 7 });

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => null,
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "waiting_command");
});

test("刷新恢复：时长合规时照旧推进到服务器给的下一条命令", async () => {
  const harness = recoveryHarness(outboxEntry(14.2));
  const feedback = {
    schema_version: 1,
    command_key: "cmd-feedback-recovery-0002",
    command_seq: 5,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 3,
    runner_generation: 7,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      speech_key: "wk2.01.success",
      speech_text: "非常好，我们继续下一个。",
      purpose: "feedback",
    },
  };
  const projections: Array<unknown | null> = [startedRecordCommand(), feedback];

  const runtime = await restoreAutopilotRuntimeAfterRefresh({
    sessionId: SESSION_ID,
    next: async () => projections.shift() ?? null,
    delivery: harness.delivery,
    deps: harness.deps,
  });

  assert.equal(runtime.phase, "tts_ready");
  assert.equal(runtime.command?.command_key, "cmd-feedback-recovery-0002");
  assert.equal(harness.sent[0].facts.ack_type, "record_stopped");
});

// ---------------- 收尾 ----------------

test("capture 收尾：任一步抛错都不阻断后续步骤，closed 一定 settle", async () => {
  const done: string[] = [];
  let closed = false;

  await settleCaptureCleanup([
    () => { done.push("clear-timer"); throw new Error("定时器句柄已失效"); },
    () => { done.push("dispose"); throw new Error("设备已被系统回收"); },
    () => { done.push("lease-release"); },
    () => Promise.reject(new Error("租约释放回执丢失")),
    () => { done.push("stream-end"); },
  ], () => { closed = true; });

  assert.deepEqual(done, ["clear-timer", "dispose", "lease-release", "stream-end"]);
  assert.equal(closed, true);
});

test("capture 收尾：dispose 抛错也必须继续尽力释放设备租约", async () => {
  let released = false;
  let closed = false;

  await settleCaptureCleanup([
    () => { throw new Error("dispose 失败"); },
    () => { released = true; },
  ], () => { closed = true; });

  assert.equal(released, true);
  assert.equal(closed, true);
});

test("best-effort 清理本身不外抛", async () => {
  await assert.doesNotReject(runBestEffortCleanup([
    () => { throw new Error("同步失败"); },
    () => Promise.reject(new Error("异步失败")),
  ]));
});
