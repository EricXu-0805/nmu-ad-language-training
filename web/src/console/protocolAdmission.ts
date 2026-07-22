import type { EventLine, PhaseType, Session } from "../types";

export interface ProtocolAdmissionDecision {
  allowed: boolean;
  reason: string | null;
}

export type BedsideProtocolRoute =
  | { screen: "training" | "relationship"; reason: null }
  | { screen: "unsupported"; reason: string };

function exactWeekTwoTraining(weekNo: number, phase: PhaseType, eventLine: EventLine): boolean {
  return weekNo === 2 && phase === "正式训练" && eventLine === "正式训练";
}

/**
 * Browser mirror of the server's deliberately narrow P0 VisitPlan gate.
 * The server remains authoritative; this mirror prevents a researcher from
 * spending time on a plan that can only fail at review/start.
 */
export function assessVisitPlanProtocol(
  weekNo: number,
  phase: PhaseType,
  eventLine: EventLine,
  isSimulation: boolean | null,
): ProtocolAdmissionDecision {
  if (exactWeekTwoTraining(weekNo, phase, eventLine)) {
    if (isSimulation === true) return { allowed: true, reason: null };
    if (isSimulation === null) {
      return { allowed: false, reason: "正在核对档案的研究/模拟分区，核对完成前不能保存或审核安排。" };
    }
    return {
      allowed: false,
      reason: "当前真实研究题库 ready_for_research=false，第2周仅开放专用模拟验收路径，不得将真实受试者切换为模拟来绕过。",
    };
  }
  if (weekNo === 1 && phase === "关系建立" && eventLine === "关系建立环节") {
    return { allowed: false, reason: "第1周关系建立尚未冻结脚本、录音和退出条件的独立完成合同。" };
  }
  if (weekNo === 1 && (phase === "基线测评" || phase === "前测") && eventLine === "基线测评窗") {
    return { allowed: false, reason: `第1周${phase}尚未冻结独立完成合同，不能进入关系建立页替代。` };
  }
  if (weekNo >= 3 && weekNo <= 8 && phase === "正式训练" && eventLine === "正式训练") {
    return { allowed: false, reason: `第${weekNo}周尚无冻结训练计划和独立完成合同。` };
  }
  return { allowed: false, reason: "周次、阶段与任务线的组合没有可执行的床旁协议合同。" };
}

export function bedsideProtocolRoute(session: Session): BedsideProtocolRoute {
  const decision = assessVisitPlanProtocol(
    session.week_no,
    session.phase_type,
    session.event_line,
    session.is_simulation,
  );
  if (decision.allowed) return { screen: "training", reason: null };
  return { screen: "unsupported", reason: decision.reason ?? "当前协议组合未开放。" };
}
