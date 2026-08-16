import assert from "node:assert/strict";
import test from "node:test";
import {
  parseResearchDictionary,
  parseResearchMeta,
  parseResearchPage,
  parseResearchReleaseState,
  researchCsvFilename,
  researchDatasetPath,
} from "./researchDataContract.ts";

const DIGEST = "a".repeat(64);

const RELEASE = {
  epoch_seq: 3,
  cohort_rule_version: "quality-release-cohort.v1",
  aggregate_payload_sha256: DIGEST,
};

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
    research_release: {
      bound: true, ...RELEASE, as_of: "2026-08-01T00:00:00Z",
      frozen_at: "2026-08-02T00:00:00Z", frozen_session_count: 12,
    },
    note: "轮换去标识密钥会让所有假名改变。",
    ...overrides,
  };
}

function pagePayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "research-read.v1",
    dataset: "turns",
    grain: "turn",
    release: RELEASE,
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
  const page = parseResearchPage(pagePayload(), "turns", "research");
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
    () => parseResearchPage(pagePayload({ rows }), "turns", "research"),
    /未声明的列 patient_id/,
  );
});

test("parseResearchPage：任何绝对时间都拒绝显示", () => {
  for (const stamp of ["2026-08-14T07:58:00Z", "2026-08-14 07:58", "2026-08-14"]) {
    const rows = [{ ...(pagePayload().rows as Record<string, unknown>[])[0], item_id: stamp }];
    assert.throws(
      () => parseResearchPage(pagePayload({ rows }), "turns", "research"),
      /绝对时间/,
      `应拒绝 ${stamp}`,
    );
  }
});

test("parseResearchPage：假名列必须真的是假名，明文标识符拒绝显示", () => {
  const rows = [{ ...(pagePayload().rows as Record<string, unknown>[])[0], subject_code: "P-001" }];
  assert.throws(
    () => parseResearchPage(pagePayload({ rows }), "turns", "research"),
    /不是 SUBJ- 假名/,
  );
});

test("parseResearchPage：墓碑行（除编号外全 null）是合法的，不能被当成坏响应", () => {
  const rows = [{
    subject_code: "SUBJ-v1-nmu-2026-01-abcdef0123456789abcd",
    session_code: "SESS-v1-nmu-2026-01-abcdef0123456789abcd",
    item_id: null, turn_seq: null, ai_score: null,
  }];
  const page = parseResearchPage(pagePayload({ rows }), "turns", "research");
  assert.deepEqual(page.rows[0]!.slice(2), [null, null, null]);
});

test("parseResearchPage：数据集对不上、行数对不上、说有下页却没游标，都拒绝", () => {
  assert.throws(() => parseResearchPage(pagePayload(), "sessions", "research"), /与请求的 sessions 不一致/);
  assert.throws(() => parseResearchPage(pagePayload({ row_count: 5 }), "turns", "research"), /row_count/);
  assert.throws(
    () => parseResearchPage(pagePayload({ has_more: true, next_cursor: null }), "turns", "research"),
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

test("researchCsvFilename：文件名带分区与纪元号，避免两份存混也避免两版存混", () => {
  assert.equal(researchCsvFilename("turns", "simulation", null), "nmu-turns-simulation.csv");
  assert.equal(
    researchCsvFilename("turns", "research", { epochSeq: 3, cohortRuleVersion: "x", aggregatePayloadSha256: DIGEST }),
    "nmu-turns-research-epoch003.csv",
  );
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

test("parseResearchPage：真实研究分区没有冻结纪元标识就拒绝渲染", () => {
  // 服务端把行面从纪元上解开时，屏幕上那一页看起来与绑着时一模一样——所以这一条
  // 必须由前端自己判，不能指望"反正服务端会拒"。
  const { release, ...withoutRelease } = pagePayload();
  void release;
  assert.throws(
    () => parseResearchPage(withoutRelease, "turns", "research"),
    /出自哪一个冻结纪元/,
  );
  assert.throws(
    () => parseResearchPage(pagePayload({ release: null }), "turns", "research"),
    /未冻结的数据/,
  );
});

test("parseResearchPage：模拟演练分区带着纪元标识同样拒绝", () => {
  // 反方向也要挡：仿真数据看起来像"已冻结的正式发布"会让人拿它写论文。
  assert.throws(
    () => parseResearchPage(pagePayload(), "turns", "simulation"),
    /不该带冻结纪元标识/,
  );
  const page = parseResearchPage(pagePayload({ release: null }), "turns", "simulation");
  assert.equal(page.release, null);
});

test("parseResearchPage：纪元标识本身也要校，坏形状不许当成「有版本」", () => {
  for (const bad of [
    { ...RELEASE, epoch_seq: 0 },
    { ...RELEASE, epoch_seq: 1.5 },
    { ...RELEASE, aggregate_payload_sha256: "not-a-digest" },
    { ...RELEASE, cohort_rule_version: "" },
  ]) {
    assert.throws(
      () => parseResearchPage(pagePayload({ release: bad }), "turns", "research"),
      /release/,
      JSON.stringify(bad),
    );
  }
});

test("parseResearchReleaseState：没绑上时要说清是哪一道闸拦的", () => {
  const closed = parseResearchReleaseState({
    bound: false, code: "research_release_not_frozen", reason: "还没有切过纪元",
  });
  assert.equal(closed.bound, false);
  if (closed.bound) return;
  assert.equal(closed.code, "research_release_not_frozen");
  // 只给 bound:false 而不说原因，等于把 503 原样搬到屏幕上
  assert.throws(() => parseResearchReleaseState({ bound: false }), /code/);
});

test("parseResearchMeta：接口状态必须带上冻结发布的绑定状态", () => {
  const { research_release, ...withoutRelease } = metaPayload();
  void research_release;
  assert.throws(() => parseResearchMeta(withoutRelease), /research_release/);
  const meta = parseResearchMeta(metaPayload());
  assert.equal(meta.release.bound, true);
  if (!meta.release.bound) return;
  assert.equal(meta.release.epochSeq, 3);
  assert.equal(meta.release.frozenSessionCount, 12);
});

test("parseResearchMeta：密钥没配时也要如实说行面没绑上", () => {
  const meta = parseResearchMeta(metaPayload({
    deidentification: { configured: false, reason: "去标识密钥未配置" },
    datasets: [],
    research_release: {
      bound: false, code: "research_deidentification_unavailable",
      reason: "去标识密钥未配置",
    },
  }));
  assert.equal(meta.release.bound, false);
});
