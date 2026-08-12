import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const COMMAND_KEY = "cmd-record-stage-0001";

const IDENTITY = {
  sessionId: "S-ONE",
  commandKey: COMMAND_KEY,
  ownerGeneration: 4,
  captureGeneration: 2,
};
const LISTENING = { ...IDENTITY, phase: "listening" };

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
    // 屏显与按钮由浏览器自己的 listening 驱动，不由服务器 runtime 驱动。
    localCapturePhase: LISTENING,
    reportAssetReadiness: () => {},
    stopMediaNow: () => {},
    stopForPatientPauseNow: () => {},
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
  view.localCapturePhase = null;
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

const PERSISTING = { ...IDENTITY, stopReason: "user_done", phase: "persisting" };

async function stageMarkup(context, mutate) {
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
  mutate(view);
  return renderToStaticMarkup(React.createElement(PatientAutopilotStage, {
    autopilot: view,
    sessionId: "S-ONE",
    activated: true,
    ttsOn: true,
    externallyPaused: false,
  }));
}

test("物理收麦之后立刻改说正在保存，并收起可选按钮与提示", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.localCapturePhase = PERSISTING;
  });

  assert.match(markup, /已收音，正在保存，请稍候/);
  // 麦克风已经关了，再说"正在听您说"就是在骗老人继续讲。
  assert.doesNotMatch(markup, /正在听您说/);
  assert.doesNotMatch(markup, /说完了可以点这里/);
  assert.doesNotMatch(markup, /不点也可以，我们会自动继续/);
});

test("服务器 runtime 还没追上时也不许闪回监听态", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.localCapturePhase = PERSISTING;
    view.runtime = { ...view.runtime, phase: "waiting_server_after_record" };
  });

  assert.match(markup, /已收音，正在保存，请稍候/);
  assert.doesNotMatch(markup, /正在听您说/);
  assert.doesNotMatch(markup, /说完了可以点这里/);
});

test("record_started 的网络 ACK 还没回来，屏幕已经在说正在听您说并给出按钮", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    // 服务器仍停在 waiting_recording：ACK 在途，runtime 尚未推进到 recording。
    view.runtime = { ...view.runtime, phase: "waiting_recording" };
    view.localCapturePhase = LISTENING;
  });

  assert.match(markup, /正在听您说/);
  assert.match(markup, /说完了可以点这里/);
  assert.match(markup, /不点也可以，我们会自动继续/);
});

test("旧场次或旧命令的本地事件一律不驱动屏显，也不给出按钮", async (context) => {
  const staleSession = await stageMarkup(context, (view) => {
    view.localCapturePhase = { ...LISTENING, sessionId: "S-OLD" };
  });
  assert.doesNotMatch(staleSession, /已收音，正在保存，请稍候/);
  assert.doesNotMatch(staleSession, /正在听您说/);
  assert.doesNotMatch(staleSession, /说完了可以点这里/);

  const staleCommand = await stageMarkup(context, (view) => {
    view.localCapturePhase = { ...PERSISTING, commandKey: "cmd-record-stage-0000" };
  });
  assert.doesNotMatch(staleCommand, /已收音，正在保存，请稍候/);
  assert.doesNotMatch(staleCommand, /正在听您说/);
  assert.doesNotMatch(staleCommand, /说完了可以点这里/);
});

test("生命周期清掉 listening 之后，服务器还停在 recording 也不残留按钮", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    // capture 已被 device_runtime_failed 丢弃，本地相位清空；
    // 服务器 runtime 还没收到失败 ACK，仍然是 recording。
    view.localCapturePhase = null;
  });

  assert.doesNotMatch(markup, /正在听您说/);
  assert.doesNotMatch(markup, /说完了可以点这里/);
  assert.doesNotMatch(markup, /不点也可以，我们会自动继续/);
});

test("runtime_released 暂停显示平静文案，绝不出现研究者处置告警", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.runtime = { ...view.runtime, phase: "paused", pause_reason: "runtime_released" };
    view.localCapturePhase = null;
  });

  // 服务器收走 runtime(收尾/暂停/中止)不是设备故障;老人端只看到休息文案,
  // 结局由 live 通道呈现。
  assert.match(markup, /我们先休息一下/);
  assert.match(markup, /练习已暂停，请稍候/);
  assert.doesNotMatch(markup, /自动流程已安全停止/);
  assert.doesNotMatch(markup, /请研究者处置/);
  assert.match(markup, /role="status"/);
});

test("technical_failure 暂停维持告警档，不被平静档吞掉", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.runtime = { ...view.runtime, phase: "paused", pause_reason: "technical_failure" };
    view.localCapturePhase = null;
  });

  assert.match(markup, /自动流程已安全停止，请研究者处置/);
  assert.match(markup, /role="alert"/);
});

test("blocked 平静档(runtime 被服务器收走)：休息文案 + role=status，无研究者告警", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.mode = "blocked";
    view.runtime = null;
    view.current = null;
    view.localCapturePhase = null;
    view.reason = "练习已暂停，请稍候";
    view.blockedCalm = true;
  });

  assert.match(markup, /我们先等一下/);
  assert.match(markup, /练习已暂停，请稍候/);
  assert.match(markup, /role="status"/);
  assert.doesNotMatch(markup, /role="alert"/);
  assert.doesNotMatch(markup, /请研究者/);
});

test("blocked 告警档(设备侧故障)维持 role=alert", async (context) => {
  const markup = await stageMarkup(context, (view) => {
    view.mode = "blocked";
    view.runtime = null;
    view.current = null;
    view.localCapturePhase = null;
    view.reason = "自动流程已绑定其他设备，请研究者处置";
    view.blockedCalm = false;
  });

  assert.match(markup, /role="alert"/);
  assert.match(markup, /请研究者处置/);
});
