import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

// 真 SSR:三种渲染器"由定义驱动、题词只来自 API"不是靠读源码断言,而是靠
// 真渲染出来的 HTML 里到底有几个档位按钮、按钮里到底是什么字。
// 夹具全部是微型假定义,绝不复制真实题词/锚点。

const SHA = "a".repeat(64);

function choice(allowed, anchorPrefix) {
  return {
    allowed,
    anchors: Object.fromEntries(allowed.map((value) => [value, `${anchorPrefix}${value}`])),
  };
}

function provenance() {
  return {
    provided_by: "假提供人",
    provided_via: "假途径",
    provided_on: "2026-08-20",
    source_file: "假来源.docx",
    source_sha256: SHA,
    final_confirmation: "假终确认状态",
  };
}

function fakeItem(key, no, overrides = {}) {
  return { item_key: key, no, text: `假题干${no}`, name: null, score_when: null, ...overrides };
}

function ordinalDefinition() {
  return {
    schema_version: "questionnaire-definition.v1",
    questionnaire_id: "fake_ordinal_v1",
    title: "假想沟通量表",
    short_name: "FAKE-A",
    respondent: "observer",
    status: "prototype",
    provenance: provenance(),
    instruction: "假指导语一句",
    response_kind: "ordinal_sections",
    value_field: choice(["7", "6", "5", "4", "3", "2", "1", "N"], "假锚"),
    element_field: {
      ...choice(["5", "4", "3", "2", "1"], "假频"),
      elements: [
        { element_key: "alpha", label: "假要素甲" },
        { element_key: "beta", label: "假要素乙" },
      ],
    },
    present_field: null,
    severity_field: null,
    frequency_field: null,
    sections: [{
      section_id: "s1",
      title: "假节甲",
      items: [fakeItem("i1", 1), fakeItem("i2", 2)],
    }],
    items: null,
    examiner_panel: null,
    scoring: null,
    transcription_notes: [],
  };
}

function binaryDefinition() {
  return {
    schema_version: "questionnaire-definition.v1",
    questionnaire_id: "fake_binary_v1",
    title: "假想是否量表",
    short_name: "FAKE-B",
    respondent: "patient_reported",
    status: "prototype",
    provenance: provenance(),
    instruction: "假指导语二句",
    response_kind: "binary_scored",
    value_field: choice(["是", "否"], "答"),
    element_field: null,
    present_field: null,
    severity_field: null,
    frequency_field: null,
    sections: null,
    items: [fakeItem("b1", 1, { score_when: "是" }), fakeItem("b2", 2, { score_when: "否" })],
    examiner_panel: null,
    scoring: {
      kind: "binary_sum",
      scoring_rule_id: "fake.rule.v1",
      max_score: 3,
      cutoff_value: 2,
      cutoff_operator: ">=",
      cutoff_label: "达界示例",
      rule_verbatim: "示例计分说明",
    },
    transcription_notes: [],
  };
}

function tripletDefinition() {
  return {
    schema_version: "questionnaire-definition.v1",
    questionnaire_id: "fake_triplet_v1",
    title: "假想症状量表",
    short_name: "FAKE-C",
    respondent: "observer",
    status: "prototype",
    provenance: provenance(),
    instruction: "假指导语三句",
    response_kind: "symptom_triplet",
    value_field: null,
    element_field: null,
    present_field: choice(["有", "无"], "记"),
    severity_field: choice(["1", "2", "3"], "假严重锚"),
    frequency_field: choice(["1", "2", "3", "4"], "假频率锚"),
    sections: null,
    items: [fakeItem("t1", 1, { name: "假名甲" }), fakeItem("t2", 2, { name: "假名乙" })],
    examiner_panel: null,
    scoring: null,
    transcription_notes: [],
  };
}

function examinerDefinition() {
  return {
    schema_version: "questionnaire-definition.v1",
    questionnaire_id: "fake_examiner_v1",
    title: "假想认知检查",
    short_name: "FAKE-D",
    respondent: "examiner_administered",
    status: "prototype",
    provenance: provenance(),
    instruction: "假指导语四句",
    response_kind: "examiner_scored",
    value_field: null,
    element_field: null,
    present_field: null,
    severity_field: null,
    frequency_field: null,
    sections: null,
    items: null,
    examiner_panel: {
      domains: [
        { domain_key: "alpha", title: "假域甲", max_score: 4 },
        { domain_key: "beta", title: "假域乙", max_score: 2 },
      ],
      items: [
        { item_key: "e1", no: 1, domain_key: "alpha", name: "假名一", text: "假题干1",
          entry: { kind: "score", max: 2, scored: true, bins: null, choice: null } },
        { item_key: "e2", no: 2, domain_key: "alpha", name: "假名二", text: "假题干2",
          entry: { kind: "count", max: 10, scored: true, choice: null, bins: [
            { min: 5, max: null, score: 2 }, { min: 2, max: 4, score: 1 }, { min: 0, max: 1, score: 0 },
          ] } },
        { item_key: "e3", no: 3, domain_key: "alpha", name: "假名三", text: "假题干3",
          entry: { kind: "count", max: 3, scored: false, bins: null, choice: null } },
        { item_key: "e4", no: 4, domain_key: "beta", name: "假名四", text: "假题干4",
          entry: { kind: "choice", max: null, scored: false, bins: null,
            choice: choice(["组甲", "组乙"], "假组") } },
        { item_key: "e5", no: 5, domain_key: "beta", name: "假名五", text: "假题干5",
          entry: { kind: "count", max: 2, scored: true, bins: null, choice: null } },
      ],
    },
    scoring: {
      kind: "examiner_sum",
      scoring_rule_id: "fake.examiner.v1",
      max_score: 6,
      cutoff: null,
      stratified_cutoff: {
        by_item: "e4",
        groups: { "组甲": { operator: "<=", value: 3, label: "假达界甲" }, "组乙": null },
        unjudged_label: "假未判定",
      },
      rule_verbatim: "示例计分",
    },
    transcription_notes: [],
  };
}

function firstAllowed(definition, kind) {
  if (kind === "symptom_triplet") return definition.present_field.allowed[0];
  if (kind === "examiner_scored") return "0";
  return definition.value_field.allowed[0];
}

function slot(itemKey, fieldKey, overrides = {}) {
  return {
    item_key: itemKey,
    field_key: fieldKey,
    ai_draft_value: null,
    ai_draft_rationale: null,
    final_value: null,
    value_source: null,
    ...overrides,
  };
}

function recordFor(definition, overrides = {}) {
  return {
    schema_version: "questionnaire-record.v1",
    record_id: "qr_render1",
    patient_id: "P-01",
    questionnaire_id: definition.questionnaire_id,
    definition_sha256: SHA,
    phase_label: "前测",
    status: "draft",
    created_by: "tester",
    created_at: "2026-08-20T01:02:03",
    locked_by: null,
    locked_at: null,
    ai_draft_status: "none",
    ai_draft_engine: null,
    ai_draft_at: null,
    computed_total: null,
    cutoff_met: null,
    computed_flag: null,
    scoring_rule_id: null,
    note: null,
    values: [],
    ...overrides,
  };
}

const noopClient = {
  putValues: async () => { throw new Error("SSR 里不应发请求"); },
  generateAiDraft: async () => { throw new Error("SSR 里不应发请求"); },
  lock: async () => { throw new Error("SSR 里不应发请求"); },
};

async function render(definition, record, definitionDrifted = false) {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  try {
    const { QuestionnaireDrawer } = await vite.ssrLoadModule(
      "/src/console/QuestionnaireDrawer.tsx",
    );
    return renderToStaticMarkup(React.createElement(QuestionnaireDrawer, {
      initialRecord: record,
      definition,
      definitionDrifted,
      client: noopClient,
      onClose() {},
      onRecordUpdated() {},
    }));
  } finally {
    await vite.close();
  }
}

function countOccurrences(markup, needle) {
  return markup.split(needle).length - 1;
}

test("ordinal_sections:每条目 8 档 + 每节要素 5 档,锚点对照可见,AI 建议与采纳全部在场", async () => {
  const markup = await render(ordinalDefinition(), recordFor(ordinalDefinition(), {
    ai_draft_status: "generated",
    ai_draft_engine: "fake-engine",
    ai_draft_at: "2026-08-20T02:00:00",
    values: [
      slot("i1", "value", { ai_draft_value: "7", ai_draft_rationale: "假理由甲" }),
      slot("i2", "value", { ai_draft_value: "6", final_value: "6", value_source: "ai_accepted" }),
    ],
  }));
  // 2 条目 × 8 档 + 2 要素 × 5 档 = 26 个档位按钮。
  assert.equal(countOccurrences(markup, "segmented-control__button"), 26);
  // 锚点对照对触屏可见(iPad 没有悬停),不能只藏在 title 里。
  assert.ok(markup.includes("7=假锚7"));
  assert.ok(markup.includes("5=假频5"));
  assert.ok(markup.includes("假题干1"));
  assert.ok(markup.includes("假要素乙"));
  // AI 建议明确标成建议,带理由;有草稿值未采纳时给一键采纳。
  assert.ok(markup.includes("AI 建议"));
  assert.ok(markup.includes("假理由甲"));
  assert.ok(markup.includes("采纳全部 AI 建议（1 项）"));
  // 已保存的终值渲染为按下态。
  assert.ok(markup.includes('aria-pressed="true"'));
  // 顶部常驻横幅原句。
  assert.ok(markup.includes("量表电子记录（试用），终确认前不作为正式研究结局"));
  // 工程名词不上屏。
  for (const banned of ["attempt", "fail-closed", "幂等"]) {
    assert.equal(markup.includes(banned), false, `渲染结果不得含 ${banned}`);
  }
});

test("binary_scored:每条目两键,键面是定义下发的词", async () => {
  const markup = await render(binaryDefinition(), recordFor(binaryDefinition()));
  // 2 条目 × 2 键 = 4 个档位按钮。
  assert.equal(countOccurrences(markup, "segmented-control__button"), 4);
  assert.ok(markup.includes(">是 答是<") || markup.includes(">是<"));
  assert.equal(countOccurrences(markup, "假题干"), 2);
  assert.ok(markup.includes("锁定这份记录"));
});

test("symptom_triplet:选「有」才展开严重度/频率,锚点全文可见", async () => {
  const markup = await render(tripletDefinition(), recordFor(tripletDefinition(), {
    values: [
      slot("t1", "present", { final_value: "有", value_source: "human_direct" }),
      slot("t2", "present", { final_value: "无", value_source: "human_direct" }),
    ],
  }));
  // 只有第 1 题展开:严重度锚点全文恰好出现一次(按钮文字,不只是 title)。
  assert.equal(countOccurrences(markup, ">1 假严重锚1<"), 1);
  assert.equal(countOccurrences(markup, ">4 假频率锚4<"), 1);
  assert.ok(markup.includes("严重度"));
  assert.ok(markup.includes("频率"));
  assert.ok(markup.includes("假名甲"));
  // 2 条目 × 2(有/无) + 展开的 3 档严重度 + 4 档频率 = 11。
  assert.equal(countOccurrences(markup, "segmented-control__button"), 11);
});

test("锁定后的 GDS 型量表显示 总分/界值标志,且全部档位只读", async () => {
  const markup = await render(binaryDefinition(), recordFor(binaryDefinition(), {
    status: "locked",
    locked_by: "tester",
    locked_at: "2026-08-20T02:00:00",
    computed_total: 2,
    cutoff_met: true,
    computed_flag: "达界示例",
    scoring_rule_id: "fake.rule.v1",
    values: [
      slot("b1", "value", { final_value: "是", value_source: "human_direct" }),
      slot("b2", "value", { final_value: "否", value_source: "human_direct" }),
    ],
  }));
  assert.ok(markup.includes("总分 2 / 3 · 达界示例"));
  assert.ok(markup.includes("已锁定"));
  // 锁定后不再出现 AI 初评入口与锁定按钮,档位按钮全部禁用。
  assert.equal(markup.includes("AI 初评（仅供核对）"), false);
  assert.equal(markup.includes("锁定这份记录"), false);
  assert.equal(
    countOccurrences(markup, "segmented-control__button"),
    countOccurrences(markup, 'segmented-control__button" aria-pressed="false" disabled')
    + countOccurrences(markup, 'segmented-control__button" aria-pressed="true" disabled'));
});

test("定义包漂移:只读并解释原因,不再给 AI 初评与锁定入口", async () => {
  const markup = await render(ordinalDefinition(), recordFor(ordinalDefinition()), true);
  assert.ok(markup.includes("量表定义已更新，这份记录只能查看"));
  assert.equal(markup.includes("AI 初评（仅供核对）"), false);
  assert.equal(markup.includes("锁定这份记录"), false);
  assert.equal(
    countOccurrences(markup, "segmented-control__button"),
    countOccurrences(markup, 'segmented-control__button" aria-pressed="false" disabled')
    + countOccurrences(markup, 'segmented-control__button" aria-pressed="true" disabled'));
});

test("examiner_scored:记分框是档位按钮、计数框是数字输入、分档换算与各域小计在场", async () => {
  const markup = await render(examinerDefinition(), recordFor(examinerDefinition(), {
    values: [
      slot("e1", "value", { final_value: "2", value_source: "human_direct" }),
      slot("e2", "value", { final_value: "4", value_source: "human_direct" }),
    ],
  }));
  // e1 记分框 0/1/2 三键 + e4 分组两键 = 5 个档位按钮;e2/e3/e5 三个数字输入。
  assert.equal(countOccurrences(markup, "segmented-control__button"), 5);
  assert.equal(countOccurrences(markup, 'inputMode="numeric"'), 3);
  assert.equal(countOccurrences(markup, 'type="number"'), 0);
  assert.ok(markup.includes("假域甲"));
  assert.ok(markup.includes("假域乙"));
  assert.ok(markup.includes("假题干5"));
  assert.ok(markup.includes("假名二"));
  // 总数 4 落在 2-4 档 → 1 分,换算结果对施测者可见。
  assert.ok(markup.includes("换算得分 1 / 2"));
  // 不计分的记录栏明说不计分。
  assert.ok(markup.includes("不计分，仅记录"));
  // 小计:假域甲 2+1=3/4,假域乙还差 1 项;总分 3 / 6。
  assert.ok(markup.includes("3 / 4"));
  assert.ok(markup.includes("还有 1 项未评"));
  assert.ok(markup.includes("总分 3 / 6"));
  // 不渲染其他三种题型的作答面。
  assert.equal(markup.includes("严重度"), false);
  assert.equal(markup.includes("本节沟通要素"), false);
  for (const banned of ["attempt", "fail-closed", "幂等", "examiner", "bins"]) {
    assert.equal(markup.includes(banned), false, `渲染结果不得含 ${banned}`);
  }
});

// 点错一格必须退得回「未评」。原来 ChoiceButtons 只有 onSelect,再点已选项也只是
// 重写同值;唯一能写 null 的是计数框的清空分支。施测者点错一格就只能带着错值锁定,
// 而「未评」是锁定门禁的判据——门禁于是形同虚设。
test("四种题型:已填的条目都给得出「退回未评」,未填的不给", async () => {
  const cases = [
    ["ordinal_sections", ordinalDefinition, "i1", "value"],
    ["binary_scored", binaryDefinition, "b1", "value"],
    ["symptom_triplet", tripletDefinition, "t1", "present"],
    ["examiner_scored", examinerDefinition, "e1", "value"],
  ];
  for (const [kind, make, itemKey, fieldKey] of cases) {
    const definition = make();
    const filled = await render(definition, recordFor(definition, {
      values: [slot(itemKey, fieldKey, { final_value: firstAllowed(definition, kind) })],
    }));
    assert.ok(filled.includes("退回未评"),
      `${kind}: 条目已填,却没有退回未评的入口`);
    const empty = await render(definition, recordFor(definition, { values: [] }));
    assert.equal(empty.includes("退回未评"), false,
      `${kind}: 一条都没填,不该出现退回未评`);
  }
});

test("锁定之后不给退回未评——锁了就不能改", async () => {
  const definition = binaryDefinition();
  const markup = await render(definition, recordFor(definition, {
    status: "locked",
    values: [slot("b1", "value", { final_value: firstAllowed(definition, "binary_scored") })],
  }));
  assert.equal(markup.includes("退回未评"), false);
});
