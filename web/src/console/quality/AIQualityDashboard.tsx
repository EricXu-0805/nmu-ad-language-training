import { useId } from "react";
import { Alert } from "../../components/Alert";
import { QualityConfusionMatrix } from "./QualityConfusionMatrix";
import { QualityMetricList } from "./QualityMetricList";
import type { AIQualityDashboardViewModel } from "./qualityDashboardViewModel";

interface AIQualityDashboardProps {
  model: AIQualityDashboardViewModel;
  headingId?: string;
}

export function AIQualityDashboard({
  model,
  headingId,
}: AIQualityDashboardProps) {
  const dashboardInstanceId = useId();
  const resolvedHeadingId = headingId ?? `${dashboardInstanceId}-heading`;
  return (
    <section className="form-section" aria-labelledby={resolvedHeadingId}>
      <header className="form-section-header">
        <div>
          <h2 id={resolvedHeadingId}>AI 质量后台</h2>
          <p className="muted">
            首版只显示当前数据分区的 overall 聚合计数、覆盖诊断与隐私状态；
            不显示受试者标识、录音、作答、转写或自由文本版本维度。
          </p>
        </div>
        <p className="muted quality-generated-at">
          生成时间：<time dateTime={model.generatedAt}>{model.generatedAt}</time>
        </p>
      </header>

      <Alert role="note" tone="info" title="两套口径严格分开">
        “AI 运行质量”只描述系统覆盖、录音尝试所处提示上下文、安全和 AI 处理延迟。真实研究区仅以人工锁定真值核查 AI；
        模拟区仅显示不可用于研究结论的复核参考。没有相应复核参考时，准确性一律保持未知。
      </Alert>

      {model.groups.length === 0 && (
        <p className="muted" role="status">暂无聚合质量数据；所有指标保持未知。</p>
      )}

      {model.groups.map((group, groupIndex) => {
        const groupDomId = `${dashboardInstanceId}-group-${String(groupIndex + 1)}`;
        const operationalHeadingId = `${groupDomId}-operational`;
        const promptHeadingId = `${groupDomId}-prompts`;
        const safetyHeadingId = `${groupDomId}-safety`;
        const latencyHeadingId = `${groupDomId}-latency`;
        const coverageHeadingId = `${groupDomId}-coverage`;
        const diagnosticsHeadingId = `${groupDomId}-diagnostics`;
        const truthHeadingId = `${groupDomId}-truth`;
        const researchTruthMetrics = [
          group.researchTruth.reviewedDecisions,
          group.researchTruth.agreementRate,
          group.researchTruth.falsePositiveRate,
          group.researchTruth.falseNegativeRate,
        ];
        return (
          <article className="form-section" key={group.key} aria-labelledby={groupDomId}>
            <header className="form-section-header">
              <div>
                <p className="page-kicker">聚合质量分组</p>
                <h3 id={groupDomId}>{group.heading}</h3>
              </div>
            </header>

            <Alert role="note" tone={group.visibilityNotice.tone} title={group.visibilityNotice.title}>
              {group.visibilityNotice.text}
            </Alert>

            <Alert role="note" tone={group.suppressionNotice.tone} title={group.suppressionNotice.title}>
              {group.suppressionNotice.text}
            </Alert>

            <section aria-label="当前聚合分组维度">
              <h4>分组维度</h4>
              <dl className="quality-dimension-list">
                {group.dimensions.map((dimension) => (
                  <div key={dimension.key} data-state={dimension.known ? "known" : "unknown"}>
                    <dt>{dimension.label}</dt>
                    <dd>{dimension.value}</dd>
                  </div>
                ))}
              </dl>
            </section>

            <QualityMetricList headingId={coverageHeadingId} title="公开范围与证据覆盖" metrics={group.coverageMetrics} />

            <section aria-labelledby={diagnosticsHeadingId}>
              <h4 id={diagnosticsHeadingId}>固定覆盖诊断</h4>
              <Alert role="note" tone={group.diagnosticsNotice.tone} title={group.diagnosticsNotice.title}>
                {group.diagnosticsNotice.text}
              </Alert>
              <QualityMetricList
                headingId={`${diagnosticsHeadingId}-reasons`}
                title="固定排除与未知原因计数"
                metrics={group.diagnosticMetrics}
              />
            </section>

            <section aria-labelledby={operationalHeadingId}>
              <h4 id={operationalHeadingId}>AI 运行质量（非研究真值）</h4>
              <QualityMetricList
                headingId={`${operationalHeadingId}-coverage`}
                title="覆盖与 ASR"
                metrics={group.operationalMetrics}
              />
              <QualityMetricList headingId={promptHeadingId} title="录音尝试所处提示上下文与告知答案证据" metrics={group.promptMetrics} />
              <QualityMetricList headingId={safetyHeadingId} title="技术失败、暂停与人工接管" metrics={group.safetyMetrics} />
              <QualityMetricList headingId={latencyHeadingId} title="AI 处理延迟（录音已上传→判类完成）" metrics={group.latencyMetrics} />
            </section>

            <section aria-labelledby={truthHeadingId}>
              <h4 id={truthHeadingId}>{group.researchTruth.sectionTitle}</h4>
              <Alert role="note" tone={group.researchTruth.notice.tone} title={group.researchTruth.notice.title}>
                {group.researchTruth.notice.text}
              </Alert>
              <QualityMetricList
                headingId={`${truthHeadingId}-metrics`}
                title={group.researchTruth.metricsTitle}
                metrics={researchTruthMetrics}
              />
              <QualityConfusionMatrix
                matrix={group.researchTruth.matrix}
                comparisonKind={group.researchTruth.comparisonKind}
              />
            </section>
          </article>
        );
      })}
    </section>
  );
}
