import assert from "node:assert/strict";
import test from "node:test";
import {
  parseResearchDictionary,
  parseResearchMeta,
  parseResearchPage,
  researchCsvFilename,
  researchDatasetPath,
} from "./researchDataContract.ts";

function metaPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "research-read.v1",
    deidentification: {
      configured: true, pseudonym_version: "v1", pseudonym_key_id: "nmu-2026-01",
    },
    datasets: [
      { key: "turns", title: "逐环节", grain: "turn", columns: ["subject_code", "turn_seq"] },
    ],
    page: { default_limit: 200, max_limit: 1000, style: "keyset" },
    note: "轮换去标识密钥会让所有假名改变。",
    ...overrides,
  };
}

function pagePayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "research-read.v1",
    dataset: "turns",
    grain: "turn",
    pseudonym_version: "v1",
    pseudonym_key_id: "nmu-2026-01",
    columns: ["subject_code", "session_code", "item_id", "turn_seq", "ai_score"],
    rows: [{
      subject_code: "SUBJ-v1-nmu-2026-01-abcdef0123456789abcd",
      session_code: "SESS-v1-nmu-2026-01-abcdef0123456789abcd",
      item_id: "SE_胡萝卜", turn_seq: 1, ai_score: 2,
    }],
    row_count: 1,
    has_more: false,
    next_cursor: null,
    ...overrides,
  };
}

test("parseResearchMeta：就绪时给出假名密钥编号与分页上限", () => {
  const meta = parseResearchMeta(metaPayload());
  assert.equal(meta.configured, true);
  if (!meta.configured) return;
  assert.equal(meta.pseudonymKeyId, "nmu-2026-01");
  assert.equal(meta.maxLimit, 1000);
  assert.deepEqual(meta.datasets[0]!.columns, ["subject_code", "turn_seq"]);
});

test("parseResearchMeta：密钥没配也必须能读，并带上可自诊的原因", () => {
  const meta = parseResearchMeta(metaPayload({
    deidentification: { configured: false, reason: "去标识密钥未配置" },
    datasets: [],
  }));
  assert.equal(meta.configured, false);
  if (meta.configured) return;
  assert.match(meta.reason, /去标识密钥/);
});

test("parseResearchMeta：就绪却零数据集是坏响应，不是空结果", () => {
  assert.throws(() => parseResearchMeta(metaPayload({ datasets: [] })), /没有声明任何数据集/);
});

test("parseResearchPage：行按列顺序投影成数组", () => {
  const page = parseResearchPage(pagePayload(), "turns");
  assert.deepEqual(page.rows[0], [
    "SUBJ-v1-nmu-2026-01-abcdef0123456789abcd",
    "SESS-v1-nmu-2026-01-abcdef0123456789abcd",
    "SE_胡萝卜", 1, 2,
  ]);
  assert.equal(page.hasMore, false);
});

test("parseResearchPage：多出一个未声明的列就拒绝渲染整页", () => {
  const rows = [{ ...(pagePayload().rows as Record<string, unknown>[])[0], patient_id: "P-001" }];
  assert.throws(
    () => parseResearchPage(pagePayload({ rows }), "turns"),
    /未声明的列 patient_id/,
  );
});

test("parseResearchPage：任何绝对时间都拒绝显示", () => {
  for (const stamp of ["2026-08-14T07:58:00Z", "2026-08-14 07:58", "2026-08-14"]) {
    const rows = [{ ...(pagePayload().rows as Record<string, unknown>[])[0], item_id: stamp }];
    assert.throws(
      () => parseResearchPage(pagePayload({ rows }), "turns"),
      /绝对时间/,
      `应拒绝 ${stamp}`,
    );
  }
});

test("parseResearchPage：假名列必须真的是假名，明文标识符拒绝显示", () => {
  const rows = [{ ...(pagePayload().rows as Record<string, unknown>[])[0], subject_code: "P-001" }];
  assert.throws(
    () => parseResearchPage(pagePayload({ rows }), "turns"),
    /不是 SUBJ- 假名/,
  );
});

test("parseResearchPage：墓碑行（除编号外全 null）是合法的，不能被当成坏响应", () => {
  const rows = [{
    subject_code: "SUBJ-v1-nmu-2026-01-abcdef0123456789abcd",
    session_code: "SESS-v1-nmu-2026-01-abcdef0123456789abcd",
    item_id: null, turn_seq: null, ai_score: null,
  }];
  const page = parseResearchPage(pagePayload({ rows }), "turns");
  assert.deepEqual(page.rows[0]!.slice(2), [null, null, null]);
});

test("parseResearchPage：数据集对不上、行数对不上、说有下页却没游标，都拒绝", () => {
  assert.throws(() => parseResearchPage(pagePayload(), "sessions"), /与请求的 sessions 不一致/);
  assert.throws(() => parseResearchPage(pagePayload({ row_count: 5 }), "turns"), /row_count/);
  assert.throws(
    () => parseResearchPage(pagePayload({ has_more: true, next_cursor: null }), "turns"),
    /没有给游标/,
  );
});

test("researchDatasetPath：CSV 与屏上那一页用的是同一个 query，只差后缀", () => {
  const common = { dataset: "turns", classification: "research" as const, cursor: "c1", limit: 50 };
  const json = researchDatasetPath(common);
  const csv = researchDatasetPath({ ...common, csv: true });
  assert.equal(json, "/research/v1/turns?data_classification=research&cursor=c1&limit=50");
  assert.equal(csv.replace(".csv", ""), json);
});

test("researchDatasetPath：没有游标和 limit 时不发空参数", () => {
  assert.equal(
    researchDatasetPath({ dataset: "subjects", classification: "simulation" }),
    "/research/v1/subjects?data_classification=simulation",
  );
});

test("researchCsvFilename：文件名带分区，避免真实与模拟两份存混", () => {
  assert.equal(researchCsvFilename("turns", "simulation"), "nmu-turns-simulation.csv");
});

test("parseResearchDictionary：被排除的列也必须出现在字典里", () => {
  const rows = parseResearchDictionary({
    schema_version: "research-read.v1",
    columns: [
      { dataset: "turns", column: "asr_text", disclosure: "forbidden", dtype: "str",
        unit: null, description: "转写原文永不出", source: null, published: false },
    ],
  });
  assert.equal(rows[0]!.published, false);
  assert.equal(rows[0]!.disclosure, "forbidden");
});
