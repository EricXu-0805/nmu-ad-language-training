import type { QualityConfusionMatrixViewModel } from "./qualityDashboardViewModel";

interface QualityConfusionMatrixProps {
  matrix: QualityConfusionMatrixViewModel;
  comparisonKind: "research" | "simulation";
}

const SCROLL_REGION_STYLE = { overflowX: "auto" } as const;

export function QualityConfusionMatrix({ matrix, comparisonKind }: QualityConfusionMatrixProps) {
  const referenceLabel = comparisonKind === "research" ? "人工锁定研究真值" : "模拟复核参考";
  return (
    <div
      className="quality-confusion-matrix-region"
      style={SCROLL_REGION_STYLE}
      tabIndex={0}
      role="region"
      aria-label={`AI 判定与${referenceLabel}混淆矩阵，可横向滚动`}
    >
      <table className="quality-confusion-matrix">
        <caption>AI 判定与{referenceLabel}混淆矩阵</caption>
        <thead>
          <tr>
            <th scope="col">判定来源</th>
            <th scope="col">复核判正确</th>
            <th scope="col">复核判错误</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">AI 判正确</th>
            <td data-state={matrix.truePositive.state}>
              <abbr title="真阳性">TP</abbr>：{matrix.truePositive.value}
            </td>
            <td data-state={matrix.falsePositive.state}>
              <abbr title="假阳性">FP</abbr>：{matrix.falsePositive.value}
            </td>
          </tr>
          <tr>
            <th scope="row">AI 判错误</th>
            <td data-state={matrix.falseNegative.state}>
              <abbr title="假阴性">FN</abbr>：{matrix.falseNegative.value}
            </td>
            <td data-state={matrix.trueNegative.state}>
              <abbr title="真阴性">TN</abbr>：{matrix.trueNegative.value}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}
