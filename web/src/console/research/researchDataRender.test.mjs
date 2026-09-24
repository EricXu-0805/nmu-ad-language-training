import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 真 SSR：断言"屏幕只能渲染响应自己声明的列"不是靠读源码，而是靠真渲染出来的
// HTML 里到底有几个格子、格子里到底是什么。

const COLUMNS = ["subject_code", "session_code", "item_id", "turn_seq", "ai_score"];
const RELEASE = { epochSeq: 3, cohortRuleVersion: "cohort.v1", aggregatePayloadSha256: "a".repeat(64) };

function page(overrides = {}) {
  return {
    schemaVersion: "research-read.v1",
    dataset: "turns",
    grain: "一行一次作答",
    release: RELEASE,
    pseudonymVersion: "v1",
    pseudonymKeyId: "nmu-2026-01",
    columns: COLUMNS,
    rows: [
      ["SUBJ-v1-nmu-2026-01-aaaa", "SESS-v1-nmu-2026-01-bbbb", "SE_胡萝卜", 1, 2],
      ["SUBJ-v1-nmu-2026-01-cccc", "SESS-v1-nmu-2026-01-dddd", null, null, null],
    ],
    rowCount: 2,
    hasMore: true,
    nextCursor: "cursor-2",
    ...overrides,
  };
}

async function renderModule(modulePath, exportName, props) {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  try {
    const module = await vite.ssrLoadModule(modulePath);
    return renderToStaticMarkup(React.createElement(module[exportName], props));
  } finally {
    await vite.close();
  }
}

function render(props) {
  return renderModule("/src/console/research/ResearchDataTable.tsx", "ResearchTable", {
    page: page(), downloading: false,
    onExportPage() {}, onExportDictionary() {},
    canGoBack: false, onBack() {}, onNext() {}, pageNo: 1,
    ...props,
  });
}

function meta(overrides = {}) {
  return {
    configured: true, schemaVersion: "research-read.v1",
    pseudonymVersion: "v1", pseudonymKeyId: "nmu-2026-01",
    release: { ...RELEASE, bound: true, asOf: "2026-08-01T00:00:00Z",
      frozenAt: "2026-08-02T00:00:00Z", frozenSessionCount: 12 },
    note: "请核对当前页的假名密钥编号。", ...overrides,
  };
}

function renderProvenance(props = {}) {
  return renderModule("/src/console/research/ResearchDataProvenance.tsx", "ResearchDataProvenance", {
    meta: meta(), page: page(), ...props,
  });
}

test("渲染出来的表头逐字等于响应声明的列，一列不多一列不少", async () => {
  const markup = await render({});
  const headers = [...markup.matchAll(/<th scope="col" class="mono">([^<]*)<\/th>/g)]
    .map((match) => match[1]);
  assert.deepEqual(headers, COLUMNS);
});

test("每行的格子数恰好等于列数，空值渲染成占位符而不是消失", async () => {
  const markup = await render({});
  const bodyRows = [...markup.matchAll(/<tr>((?:<td[^>]*>[^<]*<\/td>)+)<\/tr>/g)]
    .map((match) => [...match[1].matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((cell) => cell[1]));
  assert.equal(bodyRows.length, 2);
  for (const row of bodyRows) assert.equal(row.length, COLUMNS.length);
  // 墓碑行：除编号外全空，但行还在——否则两次拉取之差会变成"谁撤回了"的旁路。
  assert.deepEqual(bodyRows[1].slice(2), ["—", "—", "—"]);
});

test("无录音的跳过题仍渲染题号、环节与裁定原因", async () => {
  const markup = await render({ page: page({
    dataset: "adjudications",
    columns: ["item_id", "presentation_order", "turn_seq", "kind", "reason_code"],
    rows: [["SE_熨斗", 2, 1, "skipped", "participant_declined"]],
    rowCount: 1, hasMore: false, nextCursor: null,
  }) });
  const cells = [...markup.matchAll(/<td[^>]*>([^<]*)<\/td>/g)].map((match) => match[1]);
  assert.deepEqual(cells, ["SE_熨斗", "2", "1", "skipped", "participant_declined"]);
});

test("渲染结果里不含任何明文标识符、绝对时间或作答文本", async () => {
  const markup = await render({});
  for (const leak of ["patient_id", "asr_text", "我叫", "P-REAL"]) {
    assert.ok(!markup.includes(leak), `渲染结果不得含 ${leak}`);
  }
  assert.doesNotMatch(markup, /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/);
});

test("导出按钮说清导的是当前这一页，不是全量", async () => {
  const markup = await render({});
  assert.match(markup, /导出本页 CSV/);
  assert.match(markup, /导出数据字典 CSV/);
  assert.match(markup, /导出的是<strong>当前这一页<\/strong>/);
});

test("零行时明说是真的零行，且不渲染空表格骨架", async () => {
  const markup = await render({ page: page({ rows: [], rowCount: 0, hasMore: false, nextCursor: null }) });
  assert.match(markup, /目前确实没有数据/);
  assert.ok(!markup.includes("<table"), "零行不应渲染表格");
});

test("最后一页时下一页不可点，并明确告诉人已经到底", async () => {
  const markup = await render({ page: page({ hasMore: false, nextCursor: null }) });
  assert.match(markup, /已经是最后一页/);
  const nextButton = markup.match(/<button[^>]*>下一页<\/button>/);
  assert.ok(nextButton, "应有下一页按钮");
  assert.match(nextButton[0], /disabled/);
});

test("正在导出时两个导出按钮都锁住，避免重复打服务端", async () => {
  const markup = await render({ downloading: true });
  const buttons = [...markup.matchAll(/<button[^>]*>(?:正在导出…|导出数据字典 CSV)<\/button>/g)];
  assert.equal(buttons.length, 2);
  for (const button of buttons) assert.match(button[0], /disabled/);
});

test("当前页与概况版本一致时，才同时显示该版截止日和场次数", async () => {
  const markup = await renderProvenance();
  assert.match(markup, /当前页数据版本：第 3 版/);
  assert.match(markup, /本版本包含 12 个场次，截止 2026-08-01T00:00:00Z/);
  assert.match(markup, /aaaaaaaaaaaa/);
});

test("发布新版后，显示实际页版本和假名键，不沿用旧概况的截止日与场次数", async () => {
  const markup = await renderProvenance({ page: page({
    release: { ...RELEASE, epochSeq: 4, aggregatePayloadSha256: "b".repeat(64) },
    schemaVersion: "research-read.v2", pseudonymVersion: "v2", pseudonymKeyId: "new-key",
  }) });
  assert.match(markup, /当前页数据版本：第 4 版/);
  assert.match(markup, /bbbbbbbbbbbb/);
  assert.match(markup, /research-read.v2/);
  assert.match(markup, /new-key/);
  for (const stale of ["第 3 版", "aaaaaaaaaaaa", "2026-08-01", "12 个场次", "nmu-2026-01"]) {
    assert.ok(!markup.includes(stale), `不得沿用旧概况：${stale}`);
  }
  assert.match(markup, /截止日期与场次数尚未核对，暂不显示/);
});

test("纪元号相同但指纹或纳入规则不同时，也不能借用概况", async () => {
  for (const changed of [
    { aggregatePayloadSha256: "b".repeat(64) },
    { cohortRuleVersion: "cohort.v2" },
  ]) {
    const markup = await renderProvenance({ page: page({ release: { ...RELEASE, ...changed } }) });
    assert.match(markup, /截止日期与场次数尚未核对，暂不显示/);
    assert.doesNotMatch(markup, /2026-08-01|12 个场次/);
  }
});

test("概况还没有冻结版时，已成功读取的页面仍按自身版本显示", async () => {
  const markup = await renderProvenance({ meta: meta({
    release: { bound: false, code: "research_release_not_frozen", reason: "尚未发布" },
  }) });
  assert.match(markup, /当前页数据版本：第 3 版/);
  assert.doesNotMatch(markup, /2026-08-01|12 个场次/);
});

test("模拟页明确未冻结，不能挂上真实研究概况中的版本、场次数或截止日", async () => {
  const markup = await renderProvenance({ page: page({ release: null }) });
  assert.match(markup, /当前页为模拟演练数据，没有冻结版本/);
  assert.match(markup, /导出以下载时的数据为准/);
  assert.doesNotMatch(markup, /第 3 版|数据指纹|2026-08-01|12 个场次/);
});

test("取数未成功时，不用接口概况冒充当前页版本", async () => {
  const markup = await renderProvenance({ page: null });
  assert.match(markup, /数据版本将在当前页读取成功后显示/);
  assert.doesNotMatch(markup, /第 3 版|数据指纹|2026-08-01|12 个场次/);
});
