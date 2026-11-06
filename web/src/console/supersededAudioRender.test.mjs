import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 真 SSR：作废录音槽一节默认收起、只读，两种来源(规则记账/无审计)分得开。

const RULE_ROW = {
  raw_audio_id: "raw-orphan-1",
  session_id: "S-ONE",
  patient_id: "P-001",
  marked_by_rule: true,
  last_marked_at: "2026-09-13T06:30:00+00:00",
  local_copy_disposal_device_count: 1,
  local_copy_disposal_last_at: "2026-09-13T06:31:00+00:00",
};
const HAND_ROW = {
  raw_audio_id: "raw-hand-voided",
  session_id: "S-TWO",
  patient_id: "P-002",
  marked_by_rule: false,
  last_marked_at: null,
  local_copy_disposal_device_count: 0,
  local_copy_disposal_last_at: null,
};

async function withPanelModule(run) {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  try {
    return await run(await vite.ssrLoadModule("/src/console/WithdrawnAudioGovernancePanel.tsx"));
  } finally {
    await vite.close();
  }
}

const collapsed = (markup) => /<details[\s>]/.test(markup) && !/<details[^>]*\sopen/.test(markup);

test("作废录音槽一节默认收起，规则记账与无审计行分开标，且没有任何删除按钮", async () => {
  await withPanelModule(({ SupersededAudioSlots }) => {
    const markup = renderToStaticMarkup(React.createElement(SupersededAudioSlots, {
      rows: [RULE_ROW, HAND_ROW], error: null,
    }));
    assert.ok(collapsed(markup), markup);
    // summary 设成 flex 会吞掉原生展开三角；写死的「默认收起」展开后就不对了。
    assert.doesNotMatch(markup, /<summary[^>]*display:\s*flex/);
    assert.doesNotMatch(markup, /默认收起/);
    assert.match(markup, /技术作废的录音槽（服务器从未收到字节，不是撤回） · 2 条/);
    assert.match(markup, /raw-orphan-1/);
    assert.match(markup, /raw-hand-voided/);
    assert.match(markup, /研究编号 P-002 · 后台场次 S-TWO/);
    assert.equal(markup.match(/作废规则已记账/g)?.length, 1);
    assert.equal(markup.match(/无作废规则审计记录/g)?.length, 1);
    assert.equal(markup.match(/作废时间 /g)?.length, 1);
    assert.match(markup, /本地副本删除回执 1 台设备/);
    assert.match(markup, /尚无设备确认删除本地副本/);
    assert.doesNotMatch(markup, /<button/);
    assert.doesNotMatch(markup, /物理删除/);
  });
});

test("读取中、空表、读取失败三态各自如实显示，同样默认收起", async () => {
  await withPanelModule(({ SupersededAudioSlots }) => {
    const loading = renderToStaticMarkup(React.createElement(SupersededAudioSlots, {
      rows: null, error: null,
    }));
    assert.ok(collapsed(loading));
    assert.match(loading, /正在读取作废录音槽/);
    assert.doesNotMatch(loading, / · \d+ 条/);

    const empty = renderToStaticMarkup(React.createElement(SupersededAudioSlots, {
      rows: [], error: null,
    }));
    assert.match(empty, / · 0 条/);
    assert.match(empty, /暂无技术作废的录音槽/);

    const failed = renderToStaticMarkup(React.createElement(SupersededAudioSlots, {
      rows: null, error: "账号角色 researcher 无权读取技术作废录音槽治理列表",
    }));
    assert.match(failed, /作废录音槽列表未读取/);
    assert.match(failed, /无权读取技术作废录音槽治理列表/);
    assert.doesNotMatch(failed, /正在读取作废录音槽/);
  });
});

test("撤回录音治理面板同时带着撤回一节与收起的作废一节", async () => {
  await withPanelModule(({ WithdrawnAudioGovernancePanel }) => {
    const markup = renderToStaticMarkup(React.createElement(WithdrawnAudioGovernancePanel));
    assert.match(markup, /撤回录音治理 · 仅管理员/);
    assert.match(markup, /技术作废的录音槽（服务器从未收到字节，不是撤回）/);
    assert.ok(collapsed(markup), markup);
    assert.ok(markup.indexOf("撤回录音治理") < markup.indexOf("技术作废的录音槽"));
  });
});
