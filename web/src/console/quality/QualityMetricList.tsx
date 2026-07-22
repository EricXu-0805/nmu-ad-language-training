import type { QualityMetricViewModel } from "./qualityDashboardViewModel";

interface QualityMetricListProps {
  headingId: string;
  title: string;
  metrics: QualityMetricViewModel[];
}

export function QualityMetricList({ headingId, title, metrics }: QualityMetricListProps) {
  return (
    <section className="quality-metric-section" aria-labelledby={headingId}>
      <h5 id={headingId}>{title}</h5>
      <dl className="quality-metric-list">
        {metrics.map((metric) => (
          <div className="quality-metric-list__item" data-state={metric.state} key={metric.key}>
            <dt>{metric.label}</dt>
            <dd>
              <strong>{metric.value}</strong>
              <br />
              <span className="muted">{metric.detail}</span>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
