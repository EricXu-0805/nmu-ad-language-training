import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

function unknownMetric(key, label) {
  return { key, label, value: "未知", detail: "模拟复核参考不可用", state: "unknown" };
}

function simulationModel() {
  const truePositive = unknownMetric("tp", "真阳性 TP");
  const falsePositive = unknownMetric("fp", "假阳性 FP");
  const falseNegative = unknownMetric("fn", "假阴性 FN");
  const trueNegative = unknownMetric("tn", "真阴性 TN");
  return {
    generatedAt: "2026-07-22T10:00:00Z",
    groups: [{
      key: "simulation",
      heading: "模拟演练 overall",
      classification: "simulation",
      dimensions: [],
      visibilityNotice: {
        tone: "info",
        title: "当前账号可见范围：全部授权场次",
        text: "overall 仅包含当前账号有权访问的场次。",
      },
      suppressionNotice: {
        tone: "info",
        title: "模拟分区不适用真实研究小单元门槛",
        text: "不可并入真实研究结论。",
      },
      diagnosticsNotice: { tone: "info", title: "覆盖诊断完整", text: "只描述证据覆盖。" },
      coverageMetrics: [],
      diagnosticMetrics: [],
      operationalMetrics: [],
      promptMetrics: [],
      safetyMetrics: [],
      latencyMetrics: [],
      researchTruth: {
        availability: "unknown",
        sectionTitle: "模拟复核参考（不可用于研究结论）",
        metricsTitle: "模拟复核比较指标",
        notice: { tone: "warn", title: "模拟复核参考不可用", text: "不得推断准确性。" },
        comparisonKind: "simulation",
        reviewedDecisions: unknownMetric("reviewed", "模拟复核判定"),
        agreementRate: unknownMetric("agreement", "AI 与模拟复核一致率"),
        falsePositiveRate: unknownMetric("fp-rate", "假阳性率"),
        falseNegativeRate: unknownMetric("fn-rate", "假阴性率"),
        matrix: { truePositive, falsePositive, falseNegative, trueNegative },
      },
    }],
  };
}

test("real SSR output keeps simulation comparison non-research and produces unique accessible ids", async (context) => {
  const vite = await createServer({
    root: process.cwd(),
    appType: "custom",
    logLevel: "silent",
    server: { middlewareMode: true },
  });
  context.after(() => vite.close());
  const { AIQualityDashboard } = await vite.ssrLoadModule(
    "/src/console/quality/AIQualityDashboard.tsx",
  );
  const model = simulationModel();
  const markup = renderToStaticMarkup(React.createElement(
    React.Fragment,
    null,
    React.createElement(AIQualityDashboard, { model }),
    React.createElement(AIQualityDashboard, { model }),
  ));

  assert.match(markup, /模拟复核参考（不可用于研究结论）/);
  assert.match(markup, /当前账号可见范围：全部授权场次/);
  assert.match(markup, /class="muted quality-generated-at"/);
  assert.match(markup, /AI 判定与模拟复核参考混淆矩阵/);
  assert.doesNotMatch(markup, /人工锁定研究真值|人工研究真值/);
  assert.match(markup, /<caption>AI 判定与模拟复核参考混淆矩阵<\/caption>/);
  assert.match(markup, /scope="col"/);
  assert.match(markup, /scope="row"/);
  assert.ok((markup.match(/role="note"/g) ?? []).length >= 8);

  const labelledBy = [...markup.matchAll(/<section class="form-section" aria-labelledby="([^"]+)"/g)]
    .map((match) => match[1]);
  assert.equal(labelledBy.length, 2);
  assert.notEqual(labelledBy[0], labelledBy[1]);
  for (const id of labelledBy) assert.match(markup, new RegExp(`id="${id}"`));
});
