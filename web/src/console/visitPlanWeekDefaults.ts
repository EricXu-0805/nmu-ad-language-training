// P1-2:安排屏的训练周次默认值——不再写死第 2 周。
// 推导规则:该受试者已有的非取消安排覆盖了哪些周次,从 1..8 找第一个空缺;
// 全排满停在 8;没有任何数据(推导不了)默认 1。服务器仍是审核权威。
import type { VisitPlanReceipt } from "../types";

export const MAX_TRAINING_WEEK = 8;

export function nextUnplannedWeek(
  plans: readonly VisitPlanReceipt[] | null | undefined,
): number {
  if (!plans) return 1;
  const covered = new Set(
    plans.filter((plan) => plan.status !== "cancelled").map((plan) => plan.week_no));
  for (let week = 1; week <= MAX_TRAINING_WEEK; week += 1) {
    if (!covered.has(week)) return week;
  }
  return MAX_TRAINING_WEEK;
}

// 同槽再训:后端协议槽位键 = (受试者, 序号, 周次, 阶段, 任务线)。序号推导必须
// 按同一组键过滤——只按周计数会把第 1 周合法共存的三个环节(关系建立/基线测评/
// 前测,同 sitting 不同 phase/event_line)误标成「第 2、3 次」且服务器照单全收。
// 场次中止后 plan 停在 started、槽位不释放,同槽继续只能用下一个序号——后端本来
// 就放行这条路,这里只负责把默认值指对,审核权威仍在服务器。
export function occupiedSittingsForSlot(
  plans: readonly VisitPlanReceipt[] | null | undefined,
  weekNo: number,
  phaseType: string,
  eventLine: string,
): number[] {
  if (!plans) return [];
  const occupied = plans
    .filter((plan) => plan.status !== "cancelled"
      && plan.week_no === weekNo
      && plan.phase_type === phaseType
      && plan.event_line === eventLine)
    .map((plan) => plan.session_sitting_no);
  return [...new Set(occupied)].sort((a, b) => a - b);
}

export function nextSittingNoForSlot(
  plans: readonly VisitPlanReceipt[] | null | undefined,
  weekNo: number,
  phaseType: string,
  eventLine: string,
): number {
  const occupied = occupiedSittingsForSlot(plans, weekNo, phaseType, eventLine);
  return occupied.length === 0 ? 1 : Math.max(...occupied) + 1;
}

// 下拉旁的可见说明:这位受试者已完成/进行中的周次一目了然。
export function plannedWeeksSummary(
  plans: readonly VisitPlanReceipt[] | null | undefined,
): string {
  if (!plans) return "正在读取该受试者的既有安排…";
  const started: number[] = [];
  const pending: number[] = [];
  for (const plan of plans) {
    if (plan.status === "started") started.push(plan.week_no);
    else if (plan.status === "draft" || plan.status === "approved") pending.push(plan.week_no);
  }
  const uniqueSorted = (weeks: number[]) => [...new Set(weeks)].sort((a, b) => a - b);
  const parts: string[] = [];
  if (started.length > 0) {
    parts.push(`已开训：第 ${uniqueSorted(started).join("、")} 周`);
  }
  if (pending.length > 0) {
    parts.push(`待进行：第 ${uniqueSorted(pending).join("、")} 周`);
  }
  if (parts.length === 0) return "这位受试者还没有训练安排，通常从第 1 周开始";
  return `${parts.join("；")}；默认排到下一个空缺周次`;
}
