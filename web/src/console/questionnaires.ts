// 量表电子记录（原型道）的前端契约:exactKeys 解析器 + 纯逻辑。
// 题词/锚点全部来自认证接口下发的定义包;本模块与两张屏永不硬编码任何题词,
// 测试一律用微型假定义。多一键或少一键的响应整包拒收,绝不静默放行。

export const QUESTIONNAIRE_CATALOG_SCHEMA = "questionnaire-catalog.v1";
/** 同期别重测的一句话说明。只有一次施测时返回 null——别在界面上写「第 1 次」。
 *
 * 没有这句话，控制台上两条同为「前测」的记录长得一模一样，研究者只能靠创建时间猜
 * 哪条作数；而作废那条同样带总分，正是导出里最容易被统计脚本取错的那一行。
 */
export function questionnaireRetakeNote(record: {
  phase_ordinal: number;
  superseded_by_ordinal: number | null;
}): string | null {
  if (record.superseded_by_ordinal !== null) {
    return `第 ${record.phase_ordinal} 次施测 · 已被第 ${record.superseded_by_ordinal} 次取代，不作数`;
  }
  if (record.phase_ordinal > 1) {
    return `第 ${record.phase_ordinal} 次施测 · 当前作数的一条`;
  }
  return null;
}

export const QUESTIONNAIRE_RECORD_SCHEMA = "questionnaire-record.v1";
export const QUESTIONNAIRE_RECORD_LIST_SCHEMA = "questionnaire-record-list.v1";
export const QUESTIONNAIRE_DEFINITION_SCHEMA = "questionnaire-definition.v1";

// 顶部常驻横幅原句(有测试钉着,改字先改测试)。
export const QUESTIONNAIRE_TRIAL_NOTICE =
  "量表电子记录（试用），终确认前不作为正式研究结局";

export const QUESTIONNAIRE_PHASE_LABELS = ["前测", "后测", "随访", "其他"] as const;
export type QuestionnairePhaseLabel = typeof QUESTIONNAIRE_PHASE_LABELS[number];

export type QuestionnaireResponseKind =
  | "ordinal_sections" | "binary_scored" | "symptom_triplet" | "examiner_scored";
export type QuestionnaireRespondent =
  | "observer" | "patient_reported" | "examiner_administered";
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

export interface QuestionnaireBinarySumScoring {
  kind: "binary_sum";
  scoring_rule_id: string;
  max_score: number;
  cutoff_value: number;
  cutoff_operator: ">=";
  cutoff_label: string;
  rule_verbatim: string;
}

export interface QuestionnaireCutoff {
  operator: "<=" | ">=";
  value: number;
  label: string;
}

/** 界值按某个 choice 条目的选项分组;组值 null = 源表没给该组界值,锁定时不判定。 */
export interface QuestionnaireStratifiedCutoff {
  by_item: string;
  groups: Record<string, QuestionnaireCutoff | null>;
  unjudged_label: string;
}

export interface QuestionnaireExaminerSumScoring {
  kind: "examiner_sum";
  scoring_rule_id: string;
  max_score: number | null;
  cutoff: QuestionnaireCutoff | null;
  stratified_cutoff: QuestionnaireStratifiedCutoff | null;
  rule_verbatim: string;
}

export type QuestionnaireScoring =
  | QuestionnaireBinarySumScoring | QuestionnaireExaminerSumScoring;

/** 分档表一行:总数落在 [min, max] 记 score 分;max=null 表示「及以上」。 */
export interface QuestionnaireScoreBin {
  min: number;
  max: number | null;
  score: number;
}

/** 检查者录入方式:score=直接记分;count=记总数(可按 bins 换算);choice=闭集选项。 */
export interface QuestionnaireExaminerEntry {
  kind: "score" | "count" | "choice";
  max: number | null;
  scored: boolean;
  bins: QuestionnaireScoreBin[] | null;
  choice: QuestionnaireChoiceField | null;
}

export interface QuestionnaireExaminerItem {
  item_key: string;
  no: number;
  domain_key: string;
  name: string;
  text: string;
  entry: QuestionnaireExaminerEntry;
}

export interface QuestionnaireExaminerDomain {
  domain_key: string;
  title: string;
  max_score: number | null;
}

export interface QuestionnaireExaminerPanel {
  domains: QuestionnaireExaminerDomain[];
  items: QuestionnaireExaminerItem[];
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
  respondent: QuestionnaireRespondent;
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
  examiner_panel: QuestionnaireExaminerPanel | null;
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
  /** 同一期别第几次施测（1 起）。同槽位再建一条 = 序号 +1。 */
  phase_ordinal: number;
  /** 被第几次取代；null = 这条就是当前作数的那条。 */
  superseded_by_ordinal: number | null;
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
  "sections", "items", "examiner_panel", "scoring", "transcription_notes",
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
const BINARY_SCORING_KEYS = [
  "kind", "scoring_rule_id", "max_score", "cutoff_value",
  "cutoff_operator", "cutoff_label", "rule_verbatim",
] as const;
const EXAMINER_SCORING_KEYS = [
  "kind", "scoring_rule_id", "max_score", "cutoff", "stratified_cutoff", "rule_verbatim",
] as const;
const CUTOFF_KEYS = ["operator", "value", "label"] as const;
const STRATIFIED_CUTOFF_KEYS = ["by_item", "groups", "unjudged_label"] as const;
const EXAMINER_PANEL_KEYS = ["domains", "items"] as const;
const EXAMINER_DOMAIN_KEYS = ["domain_key", "title", "max_score"] as const;
const EXAMINER_ITEM_KEYS = ["item_key", "no", "domain_key", "name", "text", "entry"] as const;
const EXAMINER_ENTRY_KEYS = ["kind", "max", "scored", "bins", "choice"] as const;
const SCORE_BIN_KEYS = ["min", "max", "score"] as const;
const RECORD_KEYS = [
  "schema_version", "record_id", "patient_id", "questionnaire_id",
  "definition_sha256", "phase_label", "status", "created_by", "created_at",
  "locked_by", "locked_at", "ai_draft_status", "ai_draft_engine", "ai_draft_at",
  "computed_total", "cutoff_met", "computed_flag", "scoring_rule_id",
  "note", "values", "phase_ordinal", "superseded_by_ordinal",
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

function nullableIntegerValue(
  row: UnknownRecord, key: string, label: string, minimum: number,
): number | null {
  if (row[key] === null) return null;
  return integerValue(row, key, label, minimum);
}

function booleanValue(row: UnknownRecord, key: string, label: string): boolean {
  const value = row[key];
  if (typeof value !== "boolean") throw new Error(`${label}.${key}必须是布尔值`);
  return value;
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

function parseCutoff(value: unknown, label: string): QuestionnaireCutoff {
  const row = exactRecord(value, CUTOFF_KEYS, label);
  return {
    operator: enumValue(row, "operator", ["<=", ">="] as const, label),
    value: integerValue(row, "value", label, 0),
    label: stringValue(row, "label", label),
  };
}

function parseStratifiedCutoff(
  value: unknown, label: string,
): QuestionnaireStratifiedCutoff {
  const row = exactRecord(value, STRATIFIED_CUTOFF_KEYS, label);
  const groupsRaw = row.groups;
  if (groupsRaw === null || typeof groupsRaw !== "object" || Array.isArray(groupsRaw)) {
    throw new Error(`${label}.groups 必须是对象`);
  }
  const groups: Record<string, QuestionnaireCutoff | null> = {};
  const keys = Object.keys(groupsRaw as UnknownRecord);
  if (keys.length < 1) throw new Error(`${label}.groups 不能为空`);
  for (const key of keys) {
    const rule = (groupsRaw as UnknownRecord)[key];
    groups[key] = rule === null ? null : parseCutoff(rule, `${label}.groups.${key}`);
  }
  return {
    by_item: stringValue(row, "by_item", label),
    groups,
    unjudged_label: stringValue(row, "unjudged_label", label),
  };
}

function parseScoring(value: unknown, label: string): QuestionnaireScoring {
  const kind = (value as UnknownRecord | null)?.kind;
  if (kind === "examiner_sum") {
    const row = exactRecord(value, EXAMINER_SCORING_KEYS, label);
    const scoring: QuestionnaireExaminerSumScoring = {
      kind: "examiner_sum",
      scoring_rule_id: stringValue(row, "scoring_rule_id", label),
      max_score: nullableIntegerValue(row, "max_score", label, 1),
      cutoff: row.cutoff === null ? null : parseCutoff(row.cutoff, `${label}.cutoff`),
      stratified_cutoff: row.stratified_cutoff === null
        ? null
        : parseStratifiedCutoff(row.stratified_cutoff, `${label}.stratified_cutoff`),
      rule_verbatim: stringValue(row, "rule_verbatim", label),
    };
    if (scoring.cutoff !== null && scoring.stratified_cutoff !== null) {
      throw new Error(`${label}:cutoff 与 stratified_cutoff 只能二选一`);
    }
    return scoring;
  }
  const row = exactRecord(value, BINARY_SCORING_KEYS, label);
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

function parseScoreBins(value: unknown, label: string): QuestionnaireScoreBin[] {
  if (!Array.isArray(value)) throw new Error(`${label}必须是数组`);
  const bins = value.map((entry, index) => {
    const row = exactRecord(entry, SCORE_BIN_KEYS, `${label}[${index}]`);
    return {
      min: integerValue(row, "min", `${label}[${index}]`, 0),
      max: nullableIntegerValue(row, "max", `${label}[${index}]`, 0),
      score: integerValue(row, "score", `${label}[${index}]`, 0),
    };
  });
  // 与服务端同一条不变量:降序、最高档开放、连续不重叠、从 0 起——否则换算会静默错分。
  const ordered = [...bins].sort((a, b) => b.min - a.min);
  if (ordered.length < 2 || ordered[0].max !== null || ordered[ordered.length - 1].min !== 0) {
    throw new Error(`${label}:分档表必须至少两档、最高档开放、从 0 起`);
  }
  for (let index = 1; index < ordered.length; index += 1) {
    const upper = ordered[index - 1];
    const lower = ordered[index];
    if (lower.max === null || lower.max < lower.min || lower.max !== upper.min - 1) {
      throw new Error(`${label}:分档表必须连续且不重叠`);
    }
  }
  return bins;
}

function parseExaminerEntry(value: unknown, label: string): QuestionnaireExaminerEntry {
  const row = exactRecord(value, EXAMINER_ENTRY_KEYS, label);
  const entry: QuestionnaireExaminerEntry = {
    kind: enumValue(row, "kind", ["score", "count", "choice"] as const, label),
    max: nullableIntegerValue(row, "max", label, 1),
    scored: booleanValue(row, "scored", label),
    bins: row.bins === null ? null : parseScoreBins(row.bins, `${label}.bins`),
    choice: row.choice === null ? null : parseChoiceField(row.choice, `${label}.choice`),
  };
  if (entry.kind === "score") {
    if (entry.max === null || !entry.scored || entry.bins !== null || entry.choice !== null) {
      throw new Error(`${label}:score 条目需要 max,scored 必须为 true,不接受 bins/choice`);
    }
  } else if (entry.kind === "count") {
    if (entry.max === null || entry.choice !== null || (entry.bins !== null && !entry.scored)) {
      throw new Error(`${label}:count 条目需要 max,不接受 choice;不计分的不接受 bins`);
    }
  } else if (entry.choice === null || entry.max !== null || entry.scored || entry.bins !== null) {
    throw new Error(`${label}:choice 条目需要 choice,scored 必须为 false,不接受 max/bins`);
  }
  return entry;
}

function parseExaminerPanel(value: unknown, label: string): QuestionnaireExaminerPanel {
  const row = exactRecord(value, EXAMINER_PANEL_KEYS, label);
  const domains = arrayValue(row, "domains", label).map((entry, index) => {
    const domainRow = exactRecord(entry, EXAMINER_DOMAIN_KEYS, `${label}.domains[${index}]`);
    return {
      domain_key: stringValue(domainRow, "domain_key", `${label}.domains[${index}]`),
      title: stringValue(domainRow, "title", `${label}.domains[${index}]`),
      max_score: nullableIntegerValue(domainRow, "max_score", `${label}.domains[${index}]`, 1),
    };
  });
  const domainKeys = domains.map((domain) => domain.domain_key);
  if (domainKeys.length < 1 || new Set(domainKeys).size !== domainKeys.length) {
    throw new Error(`${label}.domains 的 domain_key 必须非空且唯一`);
  }
  const items = arrayValue(row, "items", label).map((entry, index) => {
    const itemRow = exactRecord(entry, EXAMINER_ITEM_KEYS, `${label}.items[${index}]`);
    const item: QuestionnaireExaminerItem = {
      item_key: stringValue(itemRow, "item_key", `${label}.items[${index}]`),
      no: integerValue(itemRow, "no", `${label}.items[${index}]`, 1),
      domain_key: stringValue(itemRow, "domain_key", `${label}.items[${index}]`),
      name: stringValue(itemRow, "name", `${label}.items[${index}]`),
      text: stringValue(itemRow, "text", `${label}.items[${index}]`),
      entry: parseExaminerEntry(itemRow.entry, `${label}.items[${index}].entry`),
    };
    if (!domainKeys.includes(item.domain_key)) {
      throw new Error(`${label}.items[${index}] 的 domain_key 不在 domains 内`);
    }
    return item;
  });
  if (items.length < 1) throw new Error(`${label}.items 不能为空`);
  return { domains, items };
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
    ["ordinal_sections", "binary_scored", "symptom_triplet", "examiner_scored"] as const,
    label);
  const definition: QuestionnaireDefinition = {
    schema_version: literalValue(
      row, "schema_version", QUESTIONNAIRE_DEFINITION_SCHEMA, label),
    questionnaire_id: stringValue(row, "questionnaire_id", label),
    title: stringValue(row, "title", label),
    short_name: stringValue(row, "short_name", label),
    respondent: enumValue(
      row, "respondent",
      ["observer", "patient_reported", "examiner_administered"] as const, label),
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
    examiner_panel: row.examiner_panel === null
      ? null : parseExaminerPanel(row.examiner_panel, `${label}.examiner_panel`),
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
        || definition.examiner_panel || definition.scoring) {
      throw new Error(`${label}:ordinal_sections 的字段组合不合法`);
    }
  } else if (responseKind === "binary_scored") {
    if (!definition.value_field || !definition.items || !definition.scoring
        || definition.scoring.kind !== "binary_sum"
        || definition.sections || definition.element_field
        || definition.present_field || definition.severity_field
        || definition.frequency_field || definition.examiner_panel) {
      throw new Error(`${label}:binary_scored 的字段组合不合法`);
    }
  } else if (responseKind === "symptom_triplet") {
    if (!definition.present_field || !definition.severity_field
        || !definition.frequency_field || !definition.items
        || definition.sections || definition.value_field
        || definition.element_field || definition.examiner_panel || definition.scoring) {
      throw new Error(`${label}:symptom_triplet 的字段组合不合法`);
    }
    const presentAllowed = [...definition.present_field.allowed].sort();
    if (presentAllowed.length !== 2
        || presentAllowed[0] !== "无" || presentAllowed[1] !== "有") {
      throw new Error(`${label}:symptom_triplet 的 present_field 只允许 有/无`);
    }
  } else {
    if (!definition.examiner_panel || !definition.scoring
        || definition.scoring.kind !== "examiner_sum"
        || definition.value_field || definition.element_field
        || definition.present_field || definition.severity_field
        || definition.frequency_field || definition.sections || definition.items) {
      throw new Error(`${label}:examiner_scored 的字段组合不合法`);
    }
    const stratified = definition.scoring.stratified_cutoff;
    if (stratified !== null) {
      const byItem = definition.examiner_panel.items
        .find((item) => item.item_key === stratified.by_item);
      const allowed = byItem?.entry.choice?.allowed;
      if (!allowed || [...allowed].sort().join("\u0000")
          !== Object.keys(stratified.groups).sort().join("\u0000")) {
        throw new Error(`${label}:stratified_cutoff 必须指向 choice 条目且组键与 allowed 一致`);
      }
    }
  }
  const itemKeys = [
    ...questionnaireAllItems(definition).map((item) => item.item_key),
    ...(definition.examiner_panel?.items ?? []).map((item) => item.item_key),
  ];
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
    phase_ordinal: integerValue(row, "phase_ordinal", label, 1),
    superseded_by_ordinal: nullableIntegerValue(
      row, "superseded_by_ordinal", label, 1),
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
  if (record.superseded_by_ordinal !== null
      && record.superseded_by_ordinal <= record.phase_ordinal) {
    throw new Error(`${label}:取代它的那次序号必须比自己大`);
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
  } else if (definition.response_kind === "examiner_scored") {
    const panel = definition.examiner_panel;
    for (const item of panel?.items ?? []) {
      if (value(item.item_key, "value") === null) {
        missing.push(`「${examinerDomainOf(panel, item).title}」第${item.no}题未评`);
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

// ---------------- examiner_scored 的纯换算(屏上小计与锁定摘要共用) ----------------

const CANONICAL_INT = /^(0|[1-9][0-9]*)$/;

/** 计分框/计数框的录入值 → 整数;非规范整数串或越界返回 null。 */
export function examinerEntryNumber(
  entry: QuestionnaireExaminerEntry, value: string | null,
): number | null {
  if (value === null || entry.max === null || !CANONICAL_INT.test(value)) return null;
  const number = Number(value);
  return number <= entry.max ? number : null;
}

export function examinerBinScore(bins: QuestionnaireScoreBin[], count: number): number {
  for (const row of bins) {
    if (count >= row.min && (row.max === null || count <= row.max)) return row.score;
  }
  throw new Error("分档表未覆盖该总数");
}

/** 条目对总分的贡献;不计分条目恒为 0,未录入/不合法返回 null。 */
export function examinerEntryPoints(
  entry: QuestionnaireExaminerEntry, value: string | null,
): number | null {
  if (!entry.scored) return 0;
  const number = examinerEntryNumber(entry, value);
  if (number === null) return null;
  return entry.bins ? examinerBinScore(entry.bins, number) : number;
}

export function examinerEntryMaxPoints(entry: QuestionnaireExaminerEntry): number {
  if (!entry.scored) return 0;
  if (entry.bins) return Math.max(...entry.bins.map((row) => row.score));
  return entry.max ?? 0;
}

export function examinerDomainOf(
  panel: QuestionnaireExaminerPanel | null, item: QuestionnaireExaminerItem,
): QuestionnaireExaminerDomain {
  const domain = panel?.domains.find((entry) => entry.domain_key === item.domain_key);
  return domain ?? { domain_key: item.domain_key, title: item.domain_key, max_score: null };
}

export interface QuestionnaireExaminerDomainTotal {
  domain: QuestionnaireExaminerDomain;
  points: number;
  /** 该域里还没有合法录入值的计分条目数。 */
  missing: number;
}

export function examinerDomainTotals(
  panel: QuestionnaireExaminerPanel,
  effective: (itemKey: string) => string | null,
): QuestionnaireExaminerDomainTotal[] {
  return panel.domains.map((domain) => {
    let points = 0;
    let missing = 0;
    for (const item of panel.items) {
      if (item.domain_key !== domain.domain_key || !item.entry.scored) continue;
      const contribution = examinerEntryPoints(item.entry, effective(item.item_key));
      if (contribution === null) missing += 1;
      else points += contribution;
    }
    return { domain, points, missing };
  });
}

/** 界值未命中的一句话:≥ 型说「未达」,≤ 型说「高于」——方向说反了临床会读成相反结论。 */
function cutoffMissText(cutoff: QuestionnaireCutoff): string {
  return cutoff.operator === "<="
    ? `高于界值（界值 ≤ ${cutoff.value} 分）`
    : `未达界值（界值 ≥ ${cutoff.value} 分）`;
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
  if (scoring?.kind === "examiner_sum") {
    const finals = finalValuesBySlot(record);
    const effective = (itemKey: string) =>
      finals.get(questionnaireSlotKey(itemKey, "value")) ?? null;
    const parts = [`总分 ${total}${scoring.max_score !== null ? ` / ${scoring.max_score}` : ""}`];
    const panel = definition?.examiner_panel ?? null;
    if (panel && panel.domains.length > 1) {
      for (const entry of examinerDomainTotals(panel, effective)) {
        parts.push(`${entry.domain.title} ${entry.points}${
          entry.domain.max_score !== null ? `/${entry.domain.max_score}` : ""}`);
      }
    }
    if (record.cutoff_met === true) {
      parts.push(record.computed_flag ?? "达到界值");
    } else if (record.cutoff_met === false) {
      const rule = scoring.cutoff
        ?? (scoring.stratified_cutoff
          ? scoring.stratified_cutoff.groups[effective(scoring.stratified_cutoff.by_item) ?? ""]
            ?? null
          : null);
      parts.push(rule ? cutoffMissText(rule) : "未达界值");
    } else if (record.computed_flag !== null) {
      parts.push(record.computed_flag);
    }
    return parts.join(" · ");
  }
  const denominator = scoring ? ` / ${scoring.max_score}` : "";
  if (record.cutoff_met === null) {
    // 没有界值判定的记录(例如定义包读不到的 ACE-III):只报总分,不编造「未达界值」。
    return `总分 ${total}${denominator}${
      record.computed_flag !== null ? ` · ${record.computed_flag}` : ""}`;
  }
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
