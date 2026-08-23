// 量表电子记录（原型道）的前端契约:exactKeys 解析器 + 纯逻辑。
// 题词/锚点全部来自认证接口下发的定义包;本模块与两张屏永不硬编码任何题词,
// 测试一律用微型假定义。多一键或少一键的响应整包拒收,绝不静默放行。

export const QUESTIONNAIRE_CATALOG_SCHEMA = "questionnaire-catalog.v1";
export const QUESTIONNAIRE_RECORD_SCHEMA = "questionnaire-record.v1";
export const QUESTIONNAIRE_RECORD_LIST_SCHEMA = "questionnaire-record-list.v1";
export const QUESTIONNAIRE_DEFINITION_SCHEMA = "questionnaire-definition.v1";

// 顶部常驻横幅原句(有测试钉着,改字先改测试)。
export const QUESTIONNAIRE_TRIAL_NOTICE =
  "量表电子记录（试用），终确认前不作为正式研究结局";

export const QUESTIONNAIRE_PHASE_LABELS = ["前测", "后测", "随访", "其他"] as const;
export type QuestionnairePhaseLabel = typeof QUESTIONNAIRE_PHASE_LABELS[number];

export type QuestionnaireResponseKind =
  | "ordinal_sections" | "binary_scored" | "symptom_triplet";
export type QuestionnaireAiDraftStatus =
  | "none" | "not_applicable" | "generated"
  | "unavailable_no_data" | "unavailable_not_authorized" | "failed";
export type QuestionnaireValueSource = "human_direct" | "ai_accepted" | "ai_overridden";

export interface QuestionnaireChoiceField {
  allowed: string[];
  anchors: Record<string, string>;
}

export interface QuestionnaireElement {
  element_key: string;
  label: string;
}

export interface QuestionnaireElementField extends QuestionnaireChoiceField {
  elements: QuestionnaireElement[];
}

export interface QuestionnaireItem {
  item_key: string;
  no: number;
  text: string;
  name: string | null;
  score_when: string | null;
}

export interface QuestionnaireSection {
  section_id: string;
  title: string;
  items: QuestionnaireItem[];
}

export interface QuestionnaireScoring {
  kind: "binary_sum";
  scoring_rule_id: string;
  max_score: number;
  cutoff_value: number;
  cutoff_operator: ">=";
  cutoff_label: string;
  rule_verbatim: string;
}

export interface QuestionnaireProvenance {
  provided_by: string;
  provided_via: string;
  provided_on: string;
  source_file: string;
  source_sha256: string;
  final_confirmation: string;
}

export interface QuestionnaireDefinition {
  schema_version: typeof QUESTIONNAIRE_DEFINITION_SCHEMA;
  questionnaire_id: string;
  title: string;
  short_name: string;
  respondent: "observer" | "patient_reported";
  status: "prototype";
  provenance: QuestionnaireProvenance;
  instruction: string;
  response_kind: QuestionnaireResponseKind;
  value_field: QuestionnaireChoiceField | null;
  element_field: QuestionnaireElementField | null;
  present_field: QuestionnaireChoiceField | null;
  severity_field: QuestionnaireChoiceField | null;
  frequency_field: QuestionnaireChoiceField | null;
  sections: QuestionnaireSection[] | null;
  items: QuestionnaireItem[] | null;
  scoring: QuestionnaireScoring | null;
  transcription_notes: string[];
}

export interface QuestionnaireCatalogEntry {
  content_sha256: string;
  definition: QuestionnaireDefinition;
}

export interface QuestionnaireItemValueSlot {
  item_key: string;
  field_key: string;
  ai_draft_value: string | null;
  ai_draft_rationale: string | null;
  final_value: string | null;
  value_source: QuestionnaireValueSource | null;
}

export interface QuestionnaireRecord {
  schema_version: typeof QUESTIONNAIRE_RECORD_SCHEMA;
  record_id: string;
  patient_id: string;
  questionnaire_id: string;
  definition_sha256: string;
  phase_label: QuestionnairePhaseLabel;
  status: "draft" | "locked";
  created_by: string;
  created_at: string;
  locked_by: string | null;
  locked_at: string | null;
  ai_draft_status: QuestionnaireAiDraftStatus;
  ai_draft_engine: string | null;
  ai_draft_at: string | null;
  computed_total: number | null;
  cutoff_met: boolean | null;
  computed_flag: string | null;
  scoring_rule_id: string | null;
  note: string | null;
  values: QuestionnaireItemValueSlot[];
}

export interface QuestionnaireValueWrite {
  item_key: string;
  field_key: string;
  value: string | null;
}

export interface QuestionnaireRecordExpectation {
  patientId?: string;
  recordId?: string;
}

const CATALOG_KEYS = ["schema_version", "questionnaires"] as const;
const CATALOG_ENTRY_KEYS = ["content_sha256", "definition"] as const;
const DEFINITION_KEYS = [
  "schema_version", "questionnaire_id", "title", "short_name", "respondent",
  "status", "provenance", "instruction", "response_kind", "value_field",
  "element_field", "present_field", "severity_field", "frequency_field",
  "sections", "items", "scoring", "transcription_notes",
] as const;
const PROVENANCE_KEYS = [
  "provided_by", "provided_via", "provided_on", "source_file",
  "source_sha256", "final_confirmation",
] as const;
const CHOICE_KEYS = ["allowed", "anchors"] as const;
const ELEMENT_FIELD_KEYS = ["allowed", "anchors", "elements"] as const;
const ELEMENT_KEYS = ["element_key", "label"] as const;
const ITEM_KEYS = ["item_key", "no", "text", "name", "score_when"] as const;
const SECTION_KEYS = ["section_id", "title", "items"] as const;
const SCORING_KEYS = [
  "kind", "scoring_rule_id", "max_score", "cutoff_value",
  "cutoff_operator", "cutoff_label", "rule_verbatim",
] as const;
const RECORD_KEYS = [
  "schema_version", "record_id", "patient_id", "questionnaire_id",
  "definition_sha256", "phase_label", "status", "created_by", "created_at",
  "locked_by", "locked_at", "ai_draft_status", "ai_draft_engine", "ai_draft_at",
  "computed_total", "cutoff_met", "computed_flag", "scoring_rule_id",
  "note", "values",
] as const;
const VALUE_SLOT_KEYS = [
  "item_key", "field_key", "ai_draft_value", "ai_draft_rationale",
  "final_value", "value_source",
] as const;
const LIST_KEYS = ["schema_version", "records"] as const;

const SHA256_HEX = /^[0-9a-f]{64}$/;

type UnknownRecord = Record<string, unknown>;

function exactRecord(
  value: unknown,
  keys: readonly string[],
  label: string,
): UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label}不是对象`);
  }
  const row = value as UnknownRecord;
  const actual = Object.keys(row).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length
      || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label}字段不完整或包含未允许字段`);
  }
  return row;
}

function arrayValue(row: UnknownRecord, key: string, label: string): unknown[] {
  const value = row[key];
  if (!Array.isArray(value)) throw new Error(`${label}.${key}必须是数组`);
  return value;
}

function stringValue(row: UnknownRecord, key: string, label: string): string {
  const value = row[key];
  if (typeof value !== "string" || !value || value !== value.trim()) {
    throw new Error(`${label}.${key}必须是规范非空字符串`);
  }
  return value;
}

function nullableStringValue(
  row: UnknownRecord, key: string, label: string,
): string | null {
  if (row[key] === null) return null;
  return stringValue(row, key, label);
}

function nullableBooleanValue(
  row: UnknownRecord, key: string, label: string,
): boolean | null {
  const value = row[key];
  if (value === null) return null;
  if (typeof value !== "boolean") throw new Error(`${label}.${key}必须是布尔值或空`);
  return value;
}

function nullableFiniteNumberValue(
  row: UnknownRecord, key: string, label: string,
): number | null {
  const value = row[key];
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${label}.${key}必须是有限数值或空`);
  }
  return value;
}

function integerValue(
  row: UnknownRecord, key: string, label: string, minimum: number,
): number {
  const value = row[key];
  if (!Number.isInteger(value) || (value as number) < minimum) {
    throw new Error(`${label}.${key}必须是不小于 ${minimum} 的整数`);
  }
  return value as number;
}

function enumValue<T extends string>(
  row: UnknownRecord, key: string, values: readonly T[], label: string,
): T {
  const value = row[key];
  if (typeof value !== "string" || !values.includes(value as T)) {
    throw new Error(`${label}.${key}不在冻结枚举中`);
  }
  return value as T;
}

function literalValue<T extends string>(
  row: UnknownRecord, key: string, expected: T, label: string,
): T {
  if (row[key] !== expected) throw new Error(`${label}.${key}必须是 ${expected}`);
  return expected;
}

function sha256Value(row: UnknownRecord, key: string, label: string): string {
  const value = stringValue(row, key, label);
  if (!SHA256_HEX.test(value)) throw new Error(`${label}.${key}必须是 64 位十六进制摘要`);
  return value;
}

function parseChoiceField(
  value: unknown, label: string,
  keys: readonly string[] = CHOICE_KEYS,
): QuestionnaireChoiceField {
  const row = exactRecord(value, keys, label);
  const allowed = arrayValue(row, "allowed", label).map((entry, index) => {
    if (typeof entry !== "string" || !entry || entry !== entry.trim()) {
      throw new Error(`${label}.allowed[${index}]必须是规范非空字符串`);
    }
    return entry;
  });
  if (allowed.length < 2 || new Set(allowed).size !== allowed.length) {
    throw new Error(`${label}.allowed 必须是至少两个互不相同的值`);
  }
  const anchorsRaw = row.anchors;
  if (anchorsRaw === null || typeof anchorsRaw !== "object" || Array.isArray(anchorsRaw)) {
    throw new Error(`${label}.anchors 必须是对象`);
  }
  const anchors: Record<string, string> = {};
  const anchorKeys = Object.keys(anchorsRaw as UnknownRecord).sort();
  if (anchorKeys.length !== allowed.length
      || [...allowed].sort().some((key, index) => key !== anchorKeys[index])) {
    throw new Error(`${label}.anchors 键必须与 allowed 完全一致`);
  }
  for (const key of anchorKeys) {
    anchors[key] = stringValue(anchorsRaw as UnknownRecord, key, `${label}.anchors`);
  }
  return { allowed, anchors };
}

function parseElementField(value: unknown, label: string): QuestionnaireElementField {
  const row = exactRecord(value, ELEMENT_FIELD_KEYS, label);
  const choice = parseChoiceField(
    { allowed: row.allowed, anchors: row.anchors }, label);
  const elements = arrayValue(row, "elements", label).map((entry, index) => {
    const elementRow = exactRecord(entry, ELEMENT_KEYS, `${label}.elements[${index}]`);
    return {
      element_key: stringValue(elementRow, "element_key", `${label}.elements[${index}]`),
      label: stringValue(elementRow, "label", `${label}.elements[${index}]`),
    };
  });
  if (elements.length < 1
      || new Set(elements.map((element) => element.element_key)).size !== elements.length) {
    throw new Error(`${label}.elements 的 element_key 必须非空且唯一`);
  }
  return { ...choice, elements };
}

function parseItem(value: unknown, label: string): QuestionnaireItem {
  const row = exactRecord(value, ITEM_KEYS, label);
  return {
    item_key: stringValue(row, "item_key", label),
    no: integerValue(row, "no", label, 1),
    text: stringValue(row, "text", label),
    name: nullableStringValue(row, "name", label),
    score_when: nullableStringValue(row, "score_when", label),
  };
}

function parseScoring(value: unknown, label: string): QuestionnaireScoring {
  const row = exactRecord(value, SCORING_KEYS, label);
  return {
    kind: literalValue(row, "kind", "binary_sum", label),
    scoring_rule_id: stringValue(row, "scoring_rule_id", label),
    max_score: integerValue(row, "max_score", label, 1),
    cutoff_value: integerValue(row, "cutoff_value", label, 0),
    cutoff_operator: literalValue(row, "cutoff_operator", ">=", label),
    cutoff_label: stringValue(row, "cutoff_label", label),
    rule_verbatim: stringValue(row, "rule_verbatim", label),
  };
}

export function questionnaireAllItems(
  definition: QuestionnaireDefinition,
): QuestionnaireItem[] {
  if (definition.sections !== null) {
    return definition.sections.flatMap((section) => section.items);
  }
  return definition.items ?? [];
}

export function parseQuestionnaireDefinition(value: unknown): QuestionnaireDefinition {
  const label = "量表定义";
  const row = exactRecord(value, DEFINITION_KEYS, label);
  const provenanceRow = exactRecord(row.provenance, PROVENANCE_KEYS, `${label}.provenance`);
  const responseKind = enumValue(
    row, "response_kind",
    ["ordinal_sections", "binary_scored", "symptom_triplet"] as const, label);
  const definition: QuestionnaireDefinition = {
    schema_version: literalValue(
      row, "schema_version", QUESTIONNAIRE_DEFINITION_SCHEMA, label),
    questionnaire_id: stringValue(row, "questionnaire_id", label),
    title: stringValue(row, "title", label),
    short_name: stringValue(row, "short_name", label),
    respondent: enumValue(
      row, "respondent", ["observer", "patient_reported"] as const, label),
    status: literalValue(row, "status", "prototype", label),
    provenance: {
      provided_by: stringValue(provenanceRow, "provided_by", `${label}.provenance`),
      provided_via: stringValue(provenanceRow, "provided_via", `${label}.provenance`),
      provided_on: stringValue(provenanceRow, "provided_on", `${label}.provenance`),
      source_file: stringValue(provenanceRow, "source_file", `${label}.provenance`),
      source_sha256: sha256Value(provenanceRow, "source_sha256", `${label}.provenance`),
      final_confirmation: stringValue(
        provenanceRow, "final_confirmation", `${label}.provenance`),
    },
    instruction: stringValue(row, "instruction", label),
    response_kind: responseKind,
    value_field: row.value_field === null
      ? null : parseChoiceField(row.value_field, `${label}.value_field`),
    element_field: row.element_field === null
      ? null : parseElementField(row.element_field, `${label}.element_field`),
    present_field: row.present_field === null
      ? null : parseChoiceField(row.present_field, `${label}.present_field`),
    severity_field: row.severity_field === null
      ? null : parseChoiceField(row.severity_field, `${label}.severity_field`),
    frequency_field: row.frequency_field === null
      ? null : parseChoiceField(row.frequency_field, `${label}.frequency_field`),
    sections: row.sections === null
      ? null
      : arrayValue(row, "sections", label).map((entry, index) => {
        const sectionRow = exactRecord(entry, SECTION_KEYS, `${label}.sections[${index}]`);
        const items = arrayValue(sectionRow, "items", `${label}.sections[${index}]`)
          .map((item, itemIndex) =>
            parseItem(item, `${label}.sections[${index}].items[${itemIndex}]`));
        if (items.length < 1) {
          throw new Error(`${label}.sections[${index}].items 不能为空`);
        }
        return {
          section_id: stringValue(sectionRow, "section_id", `${label}.sections[${index}]`),
          title: stringValue(sectionRow, "title", `${label}.sections[${index}]`),
          items,
        };
      }),
    items: row.items === null
      ? null
      : arrayValue(row, "items", label).map((entry, index) =>
        parseItem(entry, `${label}.items[${index}]`)),
    scoring: row.scoring === null ? null : parseScoring(row.scoring, `${label}.scoring`),
    transcription_notes: arrayValue(row, "transcription_notes", label)
      .map((entry, index) => {
        if (typeof entry !== "string" || !entry) {
          throw new Error(`${label}.transcription_notes[${index}]必须是非空字符串`);
        }
        return entry;
      }),
  };

  if (responseKind === "ordinal_sections") {
    if (!definition.value_field || !definition.element_field || !definition.sections
        || definition.items || definition.present_field
        || definition.severity_field || definition.frequency_field
        || definition.scoring) {
      throw new Error(`${label}:ordinal_sections 的字段组合不合法`);
    }
  } else if (responseKind === "binary_scored") {
    if (!definition.value_field || !definition.items || !definition.scoring
        || definition.sections || definition.element_field
        || definition.present_field || definition.severity_field
        || definition.frequency_field) {
      throw new Error(`${label}:binary_scored 的字段组合不合法`);
    }
  } else {
    if (!definition.present_field || !definition.severity_field
        || !definition.frequency_field || !definition.items
        || definition.sections || definition.value_field
        || definition.element_field || definition.scoring) {
      throw new Error(`${label}:symptom_triplet 的字段组合不合法`);
    }
    const presentAllowed = [...definition.present_field.allowed].sort();
    if (presentAllowed.length !== 2
        || presentAllowed[0] !== "无" || presentAllowed[1] !== "有") {
      throw new Error(`${label}:symptom_triplet 的 present_field 只允许 有/无`);
    }
  }
  const itemKeys = questionnaireAllItems(definition).map((item) => item.item_key);
  if (new Set(itemKeys).size !== itemKeys.length || itemKeys.length < 1) {
    throw new Error(`${label}:item_key 必须非空且全表唯一`);
  }
  return definition;
}

export function parseQuestionnaireCatalog(value: unknown): QuestionnaireCatalogEntry[] {
  const label = "量表目录";
  const row = exactRecord(value, CATALOG_KEYS, label);
  literalValue(row, "schema_version", QUESTIONNAIRE_CATALOG_SCHEMA, label);
  const entries = arrayValue(row, "questionnaires", label).map((entry, index) => {
    const entryRow = exactRecord(entry, CATALOG_ENTRY_KEYS, `${label}[${index}]`);
    return {
      content_sha256: sha256Value(entryRow, "content_sha256", `${label}[${index}]`),
      definition: parseQuestionnaireDefinition(entryRow.definition),
    };
  });
  const ids = entries.map((entry) => entry.definition.questionnaire_id);
  if (new Set(ids).size !== ids.length) {
    throw new Error(`${label}:questionnaire_id 必须唯一`);
  }
  return entries;
}

function parseValueSlot(value: unknown, label: string): QuestionnaireItemValueSlot {
  const row = exactRecord(value, VALUE_SLOT_KEYS, label);
  return {
    item_key: stringValue(row, "item_key", label),
    field_key: stringValue(row, "field_key", label),
    ai_draft_value: nullableStringValue(row, "ai_draft_value", label),
    ai_draft_rationale: nullableStringValue(row, "ai_draft_rationale", label),
    final_value: nullableStringValue(row, "final_value", label),
    value_source: row.value_source === null
      ? null
      : enumValue(row, "value_source",
        ["human_direct", "ai_accepted", "ai_overridden"] as const, label),
  };
}

export function parseQuestionnaireRecord(
  value: unknown,
  expectation: QuestionnaireRecordExpectation = {},
): QuestionnaireRecord {
  const label = "量表记录";
  const row = exactRecord(value, RECORD_KEYS, label);
  const record: QuestionnaireRecord = {
    schema_version: literalValue(
      row, "schema_version", QUESTIONNAIRE_RECORD_SCHEMA, label),
    record_id: stringValue(row, "record_id", label),
    patient_id: stringValue(row, "patient_id", label),
    questionnaire_id: stringValue(row, "questionnaire_id", label),
    definition_sha256: sha256Value(row, "definition_sha256", label),
    phase_label: enumValue(row, "phase_label", QUESTIONNAIRE_PHASE_LABELS, label),
    status: enumValue(row, "status", ["draft", "locked"] as const, label),
    created_by: stringValue(row, "created_by", label),
    created_at: stringValue(row, "created_at", label),
    locked_by: nullableStringValue(row, "locked_by", label),
    locked_at: nullableStringValue(row, "locked_at", label),
    ai_draft_status: enumValue(row, "ai_draft_status", [
      "none", "not_applicable", "generated",
      "unavailable_no_data", "unavailable_not_authorized", "failed",
    ] as const, label),
    ai_draft_engine: nullableStringValue(row, "ai_draft_engine", label),
    ai_draft_at: nullableStringValue(row, "ai_draft_at", label),
    computed_total: nullableFiniteNumberValue(row, "computed_total", label),
    cutoff_met: nullableBooleanValue(row, "cutoff_met", label),
    computed_flag: nullableStringValue(row, "computed_flag", label),
    scoring_rule_id: nullableStringValue(row, "scoring_rule_id", label),
    note: nullableStringValue(row, "note", label),
    values: arrayValue(row, "values", label).map((entry, index) =>
      parseValueSlot(entry, `${label}.values[${index}]`)),
  };
  if (record.status === "draft" && (record.locked_by !== null || record.locked_at !== null)) {
    throw new Error(`${label}:草稿态不能带锁定人/锁定时间`);
  }
  if (record.status === "locked" && (record.locked_by === null || record.locked_at === null)) {
    throw new Error(`${label}:锁定态必须带锁定人与锁定时间`);
  }
  const slots = record.values.map((slot) => questionnaireSlotKey(slot.item_key, slot.field_key));
  if (new Set(slots).size !== slots.length) {
    throw new Error(`${label}:同一 (item_key, field_key) 出现多行`);
  }
  if (expectation.patientId !== undefined && record.patient_id !== expectation.patientId) {
    throw new Error(`${label}:回执不属于当前受试者`);
  }
  if (expectation.recordId !== undefined && record.record_id !== expectation.recordId) {
    throw new Error(`${label}:回执不属于当前记录`);
  }
  return record;
}

export function parseQuestionnaireRecordList(
  value: unknown,
  expectation: QuestionnaireRecordExpectation = {},
): QuestionnaireRecord[] {
  const label = "量表记录清单";
  const row = exactRecord(value, LIST_KEYS, label);
  literalValue(row, "schema_version", QUESTIONNAIRE_RECORD_LIST_SCHEMA, label);
  return arrayValue(row, "records", label)
    .map((entry) => parseQuestionnaireRecord(entry, expectation));
}

// ---------------- 纯逻辑(组件与测试共用) ----------------

export function questionnaireSlotKey(itemKey: string, fieldKey: string): string {
  return `${itemKey}\u0000${fieldKey}`;
}

export function finalValuesBySlot(
  record: QuestionnaireRecord,
): Map<string, string | null> {
  const map = new Map<string, string | null>();
  for (const slot of record.values) {
    map.set(questionnaireSlotKey(slot.item_key, slot.field_key), slot.final_value);
  }
  return map;
}

/** 「采纳全部 AI 建议」:对每个有草稿值的条目把终值设为草稿值。
 * 当前生效值(含未保存的本地点选)已等于草稿值的跳过——重复写会把
 * 人工自选同值的来源记成 AI 采纳,篡改一致性课题的原始口径。 */
export function adoptableAiDraftEntries(
  record: QuestionnaireRecord,
  pendingValues?: ReadonlyMap<string, string | null>,
): QuestionnaireValueWrite[] {
  const entries: QuestionnaireValueWrite[] = [];
  for (const slot of record.values) {
    if (slot.ai_draft_value === null) continue;
    const key = questionnaireSlotKey(slot.item_key, slot.field_key);
    const effective = pendingValues !== undefined && pendingValues.has(key)
      ? pendingValues.get(key) ?? null
      : slot.final_value;
    if (effective === slot.ai_draft_value) continue;
    entries.push({
      item_key: slot.item_key,
      field_key: slot.field_key,
      value: slot.ai_draft_value,
    });
  }
  return entries;
}

export function aiDraftStatusLine(status: QuestionnaireAiDraftStatus): string | null {
  switch (status) {
    case "none": return null;
    case "generated": return "AI 建议已生成，请逐项核对；人工点其他档位即覆盖。";
    case "not_applicable": return "此量表不提供 AI 初评。";
    case "unavailable_not_authorized": return "该受试者未授权云处理，AI 初评不可用。";
    case "unavailable_no_data": return "暂无训练数据可供参考。";
    case "failed": return "AI 初评没有成功——可能是服务器未接入云端 AI，或云端调用失败。请直接人工评定；反复出现请联系管理员。";
  }
}

/** 锁定前的缺项预览(只是提示;锁定能不能过,以服务器的答复为准)。 */
export function missingLockEntries(
  definition: QuestionnaireDefinition,
  record: QuestionnaireRecord,
): string[] {
  const finals = finalValuesBySlot(record);
  const value = (itemKey: string, fieldKey: string): string | null =>
    finals.get(questionnaireSlotKey(itemKey, fieldKey)) ?? null;
  const missing: string[] = [];
  if (definition.response_kind === "ordinal_sections") {
    for (const section of definition.sections ?? []) {
      for (const item of section.items) {
        if (value(item.item_key, "value") === null) {
          missing.push(`「${section.title}」第${item.no}题未评`);
        }
      }
      for (const element of definition.element_field?.elements ?? []) {
        if (value(`section:${section.section_id}`, `element:${element.element_key}`) === null) {
          missing.push(`「${section.title}」要素「${element.label}」未评`);
        }
      }
    }
  } else if (definition.response_kind === "binary_scored") {
    for (const item of definition.items ?? []) {
      if (value(item.item_key, "value") === null) {
        missing.push(`第${item.no}题未作答`);
      }
    }
  } else {
    for (const item of definition.items ?? []) {
      const present = value(item.item_key, "present");
      const severity = value(item.item_key, "severity");
      const frequency = value(item.item_key, "frequency");
      if (present === null) {
        missing.push(`第${item.no}题未记录「有/无」`);
        continue;
      }
      if (present === "有") {
        if (severity === null) missing.push(`第${item.no}题缺严重度`);
        if (frequency === null) missing.push(`第${item.no}题缺频率`);
      } else if (severity !== null || frequency !== null) {
        missing.push(`第${item.no}题记为「无」但仍带严重度/频率，请先清除`);
      }
    }
  }
  return missing;
}

export function questionnaireStatusLabel(record: QuestionnaireRecord): string {
  return record.status === "locked" ? "已锁定" : "草稿";
}

/** 锁定后的总分一行话;源表没定义计分(总分为空)时返回 null,绝不发明汇总分。 */
export function lockedScoreSummary(
  record: QuestionnaireRecord,
  definition: QuestionnaireDefinition | null,
): string | null {
  if (record.computed_total === null) return null;
  const scoring = definition?.scoring ?? null;
  const total = Number.isInteger(record.computed_total)
    ? String(record.computed_total)
    : record.computed_total.toFixed(1);
  const denominator = scoring ? ` / ${scoring.max_score}` : "";
  const flag = record.cutoff_met
    ? record.computed_flag ?? "达到界值"
    : scoring
      ? `未达界值（界值 ${scoring.cutoff_operator} ${scoring.cutoff_value} 分）`
      : "未达界值";
  return `总分 ${total}${denominator} · ${flag}`;
}

export interface QuestionnaireFailure {
  message: string;
  problems: string[];
}

const FAILURE_HINTS: Record<string, string> = {
  questionnaire_content_unavailable: "量表定义暂时读不到，请稍后重试或联系管理员。",
  questionnaire_unknown: "这份量表已不在系统注册表内，请刷新页面后重新选择。",
  questionnaire_definition_drifted: "量表定义在记录建立后更新过，这份草稿不能继续填写，请新建记录。",
  questionnaire_record_locked: "记录已锁定，不能再修改；改错请新建记录。",
  questionnaire_value_invalid: "有作答值没有通过校验，请核对后重试。",
  questionnaire_lock_incomplete: "还有条目没有完成，暂时不能锁定。",
  questionnaire_field_unknown: "这个作答位置不在量表定义内，请刷新页面后重试。",
  questionnaire_value_out_of_domain: "这个档位不在量表定义内，请刷新页面后重试。",
};

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

/** 把服务端错误翻译成一行可执行中文 + 可逐条展示的 problems。 */
export function questionnaireFailureText(error: unknown): QuestionnaireFailure {
  const errorRecord = asRecord(error);
  const detailData = asRecord(errorRecord?.detailData)
    ?? asRecord(errorRecord?.detail)
    ?? asRecord(error);
  const code = typeof detailData?.code === "string" ? detailData.code : null;
  const serverMessage = typeof detailData?.message === "string"
    ? detailData.message.trim()
    : "";
  const problems = Array.isArray(detailData?.problems)
    ? detailData.problems.filter((entry): entry is string => typeof entry === "string")
    : [];
  const readable = typeof errorRecord?.detail === "string" && errorRecord.detail.trim()
    ? errorRecord.detail.trim()
    : error instanceof Error && error.message.trim()
      ? error.message.trim()
      : "";
  const message = (code !== null ? FAILURE_HINTS[code] : undefined)
    ?? (serverMessage || readable || "操作没有成功，请重试。");
  return { message, problems };
}

export type QuestionnaireMutationOutcome =
  | { ok: true }
  | ({ ok: false } & QuestionnaireFailure);

/** 只在服务器真的接受之后才交付回执;失败一律不碰调用方的本地作答。
 * (自动保存靠它守住「先 await 成功才清本地」——失败时屏幕上刚点的档位必须还在。) */
export async function performQuestionnaireMutation<T>(
  action: () => Promise<T>,
  onSuccess: (value: T) => void,
): Promise<QuestionnaireMutationOutcome> {
  let value: T;
  try {
    value = await action();
  } catch (error) {
    return { ok: false, ...questionnaireFailureText(error) };
  }
  onSuccess(value);
  return { ok: true };
}
