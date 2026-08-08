import type { EventLine, PhaseType, Session } from "../types";

export interface ProtocolAdmissionDecision {
  /** 可审核/开场(approve/start 门禁镜像)。 */
  allowed: boolean;
  /** 可留草稿:服务端 create 只验蓝图事件线,未结构化周的排期事实允许先行留档。 */
  draftAllowed: boolean;
  reason: string | null;
}

export type BedsideProtocolRoute =
  | { screen: "training" | "relationship"; reason: null }
  | { screen: "unsupported"; reason: string };

/** 服务端 GET /content/item-bank 的逐周内容状态投影(structured_training_weeks / training_week_content)。 */
export interface TrainingContentStatus {
  structuredWeeks: number[];
  researchReadyWeeks: number[];
}

// 第1周关系建立不消耗题库,真实研究门禁锚定平台默认题库(周2)——与服务端
// content.RAPPORT_ANCHOR_WEEK 同口径。
const RAPPORT_ANCHOR_WEEK = 2;

function trainingCombo(weekNo: number, phase: PhaseType, eventLine: EventLine): boolean {
  return weekNo >= 2 && weekNo <= 8 && phase === "正式训练" && eventLine === "正式训练";
}

function rapportCombo(weekNo: number, phase: PhaseType, eventLine: EventLine): boolean {
  return weekNo === 1 && phase === "关系建立" && eventLine === "关系建立环节";
}

function baselineCombo(weekNo: number, phase: PhaseType, eventLine: EventLine): boolean {
  return weekNo === 1 && (phase === "基线测评" || phase === "前测") && eventLine === "基线测评窗";
}

function researchGate(anchorWeek: number, contentStatus: TrainingContentStatus): ProtocolAdmissionDecision {
  if (contentStatus.researchReadyWeeks.includes(anchorWeek)) {
    return { allowed: true, draftAllowed: true, reason: null };
  }
  return {
    allowed: false,
    draftAllowed: true,
    reason: `第${anchorWeek}周真实研究题库 ready_for_research=false，仅开放专用模拟路径，不得将真实受试者切换为模拟来绕过。`,
  };
}

/**
 * Browser mirror of the server's VisitPlan operational + content gates.
 * The server remains authoritative; this mirror prevents a researcher from
 * spending time on a plan that can only fail at review/start.
 *
 * contentStatus 来自 GET /content/item-bank 的逐周结构化/质控信号;
 * null 表示尚未核对成功,一律 fail-closed 等待。
 */
export function assessVisitPlanProtocol(
  weekNo: number,
  phase: PhaseType,
  eventLine: EventLine,
  isSimulation: boolean | null,
  contentStatus: TrainingContentStatus | null,
): ProtocolAdmissionDecision {
  if (baselineCombo(weekNo, phase, eventLine)) {
    return {
      allowed: false,
      draftAllowed: true,
      reason: `第1周${phase}走正式量表评估事件工作流，不建训练场次。`,
    };
  }
  const isTraining = trainingCombo(weekNo, phase, eventLine);
  const isRapport = rapportCombo(weekNo, phase, eventLine);
  if (!isTraining && !isRapport) {
    return { allowed: false, draftAllowed: false, reason: "周次、阶段与任务线的组合没有可执行的床旁协议合同。" };
  }
  if (isSimulation === null) {
    return { allowed: false, draftAllowed: true, reason: "正在核对档案的研究/模拟分区，核对完成前不能审核安排。" };
  }
  if (contentStatus === null) {
    return { allowed: false, draftAllowed: true, reason: "正在核对训练题库的结构化与质控状态，核对完成前不能审核安排。" };
  }
  if (isTraining && !contentStatus.structuredWeeks.includes(weekNo)) {
    return {
      allowed: false,
      draftAllowed: true,
      reason: `第${weekNo}周材料尚未结构化并校对：服务端题库索引未登记该周。`,
    };
  }
  if (isSimulation === true) return { allowed: true, draftAllowed: true, reason: null };
  return researchGate(isTraining ? weekNo : RAPPORT_ANCHOR_WEEK, contentStatus);
}

export function bedsideProtocolRoute(session: Session): BedsideProtocolRoute {
  // 场次能存在,说明 approve/start 已通过服务端分区+题库门禁(且 start 时会复验);
  // 这里只判断该协议组合有没有已实现的床旁界面,不重复分区/内容判定——否则合法
  // 的真实研究场次会在开场成功后反而被前端挡在对应床旁屏外。
  if (trainingCombo(session.week_no, session.phase_type, session.event_line)) {
    return { screen: "training", reason: null };
  }
  if (rapportCombo(session.week_no, session.phase_type, session.event_line)) {
    return { screen: "relationship", reason: null };
  }
  if (baselineCombo(session.week_no, session.phase_type, session.event_line)) {
    return { screen: "unsupported", reason: "第1周基线测评走正式量表评估事件工作流，没有床旁训练界面。" };
  }
  return { screen: "unsupported", reason: "周次、阶段与任务线的组合没有可执行的床旁协议合同。" };
}
