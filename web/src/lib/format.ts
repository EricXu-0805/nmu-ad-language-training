// 展示格式化。评分只作展示,真值永远在后端锁定分/重建函数。
export const pct = (x: number | null | undefined): string =>
  x == null ? "—" : `${(x as number).toFixed(1)}%`;

export const num = (x: number | null | undefined, digits = 2): string =>
  x == null ? "—" : Number(x).toFixed(digits);

export const secs = (s: number | null | undefined): string =>
  s == null ? "—" : `${Number(s).toFixed(1)}s`;

// double 环节值展示:关系识别 0/0.5/1 → 未识别/部分/识别;其余 0/1 → 否/是。
export const elementLabel = (v: number | null | undefined): string => {
  if (v == null) return "未评";
  if (v === 0.5) return "部分(0.5)";
  return v >= 1 ? "是(1)" : "否(0)";
};
