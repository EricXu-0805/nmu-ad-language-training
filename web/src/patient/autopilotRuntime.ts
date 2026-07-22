import {
  parseAutopilotAck,
  parseNextCommandProjection,
  type AutopilotAck,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";

export type AutopilotRuntimePhase =
  | "waiting_command"
  | "tts_ready"
  | "tts_playing"
  | "waiting_server_after_tts"
  | "record_ready"
  | "recording"
  | "waiting_server_after_record"
  | "scope_completed"
  | "paused";

export type AutopilotPauseReason =
  | "protocol_violation"
  | "inflight_command_recovery_required"
  | "technical_failure"
  | "tts_failed"
  | "record_failed";

export interface AutopilotRuntimeState {
  phase: AutopilotRuntimePhase;
  command: NextCommandProjection | null;
  last_device_event_seq: number;
  last_ack: AutopilotAck | null;
  pause_reason: AutopilotPauseReason | null;
}

export type AutopilotRuntimeAction =
  | { type: "server_command"; command: unknown | null }
  | { type: "device_ack"; ack: unknown }
  | { type: "technical_failure" };

function pause(
  state: AutopilotRuntimeState,
  reason: AutopilotPauseReason,
): AutopilotRuntimeState {
  return { ...state, phase: "paused", pause_reason: reason };
}

function phaseForFreshCommand(command: NextCommandProjection): {
  phase: AutopilotRuntimePhase;
  pauseReason: AutopilotPauseReason | null;
} {
  if (command.state === "started") {
    // 刷新后浏览器无法证明旧播放/旧 MediaRecorder 仍连续存在。保留当前
    // command 供服务端恢复，但绝不伪造 ended/stopped 或重新开麦。
    return {
      phase: "paused",
      pauseReason: "inflight_command_recovery_required",
    };
  }
  return {
    phase: command.kind === "tts" ? "tts_ready" : "record_ready",
    pauseReason: null,
  };
}

/** Hydrate from the server's current-only /next projection after a refresh. */
export function restoreAutopilotRuntime(
  nextCommand: unknown | null,
  lastDeviceEventSeq = 0,
): AutopilotRuntimeState {
  if (!Number.isSafeInteger(lastDeviceEventSeq) || lastDeviceEventSeq < 0) {
    throw new Error("设备事件序号恢复值非法");
  }
  if (nextCommand === null) {
    return {
      phase: "waiting_command",
      command: null,
      last_device_event_seq: lastDeviceEventSeq,
      last_ack: null,
      pause_reason: null,
    };
  }
  const command = parseNextCommandProjection(nextCommand);
  const restored = phaseForFreshCommand(command);
  return {
    phase: restored.phase,
    command,
    last_device_event_seq: lastDeviceEventSeq,
    last_ack: null,
    pause_reason: restored.pauseReason,
  };
}

function sameImmutableCommand(
  before: NextCommandProjection,
  after: NextCommandProjection,
): boolean {
  const sameBase = before.schema_version === after.schema_version
    && before.command_key === after.command_key
    && before.command_seq === after.command_seq
    && before.kind === after.kind
    && before.control_generation === after.control_generation
    && before.runner_generation === after.runner_generation
    && before.item_ref === after.item_ref
    && before.turn_seq === after.turn_seq
    && before.attempt_seq === after.attempt_seq
    && before.prompt_level === after.prompt_level;
  if (!sameBase || before.kind !== after.kind) return false;
  if (before.kind === "tts" && after.kind === "tts") {
    return before.payload.speech_key === after.payload.speech_key
      && before.payload.speech_text === after.payload.speech_text
      && before.payload.purpose === after.payload.purpose;
  }
  if (before.kind === "record" && after.kind === "record") {
    return before.payload.raw_audio_id === after.payload.raw_audio_id
      && before.payload.turn_ref === after.payload.turn_ref
      && before.payload.max_duration_seconds === after.payload.max_duration_seconds
      && before.payload.contains_direct_identifier === after.payload.contains_direct_identifier
      && before.payload.presentation_speech_key === after.payload.presentation_speech_key
      && before.payload.presentation_speech_text === after.payload.presentation_speech_text
      && before.payload.presentation_purpose === after.payload.presentation_purpose;
  }
  return false;
}

function sameAck(before: AutopilotAck, after: AutopilotAck): boolean {
  const beforeRow = before as unknown as Record<string, unknown>;
  const afterRow = after as unknown as Record<string, unknown>;
  const beforeKeys = Object.keys(beforeRow).sort();
  const afterKeys = Object.keys(afterRow).sort();
  return beforeKeys.length === afterKeys.length
    && beforeKeys.every((key, index) => key === afterKeys[index]
      && beforeRow[key] === afterRow[key]);
}

function acceptSameCommand(
  state: AutopilotRuntimeState,
  next: NextCommandProjection,
): AutopilotRuntimeState {
  const current = state.command;
  if (!current || !sameImmutableCommand(current, next)
      || next.command_revision < current.command_revision
      || (current.state === "started" && next.state === "pending")) {
    return pause(state, "protocol_violation");
  }
  const sameState = next.state === current.state;
  if ((sameState && next.command_revision !== current.command_revision)
      || (!sameState && next.command_revision !== current.command_revision + 1)) {
    return pause(state, "protocol_violation");
  }
  if (!sameState) {
    const locallyObservedStart = current.kind === "tts"
      ? state.phase === "tts_playing" || state.phase === "waiting_server_after_tts"
      : state.phase === "recording" || state.phase === "waiting_server_after_record";
    if (!locallyObservedStart) return pause(state, "protocol_violation");
  }

  // 本机已经真实执行的媒体状态优先于轮询刚返回的 started 投影；终态 ACK
  // 之后迟到的 started 快照也不能把 reducer 顶回录音/播放中。
  return { ...state, command: next };
}

function expectedFollowup(
  current: NextCommandProjection,
  next: NextCommandProjection,
): boolean {
  if (next.command_seq !== current.command_seq + 1
      || next.control_generation !== current.control_generation
      || next.runner_generation !== current.runner_generation) return false;

  if (current.kind === "tts") {
    if (current.payload.purpose === "question" || current.payload.purpose === "cue") {
      return next.kind === "record"
        && next.item_ref === current.item_ref
        && next.turn_seq === current.turn_seq
        && next.attempt_seq === current.attempt_seq
        && next.prompt_level === current.prompt_level;
    }
    return next.kind === "tts";
  }
  return next.kind === "tts";
}

function acceptNewCommand(
  state: AutopilotRuntimeState,
  next: NextCommandProjection,
): AutopilotRuntimeState {
  const current = state.command;
  if (!current) {
    const fresh = phaseForFreshCommand(next);
    return { ...state, command: next, phase: fresh.phase, pause_reason: fresh.pauseReason };
  }
  const waiting = current.kind === "tts"
    ? state.phase === "waiting_server_after_tts"
    : state.phase === "waiting_server_after_record";
  if (!waiting || !expectedFollowup(current, next)) {
    return pause(state, "protocol_violation");
  }
  const fresh = phaseForFreshCommand(next);
  return { ...state, command: next, phase: fresh.phase, pause_reason: fresh.pauseReason };
}

function reduceServerCommand(
  state: AutopilotRuntimeState,
  value: unknown | null,
): AutopilotRuntimeState {
  if (state.phase === "paused" || state.phase === "scope_completed") return state;
  if (value === null) {
    if (state.command === null) return state;
    if (state.command.kind === "record"
        && state.phase === "waiting_server_after_record") return state;
    if (state.command.kind === "tts"
        && state.phase === "waiting_server_after_tts") {
      if (state.command.payload.purpose === "feedback"
          || state.command.payload.purpose === "tell_answer") {
        return { ...state, phase: "scope_completed", command: null };
      }
      return pause(state, "protocol_violation");
    }
    return pause(state, "protocol_violation");
  }
  let next: NextCommandProjection;
  try { next = parseNextCommandProjection(value); }
  catch { return pause(state, "protocol_violation"); }

  if (state.command?.command_key === next.command_key) {
    return acceptSameCommand(state, next);
  }
  return acceptNewCommand(state, next);
}

function ackMatchesCommand(ack: AutopilotAck, command: NextCommandProjection): boolean {
  return ack.control_generation === command.control_generation
    && ack.runner_generation === command.runner_generation
    && ack.command_revision === command.command_revision;
}

function reduceDeviceAck(
  state: AutopilotRuntimeState,
  value: unknown,
): AutopilotRuntimeState {
  let ack: AutopilotAck;
  try { ack = parseAutopilotAck(value); }
  catch { return pause(state, "protocol_violation"); }
  if (state.phase === "paused") return state;
  if (state.last_ack && ack.device_event_seq === state.last_device_event_seq
      && ack.idempotency_key === state.last_ack.idempotency_key) {
    return sameAck(state.last_ack, ack) ? state : pause(state, "protocol_violation");
  }
  if (!state.command || !ackMatchesCommand(ack, state.command)
      || ack.device_event_seq !== state.last_device_event_seq + 1) {
    return pause(state, "protocol_violation");
  }

  const advanced = {
    ...state,
    last_device_event_seq: ack.device_event_seq,
    last_ack: ack,
  };
  if (state.command.kind === "tts") {
    if (ack.ack_type === "tts_started" && state.phase === "tts_ready") {
      return { ...advanced, phase: "tts_playing" };
    }
    if (ack.ack_type === "tts_ended"
        && (state.phase === "tts_ready" || state.phase === "tts_playing")) {
      return { ...advanced, phase: "waiting_server_after_tts" };
    }
    if (ack.ack_type === "tts_failed"
        && (state.phase === "tts_ready" || state.phase === "tts_playing")) {
      return pause(advanced, "tts_failed");
    }
    return pause(advanced, "protocol_violation");
  }

  if (ack.ack_type === "record_started" && state.phase === "record_ready") {
    return { ...advanced, phase: "recording" };
  }
  if (ack.ack_type === "record_stopped"
      && (state.phase === "record_ready" || state.phase === "recording")) {
    if (ack.raw_audio_id !== state.command.payload.raw_audio_id) {
      return pause(advanced, "protocol_violation");
    }
    return { ...advanced, phase: "waiting_server_after_record" };
  }
  if (ack.ack_type === "record_failed"
      && (state.phase === "record_ready" || state.phase === "recording")) {
    return pause(advanced, "record_failed");
  }
  return pause(advanced, "protocol_violation");
}

/** Pure fail-closed reducer; media code may only act through the guards below. */
export function autopilotRuntimeReducer(
  state: AutopilotRuntimeState,
  action: AutopilotRuntimeAction,
): AutopilotRuntimeState {
  switch (action.type) {
    case "server_command": return reduceServerCommand(state, action.command);
    case "device_ack": return reduceDeviceAck(state, action.ack);
    case "technical_failure": return pause(state, "technical_failure");
  }
}

export function canPlayAutopilotSpeech(state: AutopilotRuntimeState): boolean {
  return state.phase === "tts_ready"
    && state.command?.kind === "tts"
    && state.command.state === "pending";
}

/** The only state from which future UI integration may call getUserMedia. */
export function canOpenAutopilotMicrophone(state: AutopilotRuntimeState): boolean {
  return state.phase === "record_ready"
    && state.command?.kind === "record"
    && state.command.state === "pending";
}
