import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 真 SSR：断言"屏幕只能渲染响应自己声明的列"不是靠读源码，而是靠真渲染出来的
// HTML 里到底有几个格子、格子里到底是什么。

const COLUMNS = ["subject_code", "session_code", "item_id", "turn_seq", "ai_score"];

function page(overrides = {}) {
  return {
    schemaVersion: "research-read.v1",
    dataset: "turns",
    grain: "一行一次作答",
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

async function render(props) {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  try {
    const { ResearchTable } = await vite.ssrLoadModule(
      "/src/console/research/ResearchDataTable.tsx",
    );
    return renderToStaticMarkup(React.createElement(ResearchTable, {
      page: page(), downloading: false,
      onExportPage() {}, onExportDictionary() {},
      canGoBack: false, onBack() {}, onNext() {}, pageNo: 1,
      ...props,
    }));
  } finally {
    await vite.close();
  }
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
