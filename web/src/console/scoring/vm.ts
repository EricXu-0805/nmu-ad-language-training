// 判分侧视图模型 + ★运行时画像守卫(五层隔离的第 4 层)。
// 本目录(console/scoring/**)由 .oxlintrc 的 no-restricted-imports 禁止 import 任何画像源;
// 这里再加一层运行时断言:构造 lock/判分载荷时,keys ∩ PORTRAIT_FIELDS 必须为空。
import { PORTRAIT_FIELDS } from "../../types";
import type { TaskType } from "../../types";

export interface TurnScoringVM {
  turnId: number;
  taskType: TaskType;
  responseRole: string;
  imageId: string | null;
  target?: string | null;      // 判分参考(命名类环节)
  leftWord?: string | null;
  rightWord?: string | null;
  asrText: string | null;
  confirmedText: string | null;
  ai?: { answerType: string | null; score: number | null; needsReview: boolean };
}

export function assertPortraitFree(payload: Record<string, unknown>): void {
  const portrait = PORTRAIT_FIELDS as readonly string[];
  const leaked = Object.keys(payload).filter((k) => portrait.includes(k));
  if (leaked.length) {
    throw new Error(`判分/锁分载荷混入画像字段 [${leaked.join(", ")}]——违反『画像不进判分』`);
  }
}

// 关系识别环节用三段值 0 / 0.5 / 1;其余(命名/作用/关键要素)为 0 / 1。
export const isRelationRole = (role: string): boolean => role === "关系识别";
