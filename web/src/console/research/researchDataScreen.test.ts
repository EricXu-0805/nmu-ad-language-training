import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const screen = readFileSync(new URL("./ResearchDataScreen.tsx", import.meta.url), "utf8");
const analysis = readFileSync(new URL("../AnalysisScreen.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../../api.ts", import.meta.url), "utf8");
const table = readFileSync(new URL("./ResearchDataTable.tsx", import.meta.url), "utf8");

test("总览屏只渲染响应自己声明的列，源码里不出现任何被禁列名", () => {
  // 行是按 columns 投影出来的数组，屏幕结构上取不到别的东西；这里再钉一遍
  // 源文本，防止有人"顺手加一列 patient_id 方便核对"。
  for (const forbidden of ["patient_id", "asr_text", "judge_reason",
                           "confirmed_response_text", "consent_person",
                           "raw_audio_id", "created_at"]) {
    for (const [name, source] of [["总览屏", screen], ["表格", table]] as const) {
      assert.doesNotMatch(source, new RegExp(forbidden), `${name}不得出现 ${forbidden}`);
    }
  }
  assert.match(table, /page\.columns\.map/);
  assert.match(table, /page\.rows\.map/);
});

test("总览屏是只读的：不发任何写请求", () => {
  assert.doesNotMatch(screen, /"(POST|PUT|PATCH|DELETE)"/);
  assert.doesNotMatch(table, /\bapi\./);
  assert.doesNotMatch(screen, /api\.(create|start|cancel|submit|complete|approve|close|delete|set)[A-Z]/);
});

test("密钥没配时给出可自诊的原因，并明说不会退化成明文", () => {
  assert.match(screen, /研究数据接口尚未开启/);
  assert.match(screen, /绝不会<\/strong>退化成返回明文受试者编号/);
  assert.match(screen, /meta\?\.configured === false/);
});

test("403 讲清楚是角色问题而不是故障，并且不教人共用账号", () => {
  assert.match(screen, /data_steward/);
  assert.match(screen, /不要共用别人的账号/);
});

test("导出与屏上那一页共用同一个 query 构造器", () => {
  // 两条路径都走 researchDatasetPath，CSV 与 JSON 结构上不可能拉到不同范围。
  assert.match(api, /researchDataset: async[\s\S]{0,400}researchDatasetPath\(request\)/);
  assert.match(api, /researchCsv:[\s\S]{0,200}researchDatasetPath\(\{ \.\.\.request, csv: true \}\)/);
  assert.match(screen, /api\.researchCsv\(\{ dataset, classification, cursor \}\)/);
  assert.match(screen, /api\.researchDataset\(\{ dataset, classification, cursor/);
});

test("分析后台挂上了入口，并写明只向数据管理员开放", () => {
  assert.match(analysis, /<ResearchDataScreen onBack=/);
  assert.match(analysis, /打开研究数据总览/);
  assert.match(analysis, /只向数据管理员与管理员开放/);
});

test("换数据集或分区必须回到第一页——游标是按自然键签名的", () => {
  assert.match(screen, /setCursorStack\(\[null\]\);? \}, \[dataset, classification\]\)/);
});
