import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("quality dashboard exposes operational, privacy, coverage, and classification-aware comparison regions", () => {
  const dashboard = source("./AIQualityDashboard.tsx");

  assert.match(dashboard, /AI 运行质量（非研究真值）/);
  assert.match(dashboard, /模拟区仅显示不可用于研究结论的复核参考/);
  assert.match(dashboard, /公开范围与证据覆盖/);
  assert.match(dashboard, /固定覆盖诊断/);
  assert.match(dashboard, /group\.suppressionNotice/);
  assert.match(dashboard, /group\.visibilityNotice/);
  assert.match(dashboard, /group\.diagnosticsNotice/);
  assert.match(dashboard, /不显示受试者标识、录音、作答、转写或自由文本版本维度/);
  assert.match(dashboard, /<time dateTime=\{model\.generatedAt\}>/);
  assert.match(dashboard, /quality-generated-at/);
  assert.match(dashboard, /AI 处理延迟（录音已上传→判类完成）/);
  assert.doesNotMatch(dashboard, /端到端延迟/);
  assert.doesNotMatch(dashboard, /dangerouslySetInnerHTML/);
  assert.match(dashboard, /const dashboardInstanceId = useId\(\)/);
  assert.match(dashboard, /const resolvedHeadingId = headingId \?\? `\$\{dashboardInstanceId\}-heading`/);
  assert.doesNotMatch(dashboard, /stableDomHash|group\.domId/);
  assert.ok((dashboard.match(/role="note"/g) ?? []).length >= 4);
});

test("group dimensions and metric collections use semantic description lists", () => {
  const dashboard = source("./AIQualityDashboard.tsx");
  const metricList = source("./QualityMetricList.tsx");

  assert.match(dashboard, /aria-label="当前聚合分组维度"/);
  assert.match(dashboard, /<dl className="quality-dimension-list">/);
  assert.match(metricList, /<dl className="quality-metric-list">/);
  assert.match(metricList, /<dt>\{metric\.label\}<\/dt>/);
  assert.match(metricList, /<dd>/);
});

test("confusion matrix has a caption, row and column headers, and a named scroll region", () => {
  const matrix = source("./QualityConfusionMatrix.tsx");

  assert.match(matrix, /comparisonKind === "research" \? "人工锁定研究真值" : "模拟复核参考"/);
  assert.match(matrix, /aria-label=\{`AI 判定与\$\{referenceLabel\}混淆矩阵，可横向滚动`\}/);
  assert.match(matrix, /<caption>AI 判定与\{referenceLabel\}混淆矩阵<\/caption>/);
  assert.equal((matrix.match(/scope="col"/g) ?? []).length, 3);
  assert.equal((matrix.match(/scope="row"/g) ?? []).length, 2);
  assert.match(matrix, /<abbr title="假阳性">FP<\/abbr>/);
  assert.match(matrix, /<abbr title="假阴性">FN<\/abbr>/);
});
