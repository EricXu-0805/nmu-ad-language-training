// 只读研究数据面 /research/v1/* 的前端契约。
//
// 这一层不只是解析，它是**前端自己的一道去标识闸**：服务端已经在序列化前过了
// export_security.assert_deidentified_sheets，这里再独立校一遍。两道闸判据不同
// （服务端按列名与列语义，这里按"响应自己声明的列集合"），所以服务端哪天回归了，
// 控制台是拒绝渲染而不是把标识符显示出来。
//
// 行按列顺序投影成数组而不是保留对象：视图只能渲染 columns 里有的东西，
// "屏幕上看到的" 与 "导出的 CSV" 在结构上不可能不一致。

export const RESEARCH_DATASET_KEYS = ["subjects", "sessions", "turns"] as const;
export type ResearchDatasetKey = typeof RESEARCH_DATASET_KEYS[number];
export type ResearchDataClassification = "research" | "simulation";

export type ResearchCell = string | number | boolean | null;

export interface ResearchDatasetMeta {
  key: string;
  title: string;
  grain: string;
  columns: string[];
}

export interface ResearchMetaReady {
  configured: true;
  schemaVersion: string;
  pseudonymVersion: string;
  pseudonymKeyId: string;
  datasets: ResearchDatasetMeta[];
  defaultLimit: number;
  maxLimit: number;
  release: ResearchReleaseState;
  note: string;
}

export interface ResearchMetaUnavailable {
  configured: false;
  schemaVersion: string;
  reason: string;
  release: ResearchReleaseState;
  note: string;
}

export type ResearchMeta = ResearchMetaReady | ResearchMetaUnavailable;

/** 冻结发布纪元。研究分区的每一页都必须说自己出自哪一版。 */
export interface ResearchRelease {
  epochSeq: number;
  cohortRuleVersion: string;
  aggregatePayloadSha256: string;
}

export type ResearchReleaseState =
  | ({ bound: true; asOf: string; frozenAt: string; frozenSessionCount: number }
      & ResearchRelease)
  | { bound: false; code: string; reason: string };

export interface ResearchPage {
  schemaVersion: string;
  dataset: string;
  grain: string;
  /** 仿真分区恒 null：那里没有真人，不冻纪元，也不该看起来像冻过。 */
  release: ResearchRelease | null;
  pseudonymVersion: string;
  pseudonymKeyId: string;
  columns: string[];
  rows: ResearchCell[][];
  rowCount: number;
  hasMore: boolean;
  nextCursor: string | null;
}

export interface ResearchDictionaryRow {
  dataset: string;
  column: string;
  disclosure: string;
  dtype: string;
  unit: string | null;
  description: string;
  source: string | null;
  published: boolean;
}

// 与 app/export_security.py 的 _ISO_ABSOLUTE_TIME 同一口径：绝对时间一律不得出现。
const ISO_ABSOLUTE_TIME =
  /^\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d(?::[0-6]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;

const PSEUDONYM_PREFIX: Record<string, string> = {
  subject_code: "SUBJ-",
  session_code: "SESS-",
  audio_code: "AUDIO-",
  batch_code: "BATCH-",
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function text(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`研究数据响应字段 ${field} 缺失或不是非空字符串`);
  }
  return value;
}

function stringList(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item)) {
    throw new Error(`研究数据响应字段 ${field} 不是非空字符串数组`);
  }
  return value as string[];
}

function positiveInt(value: unknown, field: string): number {
  if (!Number.isInteger(value) || (value as number) < 1) {
    throw new Error(`研究数据响应字段 ${field} 不是正整数`);
  }
  return value as number;
}

export function parseResearchMeta(value: unknown): ResearchMeta {
  const root = record(value);
  if (!root) throw new Error("研究数据接口状态响应不是对象");
  const schemaVersion = text(root.schema_version, "schema_version");
  const note = text(root.note, "note");
  const deid = record(root.deidentification);
  if (!deid || typeof deid.configured !== "boolean") {
    throw new Error("研究数据接口状态响应缺少 deidentification.configured");
  }
  const release = parseResearchReleaseState(root.research_release);
  if (!deid.configured) {
    // 密钥未配时**必须**仍然可读，否则取数的人只拿到裸 503 无法自诊。
    return {
      configured: false,
      schemaVersion,
      reason: text(deid.reason, "deidentification.reason"),
      release,
      note,
    };
  }
  const page = record(root.page);
  if (!page) throw new Error("研究数据接口状态响应缺少 page");
  const datasetsRaw = root.datasets;
  if (!Array.isArray(datasetsRaw) || datasetsRaw.length === 0) {
    throw new Error("研究数据接口已就绪却没有声明任何数据集");
  }
  const datasets = datasetsRaw.map((entry, index) => {
    const row = record(entry);
    if (!row) throw new Error(`datasets[${index}] 不是对象`);
    return {
      key: text(row.key, `datasets[${index}].key`),
      title: text(row.title, `datasets[${index}].title`),
      grain: text(row.grain, `datasets[${index}].grain`),
      columns: stringList(row.columns, `datasets[${index}].columns`),
    };
  });
  return {
    configured: true,
    schemaVersion,
    pseudonymVersion: text(deid.pseudonym_version, "deidentification.pseudonym_version"),
    pseudonymKeyId: text(deid.pseudonym_key_id, "deidentification.pseudonym_key_id"),
    datasets,
    defaultLimit: positiveInt(page.default_limit, "page.default_limit"),
    maxLimit: positiveInt(page.max_limit, "page.max_limit"),
    release,
    note,
  };
}

function assertCell(column: string, cell: unknown, rowIndex: number): ResearchCell {
  if (cell === null || typeof cell === "number" || typeof cell === "boolean") {
    return cell as ResearchCell;
  }
  if (typeof cell !== "string") {
    throw new Error(`第 ${rowIndex + 1} 行的 ${column} 不是标量`);
  }
  if (ISO_ABSOLUTE_TIME.test(cell.trim())) {
    throw new Error(`第 ${rowIndex + 1} 行的 ${column} 是绝对时间，已拒绝显示`);
  }
  const prefix = PSEUDONYM_PREFIX[column];
  if (prefix !== undefined && !cell.startsWith(prefix)) {
    throw new Error(`第 ${rowIndex + 1} 行的 ${column} 不是 ${prefix} 假名，已拒绝显示`);
  }
  return cell;
}

function parseRelease(value: unknown, field: string): ResearchRelease {
  const row = record(value);
  if (!row) throw new Error(`${field} 不是对象`);
  if (!Number.isInteger(row.epoch_seq) || (row.epoch_seq as number) < 1) {
    throw new Error(`${field}.epoch_seq 不是正整数`);
  }
  const digest = text(row.aggregate_payload_sha256, `${field}.aggregate_payload_sha256`);
  if (!/^[0-9a-f]{64}$/.test(digest)) {
    throw new Error(`${field}.aggregate_payload_sha256 不是 sha256`);
  }
  return {
    epochSeq: row.epoch_seq as number,
    cohortRuleVersion: text(row.cohort_rule_version, `${field}.cohort_rule_version`),
    aggregatePayloadSha256: digest,
  };
}

export function parseResearchReleaseState(value: unknown): ResearchReleaseState {
  const row = record(value);
  if (!row || typeof row.bound !== "boolean") {
    throw new Error("研究数据接口状态缺少 research_release.bound");
  }
  if (!row.bound) {
    return {
      bound: false,
      code: text(row.code, "research_release.code"),
      reason: text(row.reason, "research_release.reason"),
    };
  }
  if (!Number.isInteger(row.frozen_session_count)) {
    throw new Error("research_release.frozen_session_count 不是整数");
  }
  return {
    bound: true,
    ...parseRelease(row, "research_release"),
    asOf: text(row.as_of, "research_release.as_of"),
    frozenAt: text(row.frozen_at, "research_release.frozen_at"),
    frozenSessionCount: row.frozen_session_count as number,
  };
}

export function parseResearchPage(
  value: unknown, expectedDataset: string,
  classification: ResearchDataClassification,
): ResearchPage {
  const root = record(value);
  if (!root) throw new Error("研究数据响应不是对象");
  const dataset = text(root.dataset, "dataset");
  if (dataset !== expectedDataset) {
    throw new Error(`响应的数据集是 ${dataset}，与请求的 ${expectedDataset} 不一致`);
  }
  const columns = stringList(root.columns, "columns");
  const rowsRaw = root.rows;
  if (!Array.isArray(rowsRaw)) throw new Error("研究数据响应的 rows 不是数组");
  if (!Number.isInteger(root.row_count) || root.row_count !== rowsRaw.length) {
    throw new Error("研究数据响应的 row_count 与实际行数对不上");
  }
  if (typeof root.has_more !== "boolean") {
    throw new Error("研究数据响应缺少 has_more");
  }
  const nextCursor = root.next_cursor;
  if (nextCursor !== null && (typeof nextCursor !== "string" || !nextCursor)) {
    throw new Error("研究数据响应的 next_cursor 既不是 null 也不是非空字符串");
  }
  if (root.has_more && nextCursor === null) {
    throw new Error("研究数据响应说还有下一页却没有给游标");
  }
  const declared = new Set(columns);
  const rows = rowsRaw.map((entry, rowIndex) => {
    const row = record(entry);
    if (!row) throw new Error(`第 ${rowIndex + 1} 行不是对象`);
    for (const key of Object.keys(row)) {
      // 闭集：多出来一个键就是服务端回归，拒绝渲染而不是把它显示出来。
      if (!declared.has(key)) {
        throw new Error(`第 ${rowIndex + 1} 行带了未声明的列 ${key}，已拒绝显示`);
      }
    }
    return columns.map((column) => assertCell(column, row[column] ?? null, rowIndex));
  });
  // 研究分区没有冻结纪元标识就不许渲染：那说明服务端把行面从纪元上解开了，
  // 而屏幕上那一页会看起来和绑着的时候一模一样。
  if (root.release === undefined) {
    throw new Error("研究数据响应没有说自己出自哪一个冻结纪元，已拒绝显示");
  }
  const release = root.release === null ? null : parseRelease(root.release, "release");
  if (classification === "research" && release === null) {
    throw new Error("真实研究分区返回了未冻结的数据，已拒绝显示");
  }
  if (classification === "simulation" && release !== null) {
    throw new Error("模拟演练分区不该带冻结纪元标识，已拒绝显示");
  }
  return {
    schemaVersion: text(root.schema_version, "schema_version"),
    dataset,
    grain: text(root.grain, "grain"),
    release,
    pseudonymVersion: text(root.pseudonym_version, "pseudonym_version"),
    pseudonymKeyId: text(root.pseudonym_key_id, "pseudonym_key_id"),
    columns,
    rows,
    rowCount: rowsRaw.length,
    hasMore: root.has_more,
    nextCursor: nextCursor as string | null,
  };
}

export function parseResearchDictionary(value: unknown): ResearchDictionaryRow[] {
  const root = record(value);
  if (!root) throw new Error("数据字典响应不是对象");
  const rowsRaw = root.columns;
  if (!Array.isArray(rowsRaw) || rowsRaw.length === 0) {
    throw new Error("数据字典响应没有任何列");
  }
  return rowsRaw.map((entry, index) => {
    const row = record(entry);
    if (!row) throw new Error(`数据字典第 ${index + 1} 行不是对象`);
    if (typeof row.published !== "boolean") {
      throw new Error(`数据字典第 ${index + 1} 行缺少 published`);
    }
    return {
      dataset: text(row.dataset, `columns[${index}].dataset`),
      column: text(row.column, `columns[${index}].column`),
      disclosure: text(row.disclosure, `columns[${index}].disclosure`),
      dtype: text(row.dtype, `columns[${index}].dtype`),
      unit: typeof row.unit === "string" && row.unit ? row.unit : null,
      description: text(row.description, `columns[${index}].description`),
      source: typeof row.source === "string" && row.source ? row.source : null,
      published: row.published,
    };
  });
}

/** 屏上那一页与导出的那一份 CSV 用同一个 query，只差一个 `.csv` 后缀。 */
export function researchDatasetPath(request: {
  dataset: string;
  classification: ResearchDataClassification;
  cursor?: string | null;
  limit?: number | null;
  csv?: boolean;
}): string {
  const params = new URLSearchParams();
  params.set("data_classification", request.classification);
  if (request.cursor) params.set("cursor", request.cursor);
  if (request.limit != null) params.set("limit", String(request.limit));
  const suffix = request.csv ? ".csv" : "";
  return `/research/v1/${encodeURIComponent(request.dataset)}${suffix}?${params.toString()}`;
}

export function researchCsvFilename(
  dataset: string, classification: ResearchDataClassification,
  release: ResearchRelease | null,
): string {
  // 与服务端 Content-Disposition 同一口径。文件存到磁盘之后信封就不在了，
  // 而"这份数据是哪一版"正是半年后写论文时最需要留住的。
  const stamp = release === null
    ? ""
    : `-epoch${String(release.epochSeq).padStart(3, "0")}`;
  return `nmu-${dataset}-${classification}${stamp}.csv`;
}
