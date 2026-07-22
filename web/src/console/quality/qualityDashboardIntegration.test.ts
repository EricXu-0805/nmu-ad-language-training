import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("API binds one no-store quality request to the selected classification and strict v2 parser", () => {
  const api = source("../../api.ts");

  assert.match(api, /getAIQualityMetrics: async \(/);
  assert.match(api, /qualityDashboardRequestPath\(classification\)/);
  assert.match(api, /\{ noStore: true, signal \}/);
  assert.match(api, /\), classification\),/);
  assert.match(api, /retryAfter: res\.headers\.get\("Retry-After"\)/);
});

test("analysis screen never requests legacy quality data and never flashes the previous classification", () => {
  const analysis = source("../AnalysisScreen.tsx");

  assert.match(analysis, /qualityDashboardRequestClassification\(classificationFilter\)/);
  assert.match(analysis, /if \(classification === null\) \{\s+setQualityState\(null\);\s+return;/);
  assert.match(analysis, /qualityState\?\.classification === qualityClassification/);
  assert.match(analysis, /历史\/未知分区不请求 AI 质量汇总/);
  assert.match(analysis, /不会沿用上一分区数据/);
});

test("analysis screen exposes loading, forbidden, contract error, retry, and ready states", () => {
  const analysis = source("../AnalysisScreen.tsx");

  assert.match(analysis, /正在加载当前分区 AI 质量 overall/);
  assert.match(analysis, /当前账号无权查看 AI 质量汇总/);
  assert.match(analysis, /服务器不可用或返回结果未通过 v2 聚合隐私契约/);
  assert.match(analysis, /重试质量汇总/);
  assert.match(analysis, /disabled=\{qualityRetrySeconds > 0\}/);
  assert.match(analysis, /秒后可重试/);
  assert.match(analysis, /<AIQualityDashboard/);
  assert.match(analysis, /const controller = new AbortController\(\)/);
  assert.match(analysis, /controller\.abort\(\)/);
});
