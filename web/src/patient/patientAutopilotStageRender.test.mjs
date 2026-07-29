import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const COMMAND_KEY = "cmd-record-stage-0001";

function recordingView() {
  const command = {
    schema_version: 1,
    command_key: COMMAND_KEY,
    command_seq: 2,
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
      raw_audio_id: "raw-stage-0001",
      turn_ref: "itm-0001#1",
      max_duration_seconds: 15,
      contains_direct_identifier: false,
      presentation_speech_key: "wk2.01.question",
      presentation_speech_text: "请看这张图片，您能告诉我这是什么吗？",
      presentation_purpose: "question",
    },
  };
  return {
    mode: "server",
    runtime: {
      phase: "recording",
      command,
      last_device_event_seq: 3,
      last_ack: null,
      pause_reason: null,
    },
    current: command,
    reason: null,
    assetReadiness: { requestKey: COMMAND_KEY, readiness: "ready" },
    reportAssetReadiness: () => {},
    stopMediaNow: () => {},
    stopRecordingNow: () => {},
  };
}

test("服务器托管录音阶段：提前结束按钮明确标成可选，不点也会自动继续", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { PatientAutopilotStage } = await vite.ssrLoadModule(
    "/src/patient/PatientAutopilotStage.tsx",
  );

  const markup = renderToStaticMarkup(React.createElement(PatientAutopilotStage, {
    autopilot: recordingView(),
    sessionId: "S-ONE",
    activated: true,
    ttsOn: true,
    externallyPaused: false,
  }));

  // 旧文案"我说好了"读起来像"必须点一下才算说完",老人不点就以为卡住了。
  assert.doesNotMatch(markup, /我说好了/);
  assert.match(markup, /说完了可以点这里/);
  // 可选性必须写在屏幕上,而且和按钮建立可访问关联。
  assert.match(markup, /不点也可以，我们会自动继续/);
  assert.match(markup, /aria-describedby="autopilot-stop-optional"/);
  assert.match(markup, /id="autopilot-stop-optional"/);
  assert.match(markup, /正在听您说/);
});

test("非录音阶段不出现这个可选按钮，也不出现它的提示", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { PatientAutopilotStage } = await vite.ssrLoadModule(
    "/src/patient/PatientAutopilotStage.tsx",
  );

  const view = recordingView();
  view.runtime = { ...view.runtime, phase: "tts_playing" };
  const markup = renderToStaticMarkup(React.createElement(PatientAutopilotStage, {
    autopilot: view,
    sessionId: "S-ONE",
    activated: true,
    ttsOn: true,
    externallyPaused: false,
  }));

  assert.doesNotMatch(markup, /说完了可以点这里/);
  assert.doesNotMatch(markup, /不点也可以，我们会自动继续/);
  assert.match(markup, /正在为您朗读/);
});
