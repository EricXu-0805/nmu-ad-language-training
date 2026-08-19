// 展示格式化。评分只作展示,真值永远在后端锁定分/重建函数。
// ratioPct:0–1 比率 → 百分比(naming_accuracy 等);pct:已是百分制的数(weekly_de_score_percentile)。
export const ratioPct = (x: number | null | undefined): string =>
  x == null ? "—" : `${(x * 100).toFixed(1)}%`;
export const pct = (x: number | null | undefined): string =>
  x == null ? "—" : `${(x as number).toFixed(1)}%`;

export const num = (x: number | null | undefined, digits = 2): string =>
  x == null ? "—" : Number(x).toFixed(digits);

export const secs = (s: number | null | undefined): string =>
  s == null ? "—" : `${Number(s).toFixed(1)}s`;

// 环节值展示:全部环节 0/1(关系识别 0.5 档已于 2026-08-19 取消)。
export const elementLabel = (v: number | null | undefined): string => {
  if (v == null) return "未评";
  return v >= 1 ? "是(1)" : "否(0)";
};
