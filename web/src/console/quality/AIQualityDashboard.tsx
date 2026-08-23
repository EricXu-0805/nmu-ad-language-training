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
            只显示汇总计数，不含任何受试者个人内容。
          </p>
        </div>
        <p className="muted quality-generated-at">
          生成时间：<time dateTime={model.generatedAt}>{model.generatedAt.replace("T", " ").slice(0, 19)}</time>
        </p>
      </header>

      <Alert role="note" tone="info" title="两套口径严格分开">
        “AI 运行质量”只说明系统是否正常运转和处理速度，不代表判分准确。
        准确性只以人工核对的结果为准；没有人工核对时，一律显示“未知”。
        模拟区的数据只作调试参考，不用于研究结论。
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

            {!group.metricsWithheld && (<>
            <section aria-label="当前聚合分组维度">
              <h4>分组维度</h4>
              <dl className="quality-dimension-list">
                {group.dimensions.filter((dimension) => dimension.known).map((dimension) => (
                  <div key={dimension.key} data-state="known">
                    <dt>{dimension.label}</dt>
                    <dd>{dimension.value}</dd>
                  </div>
                ))}
              </dl>
              {group.dimensions.some((dimension) => !dimension.known) && (
                <details>
                  <summary>查看未拆分的维度</summary>
                  <dl className="quality-dimension-list">
                    {group.dimensions.filter((dimension) => !dimension.known).map((dimension) => (
                      <div key={dimension.key} data-state="unknown">
                        <dt>{dimension.label}</dt>
                        <dd>{dimension.value}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              )}
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
              <h4 id={operationalHeadingId}>AI 运行质量（不是研究评分）</h4>
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
            </>)}
          </article>
        );
      })}
    </section>
  );
}
