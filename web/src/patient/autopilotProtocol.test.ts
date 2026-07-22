import assert from "node:assert/strict";
import test from "node:test";
import { decodeJsonApiResponse } from "../apiResponse.ts";
import {
  buildAutopilotAck,
  parseAutopilotAck,
  parseNextCommandProjection,
  type NextCommandProjection,
} from "./autopilotProtocol.ts";
import {
  autopilotRuntimeReducer,
  canOpenAutopilotMicrophone,
  canPlayAutopilotSpeech,
  restoreAutopilotRuntime,
} from "./autopilotRuntime.ts";

const CHECKSUM = "a".repeat(64);

function questionCommand(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-question-0001",
    command_seq: 1,
    kind: "tts",
    state: "pending",
    command_revision: 0,
    control_generation: 2,
    runner_generation: 4,
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

function recordCommand(): NextCommandProjection {
  return {
    schema_version: 1,
    command_key: "cmd-recording-0002",
    command_seq: 2,
    kind: "record",
    state: "pending",
    command_revision: 0,
    control_generation: 2,
    runner_generation: 4,
    item_ref: "itm-0001",
    turn_seq: 1,
    attempt_seq: 1,
    prompt_level: 0,
    payload: {
      raw_audio_id: "raw-server-issued-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 12,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请说出图片中的物品。",
      presentation_purpose: "question",
    },
  };
}

function feedbackCommand(): NextCommandProjection {
  return {
    ...questionCommand(),
    command_key: "cmd-feedback-0003",
    command_seq: 3,
    attempt_seq: 2,
    payload: {
      speech_key: "wk2.01.feedback",
      speech_text: "回答正确。",
      purpose: "feedback",
    },
  };
}

function dispatch(state: ReturnType<typeof restoreAutopilotRuntime>, action: Parameters<typeof autopilotRuntimeReducer>[1]) {
  return autopilotRuntimeReducer(state, action);
}

test("strict command parser accepts only the backend's exact TTS and record projections", () => {
  const parsedQuestion = parseNextCommandProjection(questionCommand());
  assert.deepEqual(parsedQuestion, questionCommand());
  assert.equal(Object.hasOwn(parsedQuestion, "image_id"), false);
  assert.deepEqual(parseNextCommandProjection(recordCommand()), recordCommand());
});

test("command parser rejects aliases and outer or payload answer leakage", () => {
  const question = questionCommand();
  const { command_seq: _removed, ...withoutSequence } = question;
  assert.throws(() => parseNextCommandProjection({ ...withoutSequence, seq: 1 }));
  assert.throws(() => parseNextCommandProjection({ ...question, future_answer: "苹果" }));
  for (const leaked of [
    { image_id: "wk2-01" },
    { target_word: "苹果" },
    { cue_text: "一种水果" },
    { tell_answer: "这是苹果" },
  ]) {
    assert.throws(() => parseNextCommandProjection({ ...question, ...leaked }));
  }
  assert.throws(() => parseNextCommandProjection({
    ...question,
    payload: { ...question.payload, target_word: "苹果" },
  }));
  assert.throws(() => parseNextCommandProjection({
    ...question,
    payload: { speech_key: "wk2.01.question", text: "旧字段", purpose: "question" },
  }));
});

test("command parser binds purpose and opaque record turn to the current position", () => {
  const question = questionCommand();
  assert.throws(() => parseNextCommandProjection({
    ...question,
    prompt_level: 1,
  }));
  const record = recordCommand();
  assert.throws(() => parseNextCommandProjection({
    ...record,
    payload: { ...record.payload, turn_ref: "itm-0002#1" },
  }));
  assert.throws(() => parseNextCommandProjection({
    ...record,
    payload: { ...record.payload, max_duration_seconds: 301 },
  }));
  const { presentation_speech_text: _text, ...withoutPresentationText } = record.payload;
  assert.throws(() => parseNextCommandProjection({
    ...record,
    payload: withoutPresentationText,
  }));
  assert.throws(() => parseNextCommandProjection({
    ...record,
    payload: { ...record.payload, presentation_purpose: "feedback" },
  }));
  assert.throws(() => parseNextCommandProjection({
    ...record,
    prompt_level: 1,
    payload: { ...record.payload, presentation_purpose: "question" },
  }));
});

test("ACK parser requires tts_ended=true and rejects cross-event facts", () => {
  const ended = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-ended-0001",
    { ack_type: "tts_ended", media_ended: true, media_duration_ms: 1_200 },
  );
  assert.deepEqual(parseAutopilotAck(ended), ended);
  assert.throws(() => parseAutopilotAck({ ...ended, media_ended: false }));
  assert.throws(() => parseAutopilotAck({ ...ended, raw_audio_id: "raw-wrong" }));
  assert.throws(() => parseAutopilotAck({ ...ended, schema_version: 1 }));
});

test("record_started accepts only the closed MIME set", () => {
  const started = buildAutopilotAck(
    recordCommand(), 8, "evt-rec-start-0009",
    {
      ack_type: "record_started",
      mime_type: "audio/webm;codecs=opus",
      sample_rate_hz: 48_000,
      channels: 1,
    },
  );
  assert.equal(started.device_event_seq, 9);
  assert.deepEqual(parseAutopilotAck(started), started);
  assert.throws(() => parseAutopilotAck({ ...started, mime_type: "audio/wav" }));
});

test("record_stopped requires the complete exact server receipt tuple", () => {
  const stopped = buildAutopilotAck(
    recordCommand(), 9, "evt-rec-stop-0010",
    {
      ack_type: "record_stopped",
      stop_reason: "silence",
      raw_audio_id: "raw-server-issued-0001",
      receipt_server_seq: 27,
      checksum: CHECKSUM,
      byte_count: 8_192,
      duration_seconds: 4.25,
    },
  );
  assert.deepEqual(parseAutopilotAck(stopped), stopped);
  const { receipt_server_seq: _missing, ...withoutReceipt } = stopped;
  assert.throws(() => parseAutopilotAck(withoutReceipt));
  assert.throws(() => parseAutopilotAck({ ...stopped, checksum: CHECKSUM.toUpperCase() }));
  assert.throws(() => parseAutopilotAck({ ...stopped, stop_reason: "operator" }));
});

test("failure ACKs use a closed machine-code vocabulary", () => {
  const failure = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-failed-0001",
    { ack_type: "tts_failed", error_code: "audio_playback_failed" },
  );
  assert.deepEqual(parseAutopilotAck(failure), failure);
  assert.throws(() => parseAutopilotAck({ ...failure, error_code: "unknown_failure" }));
});

test("JSON response can hydrate a pending current command after refresh", () => {
  const decoded = decodeJsonApiResponse({
    status: 200,
    ok: true,
    text: JSON.stringify(recordCommand()),
  });
  const state = restoreAutopilotRuntime(decoded, 12);
  assert.equal(state.phase, "record_ready");
  assert.equal(state.last_device_event_seq, 12);
  assert.equal(canOpenAutopilotMicrophone(state), true);
});

test("refresh never pretends an in-flight media command is still continuous", () => {
  const tts = restoreAutopilotRuntime({
    ...questionCommand(), state: "started", command_revision: 1,
  });
  const record = restoreAutopilotRuntime({
    ...recordCommand(), state: "started", command_revision: 1,
  });
  assert.equal(tts.phase, "paused");
  assert.equal(record.phase, "paused");
  assert.equal(canPlayAutopilotSpeech(tts), false);
  assert.equal(canOpenAutopilotMicrophone(record), false);
});

test("TTS start alone cannot admit a server record command", () => {
  let state = restoreAutopilotRuntime(questionCommand());
  const started = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-started-0001", { ack_type: "tts_started" },
  );
  state = dispatch(state, { type: "device_ack", ack: started });
  assert.equal(state.phase, "tts_playing");
  assert.equal(canOpenAutopilotMicrophone(state), false);
  state = dispatch(state, { type: "server_command", command: recordCommand() });
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "protocol_violation");
});

test("TTS ended waits for a server record command before opening the microphone", () => {
  let state = restoreAutopilotRuntime(questionCommand());
  const ended = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-ended-0001",
    { ack_type: "tts_ended", media_ended: true },
  );
  state = dispatch(state, { type: "device_ack", ack: ended });
  assert.equal(state.phase, "waiting_server_after_tts");
  assert.equal(canOpenAutopilotMicrophone(state), false);
  state = dispatch(state, { type: "server_command", command: recordCommand() });
  assert.equal(state.phase, "record_ready");
  assert.equal(canOpenAutopilotMicrophone(state), true);
});

test("the full question-to-record flow echoes server revisions and preserves receipt order", () => {
  let state = restoreAutopilotRuntime(questionCommand());
  const ttsStarted = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-started-0001", { ack_type: "tts_started" },
  );
  state = dispatch(state, { type: "device_ack", ack: ttsStarted });
  state = dispatch(state, {
    type: "server_command",
    command: { ...questionCommand(), state: "started", command_revision: 1 },
  });
  assert.equal(state.phase, "tts_playing");
  assert.equal(state.command?.command_revision, 1);

  const ttsEnded = buildAutopilotAck(
    state.command!, state.last_device_event_seq, "evt-tts-ended-0002",
    { ack_type: "tts_ended", media_ended: true, media_duration_ms: 950 },
  );
  state = dispatch(state, { type: "device_ack", ack: ttsEnded });
  state = dispatch(state, { type: "server_command", command: recordCommand() });

  const recordStarted = buildAutopilotAck(
    state.command!, state.last_device_event_seq, "evt-rec-started-0003",
    { ack_type: "record_started", mime_type: "audio/webm", channels: 1 },
  );
  state = dispatch(state, { type: "device_ack", ack: recordStarted });
  state = dispatch(state, {
    type: "server_command",
    command: { ...recordCommand(), state: "started", command_revision: 1 },
  });
  assert.equal(state.phase, "recording");

  const recordStopped = buildAutopilotAck(
    state.command!, state.last_device_event_seq, "evt-rec-stopped-0004",
    {
      ack_type: "record_stopped",
      stop_reason: "user_done",
      raw_audio_id: "raw-server-issued-0001",
      receipt_server_seq: 44,
      checksum: CHECKSUM,
      byte_count: 12_345,
      duration_seconds: 3.5,
    },
  );
  state = dispatch(state, { type: "device_ack", ack: recordStopped });
  assert.equal(state.phase, "waiting_server_after_record");
  assert.equal(state.last_device_event_seq, 4);
  assert.equal(canOpenAutopilotMicrophone(state), false);
});

test("device_event_seq gaps, reorder, and same-sequence mutation fail closed", () => {
  const command = questionCommand();
  let state = restoreAutopilotRuntime(command, 4);
  const gap = buildAutopilotAck(
    command, 5, "evt-sequence-gap-0006", { ack_type: "tts_started" },
  );
  state = dispatch(state, { type: "device_ack", ack: gap });
  assert.equal(state.phase, "paused");

  let replayState = restoreAutopilotRuntime(command);
  const first = buildAutopilotAck(
    command, 0, "evt-replay-safe-0001", { ack_type: "tts_started" },
  );
  replayState = dispatch(replayState, { type: "device_ack", ack: first });
  const reordered = Object.fromEntries(Object.entries(first).reverse());
  assert.equal(dispatch(replayState, { type: "device_ack", ack: reordered }), replayState);
  const mutated = { ...first, idempotency_key: "evt-replay-other-0001" };
  replayState = dispatch(replayState, { type: "device_ack", ack: mutated });
  assert.equal(replayState.phase, "paused");
});

test("wrong command revision or generation cannot move the client state", () => {
  const command = questionCommand();
  const ended = buildAutopilotAck(
    command, 0, "evt-wrong-revision-01", { ack_type: "tts_ended", media_ended: true },
  );
  const state = dispatch(restoreAutopilotRuntime(command), {
    type: "device_ack",
    ack: { ...ended, command_revision: 2 },
  });
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "protocol_violation");
});

test("same command polling accepts only the exact next lifecycle revision", () => {
  const state = restoreAutopilotRuntime(questionCommand());
  const unobservedStart = dispatch(state, {
    type: "server_command",
    command: { ...questionCommand(), state: "started", command_revision: 1 },
  });
  assert.equal(unobservedStart.phase, "paused");

  const jumped = dispatch(state, {
    type: "server_command",
    command: { ...questionCommand(), state: "started", command_revision: 2 },
  });
  assert.equal(jumped.phase, "paused");

  const rewrittenPending = dispatch(state, {
    type: "server_command",
    command: { ...questionCommand(), command_revision: 1 },
  });
  assert.equal(rewrittenPending.phase, "paused");

  const record = recordCommand();
  const rewrittenPresentation = dispatch(restoreAutopilotRuntime(record), {
    type: "server_command",
    command: {
      ...record,
      payload: { ...record.payload, presentation_speech_text: "被改写的话术" },
    },
  });
  assert.equal(rewrittenPresentation.phase, "paused");
});

test("record receipt must name the server-preallocated raw audio id", () => {
  const command = recordCommand();
  const stopped = buildAutopilotAck(
    command, 0, "evt-wrong-raw-id-01",
    {
      ack_type: "record_stopped",
      stop_reason: "silence",
      raw_audio_id: "raw-some-other-slot",
      receipt_server_seq: 4,
      checksum: CHECKSUM,
      byte_count: 9_000,
      duration_seconds: 2,
    },
  );
  const state = dispatch(restoreAutopilotRuntime(command), {
    type: "device_ack", ack: stopped,
  });
  assert.equal(state.phase, "paused");
});

test("technical and bounded media failures latch a safe pause", () => {
  let state = dispatch(restoreAutopilotRuntime(questionCommand()), {
    type: "technical_failure",
  });
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "technical_failure");

  const failure = buildAutopilotAck(
    questionCommand(), 0, "evt-tts-failed-0001",
    { ack_type: "tts_failed", error_code: "audio_playback_failed" },
  );
  state = dispatch(restoreAutopilotRuntime(questionCommand()), {
    type: "device_ack", ack: failure,
  });
  assert.equal(state.phase, "paused");
  assert.equal(state.pause_reason, "tts_failed");
});

test("terminal feedback accepts null next as explicit P0a scope completion", () => {
  const feedback = feedbackCommand();
  let state = restoreAutopilotRuntime(feedback);
  const ended = buildAutopilotAck(
    feedback, 0, "evt-feedback-end-01",
    { ack_type: "tts_ended", media_ended: true },
  );
  state = dispatch(state, { type: "device_ack", ack: ended });
  state = dispatch(state, { type: "server_command", command: null });
  assert.equal(state.phase, "scope_completed");
  assert.equal(state.command, null);
  assert.equal(canOpenAutopilotMicrophone(state), false);
});

test("null next after question is a protocol violation, but after record remains processing", () => {
  const question = questionCommand();
  let questionState = restoreAutopilotRuntime(question);
  questionState = dispatch(questionState, {
    type: "device_ack",
    ack: buildAutopilotAck(
      question, 0, "evt-question-end-01", { ack_type: "tts_ended", media_ended: true },
    ),
  });
  questionState = dispatch(questionState, { type: "server_command", command: null });
  assert.equal(questionState.phase, "paused");

  const record = recordCommand();
  let recordState = restoreAutopilotRuntime(record);
  recordState = dispatch(recordState, {
    type: "device_ack",
    ack: buildAutopilotAck(
      record, 0, "evt-record-stop-01",
      {
        ack_type: "record_stopped",
        stop_reason: "silence",
        raw_audio_id: record.payload.raw_audio_id,
        receipt_server_seq: 8,
        checksum: CHECKSUM,
        byte_count: 4_000,
        duration_seconds: 1.5,
      },
    ),
  });
  const processing = dispatch(recordState, { type: "server_command", command: null });
  assert.equal(processing.phase, "waiting_server_after_record");
});

test("a question cannot skip record, and terminal feedback cannot open a new record", () => {
  const question = questionCommand();
  let questionState = restoreAutopilotRuntime(question);
  questionState = dispatch(questionState, {
    type: "device_ack",
    ack: buildAutopilotAck(
      question, 0, "evt-question-end-02", { ack_type: "tts_ended", media_ended: true },
    ),
  });
  questionState = dispatch(questionState, {
    type: "server_command",
    command: { ...feedbackCommand(), command_seq: 2 },
  });
  assert.equal(questionState.phase, "paused");

  const feedback = feedbackCommand();
  let feedbackState = restoreAutopilotRuntime(feedback);
  feedbackState = dispatch(feedbackState, {
    type: "device_ack",
    ack: buildAutopilotAck(
      feedback, 0, "evt-feedback-end-02", { ack_type: "tts_ended", media_ended: true },
    ),
  });
  feedbackState = dispatch(feedbackState, {
    type: "server_command",
    command: { ...recordCommand(), command_seq: 4 },
  });
  assert.equal(feedbackState.phase, "paused");
});
