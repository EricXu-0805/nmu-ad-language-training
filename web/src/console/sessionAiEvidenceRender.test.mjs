import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 这些完成态/播放态字样绝不能出现在证据区 DOM 里——served 只证明服务端把字节交回了
// 请求方，不是终端已播放；真实 started/ended playback ACK 属于 T5。
const FORBIDDEN_PLAYBACK_CLAIMS = [
  "老人已听到", "已播放", "播放完成", "已收听", "确认播放",
];

test("TTS serve evidence section states never overclaim playback, and stay distinct (contract-error/empty/ready, served vs degraded)", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { TtsServeEvidenceSectionView } = await vite.ssrLoadModule("/src/console/AnalysisScreen.tsx");

  const contractError = renderToStaticMarkup(React.createElement(TtsServeEvidenceSectionView, {
    section: { status: "contract-error", message: "text_sha256 必须是 64 位小写 hex" },
  }));
  assert.match(contractError, /语音返回记录异常/);
  assert.match(contractError, /text_sha256/);

  const empty = renderToStaticMarkup(React.createElement(TtsServeEvidenceSectionView, {
    section: { status: "empty", notice: "当前汇总未记录实际使用证据" },
  }));
  assert.match(empty, /当前汇总未记录实际使用证据/);
  assert.doesNotMatch(empty, /没有使用 AI/);

  const ready = renderToStaticMarkup(React.createElement(TtsServeEvidenceSectionView, {
    section: {
      status: "ready",
      summary: { served: 1, degraded: 1, cacheHits: 0 },
      rows: [
        {
          key: "tts-1", time: "2026-07-28 10:00:00", sourceLabel: "自动驾驶指令",
          commandLabel: "指令 #7", engineVersion: "qwen-tts-v1", resultLabel: "已实际返回音频",
          resultTone: "ok", cacheLabel: "缓存未命中", byteLabel: "4096 字节", isSimulation: false,
        },
        {
          key: "tts-2", time: "2026-07-28 10:05:00", sourceLabel: "现场朗读",
          commandLabel: "无绑定指令", engineVersion: "qwen-tts-v1", resultLabel: "已改用本机语音(服务器未返回音频)",
          resultTone: "warn", cacheLabel: "缓存不适用(服务器未返回音频)", byteLabel: "—", isSimulation: false,
        },
      ],
    },
  }));
  assert.match(ready, /已实际返回音频/);
  assert.match(ready, /已改用本机语音\(服务器未返回音频\)/);
  assert.match(ready, /缓存不适用\(服务器未返回音频\)/);
  for (const markup of [contractError, empty, ready]) {
    for (const phrase of FORBIDDEN_PLAYBACK_CLAIMS) {
      assert.equal(markup.includes(phrase), false, `不该出现「${phrase}」`);
    }
    assert.match(markup, /不代表老人设备上已实际播放/);
  }
});

test("confirmation revision history is content-blind and distinguishes no_ledger from tracked without claiming 'never confirmed'", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { ConfirmationRevisionHistory } = await vite.ssrLoadModule("/src/console/AnalysisScreen.tsx");

  const noLedger = renderToStaticMarkup(React.createElement(ConfirmationRevisionHistory, {
    confirmation: { turnId: 10, status: "no_ledger", notice: "当前无可追溯修订履历，可能为历史兼容记录" },
  }));
  assert.match(noLedger, /可能为历史兼容记录/);
  assert.doesNotMatch(noLedger, /从未确认/);

  const tracked = renderToStaticMarkup(React.createElement(ConfirmationRevisionHistory, {
    confirmation: {
      turnId: 11,
      status: "tracked",
      finalRevision: 2,
      entries: [
        { key: "turn-11-rev-1", revision: 1, actorDisplayId: "researcher-1", time: "2026-07-28 09:00:00" },
        { key: "turn-11-rev-2", revision: 2, actorDisplayId: "researcher-2", time: "2026-07-28 09:05:00" },
      ],
    },
  }));
  assert.match(tracked, /第 1 次修订/);
  assert.match(tracked, /第 2 次修订/);
  assert.match(tracked, /操作者 researcher-1/);
  assert.match(tracked, /内容无关记录/);
});

test("AI usage section distinguishes loading/forbidden/withdrawn/network-error/contract-error/ready and never claims 'no AI used'", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { AiUsageSectionView } = await vite.ssrLoadModule("/src/console/AnalysisScreen.tsx");

  const loading = renderToStaticMarkup(React.createElement(AiUsageSectionView, { state: { status: "loading" } }));
  assert.match(loading, /正在加载 AI 使用汇总/);

  const forbidden = renderToStaticMarkup(React.createElement(AiUsageSectionView, { state: { status: "forbidden" } }));
  assert.match(forbidden, /无权查看/);

  const withdrawn = renderToStaticMarkup(React.createElement(AiUsageSectionView, { state: { status: "withdrawn" } }));
  assert.match(withdrawn, /受试者内容已撤回/);
  assert.doesNotMatch(withdrawn, /0 次/);

  const networkError = renderToStaticMarkup(React.createElement(AiUsageSectionView, {
    state: { status: "network-error", message: "无法连接服务器" },
  }));
  assert.match(networkError, /AI 使用汇总读取失败/);
  assert.match(networkError, /无法连接服务器/);

  const contractError = renderToStaticMarkup(React.createElement(AiUsageSectionView, {
    state: { status: "contract-error", message: "AI 使用汇总返回了其他场次，已拒绝显示" },
  }));
  assert.match(contractError, /AI 使用汇总数据异常/);
  assert.match(contractError, /其他场次/);

  const emptyReady = renderToStaticMarkup(React.createElement(AiUsageSectionView, {
    state: {
      status: "ready",
      model: { hasAnyRecords: false, ttsRows: [], asrRows: [], judgeRows: [], rapportRows: [] },
    },
  }));
  assert.match(emptyReady, /当前汇总未记录实际使用证据/);
  assert.doesNotMatch(emptyReady, /没有使用 AI/);

  const populatedReady = renderToStaticMarkup(React.createElement(AiUsageSectionView, {
    state: {
      status: "ready",
      model: {
        hasAnyRecords: true,
        ttsRows: [{ key: "tts-qwen-tts-v1", label: "qwen-tts-v1", detail: "实际返回 3 次(缓存命中 1) · 改用本机语音 1 次" }],
        asrRows: [{ key: "asr-asr-v1", label: "asr-v1", detail: "尝试 5 次" }],
        judgeRows: [{ key: "judge-规则确定式-rule-1", label: "规则确定式 · rule-1", detail: "5 次" }],
        rapportRows: [{ key: "rapport-reply-qwen-plus", label: "现编回应 · qwen-plus", detail: "3 次" }],
      },
    },
  }));
  assert.match(populatedReady, /实际返回 3 次/);
  assert.match(populatedReady, /规则确定式/);
  assert.doesNotMatch(populatedReady, /当前汇总未记录实际使用证据/);
  // provider readiness/健康度不是这个区的口径，不该跟"实际使用"混在一起宣称。
  assert.doesNotMatch(populatedReady, /provider ready|健康度良好/);
  for (const markup of [loading, forbidden, withdrawn, networkError, contractError, emptyReady, populatedReady]) {
    assert.doesNotMatch(markup, /provider ready/);
  }
});
