import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  QUESTIONNAIRE_TRIAL_NOTICE,
  adoptableAiDraftEntries,
  aiDraftStatusLine,
  examinerDomainTotals,
  examinerEntryPoints,
  lockedScoreSummary,
  missingLockEntries,
  parseQuestionnaireCatalog,
  parseQuestionnaireRecord,
  parseQuestionnaireRecordList,
  performQuestionnaireMutation,
  questionnaireRetakeNote,
  questionnaireFailureText,
  questionnaireSlotKey,
  type QuestionnaireDefinition,
  type QuestionnaireItemValueSlot,
  type QuestionnaireRecord,
} from "./questionnaires.ts";

// 全部夹具都是微型假定义;绝不复制真实题词/锚点。
const SHA = "a".repeat(64);

function choice(allowed: string[], anchorPrefix: string) {
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

function fakeItem(key: string, no: number, overrides: Record<string, unknown> = {}) {
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
    sections: [{ section_id: "s1", title: "假节甲", items: [fakeItem("i1", 1), fakeItem("i2", 2)] }],
    items: null,
    examiner_panel: null,
    scoring: null,
    transcription_notes: ["假转录注一"],
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
    items: [
      fakeItem("b1", 1, { score_when: "是" }),
      fakeItem("b2", 2, { score_when: "否" }),
      fakeItem("b3", 3, { score_when: "是" }),
    ],
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

function catalog(definitions: Record<string, unknown>[]) {
  return {
    schema_version: "questionnaire-catalog.v1",
    questionnaires: definitions.map((definition) => ({
      content_sha256: SHA,
      definition,
    })),
  };
}

function slot(
  itemKey: string, fieldKey: string, overrides: Partial<QuestionnaireItemValueSlot> = {},
) {
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

function recordFor(
  definition: { questionnaire_id: string }, overrides: Record<string, unknown> = {},
) {
  return {
    schema_version: "questionnaire-record.v1",
    record_id: "qr_test1",
    patient_id: "P-01",
    questionnaire_id: definition.questionnaire_id,
    definition_sha256: SHA,
    phase_label: "前测",
    phase_ordinal: 1,
    superseded_by_ordinal: null,
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

function parsedDefinition(raw: Record<string, unknown>): QuestionnaireDefinition {
  return parseQuestionnaireCatalog(catalog([raw]))[0].definition;
}

function parsedRecord(raw: Record<string, unknown>): QuestionnaireRecord {
  return parseQuestionnaireRecord(raw);
}

test("目录解析:三种量表恰好的键与形状全部通过", () => {
  const entries = parseQuestionnaireCatalog(
    catalog([ordinalDefinition(), binaryDefinition(), tripletDefinition()]));
  assert.equal(entries.length, 3);
  assert.deepEqual(
    entries.map((entry) => entry.definition.response_kind),
    ["ordinal_sections", "binary_scored", "symptom_triplet"]);
  assert.equal(entries[0].content_sha256, SHA);
  assert.equal(entries[1].definition.scoring?.cutoff_value, 2);
  assert.equal(entries[0].definition.sections?.[0].items.length, 2);
});

test("目录解析:多一键/少一键/错 schema_version 整包拒收", () => {
  assert.throws(
    () => parseQuestionnaireCatalog({ ...catalog([ordinalDefinition()]), extra: 1 }),
    /未允许字段/);
  const extraEntryKey = catalog([ordinalDefinition()]);
  (extraEntryKey.questionnaires[0] as Record<string, unknown>).extra = 1;
  assert.throws(() => parseQuestionnaireCatalog(extraEntryKey), /未允许字段/);
  const extraDefinitionKey = catalog([{ ...ordinalDefinition(), extra: 1 }]);
  assert.throws(() => parseQuestionnaireCatalog(extraDefinitionKey), /未允许字段/);
  const missingTitle = ordinalDefinition() as Record<string, unknown>;
  delete missingTitle.title;
  assert.throws(() => parseQuestionnaireCatalog(catalog([missingTitle])), /字段不完整/);
  assert.throws(
    () => parseQuestionnaireCatalog({ ...catalog([ordinalDefinition()]), schema_version: "questionnaire-catalog.v2" }),
    /schema_version/);
});

test("目录解析:anchors 键与 allowed 不一致拒收", () => {
  const definition = ordinalDefinition();
  delete definition.value_field.anchors["N"];
  assert.throws(
    () => parseQuestionnaireCatalog(catalog([definition])),
    /anchors 键必须与 allowed 完全一致/);
});

test("定义解析:response_kind 的字段组合合同", () => {
  const ordinalWithoutElements = { ...ordinalDefinition(), element_field: null };
  assert.throws(
    () => parseQuestionnaireCatalog(catalog([ordinalWithoutElements])),
    /ordinal_sections 的字段组合不合法/);
  const binaryWithoutScoring = { ...binaryDefinition(), scoring: null };
  assert.throws(
    () => parseQuestionnaireCatalog(catalog([binaryWithoutScoring])),
    /binary_scored 的字段组合不合法/);
  const tripletWrongPresent = {
    ...tripletDefinition(),
    present_field: choice(["是", "否"], "记"),
  };
  assert.throws(
    () => parseQuestionnaireCatalog(catalog([tripletWrongPresent])),
    /只允许 有\/无/);
});

test("记录解析:通过并执行受试者/记录绑定期望", () => {
  const record = parseQuestionnaireRecord(
    recordFor(binaryDefinition(), {
      values: [slot("b1", "value", { final_value: "是", value_source: "human_direct" })],
    }),
    { patientId: "P-01", recordId: "qr_test1" });
  assert.equal(record.values[0].final_value, "是");
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition()), { patientId: "P-99" }),
    /不属于当前受试者/);
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition()), { recordId: "qr_other" }),
    /不属于当前记录/);
});

test("记录解析:多键/枚举外值/锁定一致性/重复槽位全部拒收", () => {
  assert.throws(
    () => parseQuestionnaireRecord({ ...recordFor(binaryDefinition()), extra: 1 }),
    /未允许字段/);
  const missingNote = recordFor(binaryDefinition()) as Record<string, unknown>;
  delete missingNote.note;
  assert.throws(() => parseQuestionnaireRecord(missingNote), /字段不完整/);
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition(), { ai_draft_status: "weird" })),
    /不在冻结枚举中/);
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition(), { status: "locked" })),
    /锁定态必须带锁定人与锁定时间/);
  assert.throws(
    () => parseQuestionnaireRecord(
      recordFor(binaryDefinition(), { locked_at: "2026-08-20T02:00:00" })),
    /草稿态不能带锁定人\/锁定时间/);
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition(), {
      values: [slot("b1", "value"), slot("b1", "value")],
    })),
    /出现多行/);
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition(), {
      values: [{ ...slot("b1", "value"), extra: 1 }],
    })),
    /未允许字段/);
});

test("清单解析:schema 钉死,内嵌记录同样绑定受试者", () => {
  const list = parseQuestionnaireRecordList({
    schema_version: "questionnaire-record-list.v1",
    records: [recordFor(binaryDefinition())],
  }, { patientId: "P-01" });
  assert.equal(list.length, 1);
  assert.throws(
    () => parseQuestionnaireRecordList({
      schema_version: "questionnaire-record.v1",
      records: [],
    }),
    /schema_version/);
  assert.throws(
    () => parseQuestionnaireRecordList({
      schema_version: "questionnaire-record-list.v1",
      records: [recordFor(binaryDefinition())],
    }, { patientId: "P-99" }),
    /不属于当前受试者/);
});

test("采纳全部:只挑有草稿值且当前生效值不同的条目", () => {
  const record = parsedRecord(recordFor(ordinalDefinition(), {
    ai_draft_status: "generated",
    values: [
      slot("i1", "value", { ai_draft_value: "7", ai_draft_rationale: "假理由甲" }),
      slot("i2", "value", {
        ai_draft_value: "6", final_value: "6", value_source: "ai_accepted",
      }),
      slot("section:s1", "element:alpha", {
        ai_draft_value: "5", final_value: "4", value_source: "ai_overridden",
      }),
    ],
  }));
  const entries = adoptableAiDraftEntries(record);
  assert.deepEqual(entries, [
    { item_key: "i1", field_key: "value", value: "7" },
    { item_key: "section:s1", field_key: "element:alpha", value: "5" },
  ]);
  // 屏上刚点过但还没保存成功的档位同样算「当前生效值」。
  const pending = new Map<string, string | null>([
    [questionnaireSlotKey("i1", "value"), "7"],
  ]);
  assert.deepEqual(adoptableAiDraftEntries(record, pending), [
    { item_key: "section:s1", field_key: "element:alpha", value: "5" },
  ]);
});

test("AI 初评各状态给一句人话", () => {
  assert.equal(aiDraftStatusLine("none"), null);
  assert.match(aiDraftStatusLine("generated") ?? "", /逐项核对/);
  assert.match(aiDraftStatusLine("not_applicable") ?? "", /此量表不提供 AI 初评/);
  assert.match(aiDraftStatusLine("unavailable_not_authorized") ?? "", /该受试者未授权云处理/);
  assert.match(aiDraftStatusLine("unavailable_no_data") ?? "", /暂无训练数据可供参考/);
  assert.match(aiDraftStatusLine("failed") ?? "", /AI 初评没有成功/);
  assert.match(aiDraftStatusLine("failed") ?? "", /请直接人工评定/);
});

test("缺项预览:ordinal 数条目与每节要素", () => {
  const definition = parsedDefinition(ordinalDefinition());
  const empty = parsedRecord(recordFor(ordinalDefinition()));
  const missing = missingLockEntries(definition, empty);
  assert.equal(missing.length, 4);
  assert.ok(missing.some((entry) => entry.includes("第1题未评")));
  assert.ok(missing.some((entry) => entry.includes("假要素乙")));
  const full = parsedRecord(recordFor(ordinalDefinition(), {
    values: [
      slot("i1", "value", { final_value: "7", value_source: "human_direct" }),
      slot("i2", "value", { final_value: "N", value_source: "human_direct" }),
      slot("section:s1", "element:alpha", { final_value: "5", value_source: "human_direct" }),
      slot("section:s1", "element:beta", { final_value: "1", value_source: "human_direct" }),
    ],
  }));
  assert.deepEqual(missingLockEntries(definition, full), []);
});

test("缺项预览:symptom_triplet 的条件必填与矛盾", () => {
  const definition = parsedDefinition(tripletDefinition());
  const record = parsedRecord(recordFor(tripletDefinition(), {
    values: [
      slot("t1", "present", { final_value: "有", value_source: "human_direct" }),
      slot("t2", "present", { final_value: "无", value_source: "human_direct" }),
      slot("t2", "severity", { final_value: "2", value_source: "human_direct" }),
    ],
  }));
  const missing = missingLockEntries(definition, record);
  assert.ok(missing.some((entry) => entry.includes("第1题缺严重度")));
  assert.ok(missing.some((entry) => entry.includes("第1题缺频率")));
  assert.ok(missing.some((entry) => entry.includes("第2题记为「无」但仍带严重度/频率")));
  const noPresent = parsedRecord(recordFor(tripletDefinition()));
  assert.ok(missingLockEntries(definition, noPresent)
    .some((entry) => entry.includes("第1题未记录「有/无」")));
});

test("错误翻译:错误码翻成人话,problems 逐条透传", () => {
  const locked = questionnaireFailureText({
    detail: "[409] 记录已锁定",
    detailData: {
      code: "questionnaire_record_locked",
      message: "记录已锁定，不可再改；改错请新建记录",
    },
  });
  assert.match(locked.message, /锁定/);
  assert.match(locked.message, /新建记录/);
  const incomplete = questionnaireFailureText({
    detailData: {
      code: "questionnaire_lock_incomplete",
      message: "作答尚不满足锁定完整性合同",
      problems: ["缺少 (b1, value)", "缺少 (b2, value)"],
    },
  });
  assert.match(incomplete.message, /暂时不能锁定/);
  assert.deepEqual(incomplete.problems, ["缺少 (b1, value)", "缺少 (b2, value)"]);
  assert.equal(questionnaireFailureText(new Error("网络断了")).message, "网络断了");
  assert.equal(questionnaireFailureText({}).message, "操作没有成功，请重试。");
});

test("先 await 成功才清本地:失败绝不触发 onSuccess", async () => {
  let successCalls = 0;
  const failed = await performQuestionnaireMutation(
    () => Promise.reject(new Error("保存失败")),
    () => { successCalls += 1; });
  assert.equal(failed.ok, false);
  assert.equal(successCalls, 0);
  const succeeded = await performQuestionnaireMutation(
    () => Promise.resolve("回执"),
    (value) => { successCalls += 1; assert.equal(value, "回执"); });
  assert.equal(succeeded.ok, true);
  assert.equal(successCalls, 1);
});

test("总分一句话:有计分的显示分母与界值,未达界不发明标签", () => {
  const definition = parsedDefinition(binaryDefinition());
  const met = parsedRecord(recordFor(binaryDefinition(), {
    status: "locked", locked_by: "tester", locked_at: "2026-08-20T02:00:00",
    computed_total: 2, cutoff_met: true, computed_flag: "达界示例",
    scoring_rule_id: "fake.rule.v1",
  }));
  assert.equal(lockedScoreSummary(met, definition), "总分 2 / 3 · 达界示例");
  const unmet = parsedRecord(recordFor(binaryDefinition(), {
    status: "locked", locked_by: "tester", locked_at: "2026-08-20T02:00:00",
    computed_total: 1, cutoff_met: false, computed_flag: null,
    scoring_rule_id: "fake.rule.v1",
  }));
  assert.match(lockedScoreSummary(unmet, definition) ?? "", /未达界值/);
  assert.match(lockedScoreSummary(unmet, definition) ?? "", />= 2/);
  assert.equal(lockedScoreSummary(parsedRecord(recordFor(binaryDefinition())), definition), null);
});

test("文案钉:试用横幅原句 + 两张屏禁工程名词上屏", () => {
  assert.equal(
    QUESTIONNAIRE_TRIAL_NOTICE,
    "量表电子记录（试用），终确认前不作为正式研究结局");
  const panelSource = readFileSync(
    new URL("./QuestionnairePanel.tsx", import.meta.url), "utf8");
  const drawerSource = readFileSync(
    new URL("./QuestionnaireDrawer.tsx", import.meta.url), "utf8");
  assert.match(panelSource, /QUESTIONNAIRE_TRIAL_NOTICE/);
  assert.match(drawerSource, /QUESTIONNAIRE_TRIAL_NOTICE/);
  for (const banned of ["attempt", "fail-closed", "幂等", "idempot", "schema_version"]) {
    assert.equal(panelSource.toLowerCase().includes(banned), false,
      `QuestionnairePanel 不得出现 ${banned}`);
    assert.equal(drawerSource.toLowerCase().includes(banned), false,
      `QuestionnaireDrawer 不得出现 ${banned}`);
  }
});

test("P2-5:预检已知服务器会拒绝锁定时,确认框的锁定钮禁用", () => {
  const drawer = readFileSync(new URL("./QuestionnaireDrawer.tsx", import.meta.url), "utf8");
  assert.match(drawer, /confirmDisabled=\{missing\.length > 0\}/);
  assert.match(drawer, /暂不能锁定——请先补齐/);
});

// ---------------- examiner_scored ----------------

test("examiner_scored:定义解析通过,四种量表同目录", () => {
  const entries = parseQuestionnaireCatalog(catalog([
    ordinalDefinition(), binaryDefinition(), tripletDefinition(), examinerDefinition()]));
  assert.deepEqual(
    entries.map((entry) => entry.definition.response_kind),
    ["ordinal_sections", "binary_scored", "symptom_triplet", "examiner_scored"]);
  const examiner = entries[3].definition;
  assert.equal(examiner.examiner_panel?.items.length, 5);
  assert.equal(examiner.scoring?.kind, "examiner_sum");
  assert.equal(examiner.examiner_panel?.items[1].entry.bins?.[0].max, null);
});

test("examiner_scored:旧夹具少了 examiner_panel 键整包拒收(exactKeys 对新键同样生效)", () => {
  const stale = ordinalDefinition() as Record<string, unknown>;
  delete stale.examiner_panel;
  assert.throws(() => parseQuestionnaireCatalog(catalog([stale])), /字段不完整/);
});

test("examiner_scored:重叠分档/错放 examiner_panel/分组键不齐/score 不计分 全部拒收", () => {
  const overlapping = examinerDefinition();
  overlapping.examiner_panel.items[1].entry.bins = [
    { min: 5, max: null, score: 2 }, { min: 3, max: 4, score: 1 }, { min: 0, max: 3, score: 0 },
  ];
  assert.throws(() => parseQuestionnaireCatalog(catalog([overlapping])), /连续且不重叠/);

  const misplaced = { ...ordinalDefinition(), examiner_panel: examinerDefinition().examiner_panel };
  assert.throws(() => parseQuestionnaireCatalog(catalog([misplaced])), /字段组合不合法/);

  const groups = examinerDefinition();
  delete (groups.scoring.stratified_cutoff.groups as Record<string, unknown>)["组乙"];
  assert.throws(() => parseQuestionnaireCatalog(catalog([groups])), /组键与 allowed 一致/);

  const unscored = examinerDefinition();
  unscored.examiner_panel.items[0].entry.scored = false;
  assert.throws(() => parseQuestionnaireCatalog(catalog([unscored])), /scored 必须为 true/);

  const binaryScoring = { ...examinerDefinition(), scoring: binaryDefinition().scoring };
  assert.throws(() => parseQuestionnaireCatalog(catalog([binaryScoring])), /字段组合不合法/);
});

test("examiner_scored:录入值换算——记分直取、计数查档、越界与非规范整数为 null、不计分恒 0", () => {
  const definition = parsedDefinition(examinerDefinition());
  const [score, binned, aux, group, direct] = definition.examiner_panel!.items.map((item) => item.entry);
  assert.equal(examinerEntryPoints(score, "2"), 2);
  assert.equal(examinerEntryPoints(score, "3"), null);
  assert.equal(examinerEntryPoints(score, null), null);
  assert.equal(examinerEntryPoints(binned, "0"), 0);
  assert.equal(examinerEntryPoints(binned, "1"), 0);
  assert.equal(examinerEntryPoints(binned, "2"), 1);
  assert.equal(examinerEntryPoints(binned, "4"), 1);
  assert.equal(examinerEntryPoints(binned, "5"), 2);
  assert.equal(examinerEntryPoints(binned, "10"), 2);
  assert.equal(examinerEntryPoints(binned, "11"), null);
  assert.equal(examinerEntryPoints(binned, "05"), null);
  assert.equal(examinerEntryPoints(binned, "1.5"), null);
  assert.equal(examinerEntryPoints(aux, "3"), 0);
  assert.equal(examinerEntryPoints(group, "组甲"), 0);
  assert.equal(examinerEntryPoints(direct, "2"), 2);
});

test("examiner_scored:认知域小计只累加合法录入,缺项计数", () => {
  const definition = parsedDefinition(examinerDefinition());
  const values = new Map([["e1", "2"], ["e2", "4"], ["e3", "1"]]);
  const totals = examinerDomainTotals(
    definition.examiner_panel!, (key) => values.get(key) ?? null);
  assert.deepEqual(totals.map((entry) => [entry.domain.title, entry.points, entry.missing]), [
    ["假域甲", 3, 0], ["假域乙", 0, 1],
  ]);
});

test("examiner_scored:缺项预览带认知域标题,不计分的记录栏同样必填", () => {
  const definition = parsedDefinition(examinerDefinition());
  const record = parsedRecord(recordFor(definition, {
    values: [slot("e1", "value", { final_value: "1", value_source: "human_direct" })],
  }));
  assert.deepEqual(missingLockEntries(definition, record), [
    "「假域甲」第2题未评", "「假域甲」第3题未评", "「假域乙」第4题未评", "「假域乙」第5题未评",
  ]);
});

test("examiner_scored:锁定摘要 = 总分/满分 + 各域小计 + 界值三态", () => {
  const definition = parsedDefinition(examinerDefinition());
  const values = (group: string) => [
    slot("e1", "value", { final_value: "2", value_source: "human_direct" }),
    slot("e2", "value", { final_value: "3", value_source: "human_direct" }),
    slot("e3", "value", { final_value: "0", value_source: "human_direct" }),
    slot("e4", "value", { final_value: group, value_source: "human_direct" }),
    slot("e5", "value", { final_value: "2", value_source: "human_direct" }),
  ];
  const locked = (overrides: Record<string, unknown>) => parsedRecord(recordFor(definition, {
    status: "locked", locked_by: "tester", locked_at: "2026-08-27T01:00:00",
    computed_total: 5, scoring_rule_id: "fake.examiner.v1", ...overrides,
  }));
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: false, computed_flag: null, values: values("组甲") }), definition),
    "总分 5 / 6 · 假域甲 3/4 · 假域乙 2/2 · 高于界值（界值 ≤ 3 分）");
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: true, computed_flag: "假达界甲", values: values("组甲") }), definition),
    "总分 5 / 6 · 假域甲 3/4 · 假域乙 2/2 · 假达界甲");
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: null, computed_flag: "假未判定", values: values("组乙") }), definition),
    "总分 5 / 6 · 假域甲 3/4 · 假域乙 2/2 · 假未判定");
  // 定义包读不到时退回不带满分/小计的一行话;没有界值判定就不编造「未达界值」。
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: null, computed_flag: null, values: values("组乙") }), null),
    "总分 5");
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: null, computed_flag: "假未判定", values: values("组乙") }), null),
    "总分 5 · 假未判定");
  assert.equal(
    lockedScoreSummary(locked({ cutoff_met: false, computed_flag: null, values: values("组甲") }), null),
    "总分 5 · 未达界值");
});

test("同期别重测:序号与取代关系要说成人话,只有一次时不写「第 1 次」", () => {
  assert.equal(
    questionnaireRetakeNote({ phase_ordinal: 1, superseded_by_ordinal: null }),
    null, "只施测过一次就别在界面上写「第 1 次」");
  assert.equal(
    questionnaireRetakeNote({ phase_ordinal: 2, superseded_by_ordinal: null }),
    "第 2 次施测 · 当前作数的一条");
  assert.equal(
    questionnaireRetakeNote({ phase_ordinal: 1, superseded_by_ordinal: 2 }),
    "第 1 次施测 · 已被第 2 次取代，不作数");
});

test("解析器拒收自相矛盾的取代指针", () => {
  assert.throws(
    () => parseQuestionnaireRecord(recordFor(binaryDefinition(), {
      phase_ordinal: 3, superseded_by_ordinal: 2,
    })),
    /取代它的那次序号必须比自己大/);
});
