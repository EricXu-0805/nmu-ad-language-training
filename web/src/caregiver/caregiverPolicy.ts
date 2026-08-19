export const CAREGIVER_VISIBLE_ACTION_LABELS = [
  "今天任务",
  "开始本次",
  "开始练习",
  "暂停练习",
  "请求协助",
  "接管练习",
  "结束本次",
  "退出",
] as const;

export const CAREGIVER_DEMO_PROFILE_VERSION = "week2-single20-demo-v1" as const;
export const CAREGIVER_DEMO_COMPLETION_SCOPE = "demo_plan_only" as const;
export const CAREGIVER_DEMO_POSITION_COUNT = 20 as const;
export const CAREGIVER_DEMO_BOUNDARY_MESSAGE =
  "演练模式：仅供操作练习，不得用于真实老人或正式研究。" as const;

export type CaregiverDataClassification = "research" | "simulation";
export type CaregiverCompletionScope = "canonical_full_source" | "demo_plan_only";

export const CAREGIVER_HELP_REASONS = [
  {
    code: "participant_distress",
    label: "老人明显不安或不舒服",
    hint: "先停下来，请负责人到场看一下。",
  },
  {
    code: "participant_request",
    label: "老人主动要求帮助",
    hint: "包括想休息、想找家属或需要工作人员。",
  },
  {
    code: "clinical_concern",
    label: "工作人员担心老人安全",
    hint: "只要觉得不对劲，就应选这一项。",
  },
  {
    code: "technical_failure",
    label: "设备或系统无法继续",
    hint: "例如没有声音、画面卡住或连接中断。",
  },
  {
    code: "other_staff_needed",
    label: "需要其他工作人员到场",
    hint: "不属于上述情况，但现场需要多一人协助。",
  },
] as const;

export type CaregiverHelpReasonCode = (typeof CAREGIVER_HELP_REASONS)[number]["code"];

export const CAREGIVER_END_REASONS = [
  {
    code: "finish",
    label: "本次练习已按计划做完",
    hint: "只有页面已完成全部练习时才选。",
  },
  {
    code: "participant_declined",
    label: "老人表示不想继续",
    hint: "尊重老人的意愿，不要劝其继续。",
  },
  {
    code: "clinical_safety",
    label: "为了老人安全停止",
    hint: "包括不适、明显焦虑或其他安全顾虑。",
  },
  {
    code: "technical_failure",
    label: "设备或系统无法继续",
    hint: "反复检查后仍无法安全继续时选。",
  },
] as const;

export type CaregiverEndSelection = (typeof CAREGIVER_END_REASONS)[number]["code"];
export type CaregiverAbortReasonCode = Exclude<CaregiverEndSelection, "finish">;

export interface CaregiverIdentity {
  displayId: string;
  username?: string;
}

export interface CaregiverPlan {
  planId: string;
  participantLabel: string;
  scheduledDate: string;
  scheduledTime: string | null;
  weekLabel: string;
  phaseLabel: string;
  revision: number;
  isSimulation: true;
  dataClassification: "simulation";
  autopilotProfileVersionId: typeof CAREGIVER_DEMO_PROFILE_VERSION;
  completionScope: typeof CAREGIVER_DEMO_COMPLETION_SCOPE;
  resolvedPositionCount: typeof CAREGIVER_DEMO_POSITION_COUNT;
  operationalDemoReady: true;
}

export interface CaregiverOperationalBoundary {
  /** 保留服务端原始边界，用于识别历史/非模拟场次，不在前端猜测。 */
  isSimulation: boolean;
  dataClassification: CaregiverDataClassification;
  autopilotProfileVersionId: string | null;
  completionScope: CaregiverCompletionScope | null;
  resolvedPositionCount: number | null;
  /** 唯一可以开启自动练习的服务端权威结论。 */
  operationalDemoReady: boolean;
}

export interface CaregiverSessionSummary extends CaregiverOperationalBoundary {
  sessionId: string;
  participantLabel: string;
  weekLabel: string;
  phaseLabel: string;
}

export interface CaregiverToday {
  asOfDateLabel: string;
  plans: CaregiverPlan[];
  withheldCount: number;
  currentSession: CaregiverSessionSummary | null;
}

export type CaregiverRuntimeState =
  | "active"
  | "paused"
  | "intervention_completed"
  | "completed"
  | "aborted"
  | "failed";

export type CaregiverPracticeState =
  | "not_started"
  | "starting"
  | "running"
  | "pausing"
  | "paused"
  | "taken_over"
  | "ended"
  | "error";

export type CaregiverPatientPresence = "online" | "offline" | "unknown";

export interface CaregiverServerAllowedActions {
  startPractice: boolean;
  pause: boolean;
  help: boolean;
  takeOver: boolean;
  end: boolean;
}

export interface CaregiverSessionStatus extends CaregiverOperationalBoundary {
  sessionId: string;
  runtimeState: CaregiverRuntimeState;
  practiceState: CaregiverPracticeState;
  patientPresence: CaregiverPatientPresence;
  runtimeRevision: number;
  practiceRevision: number;
  /** 只有服务器已持久化精确收麦证据时才为 true。 */
  takeoverReady: boolean;
  allowed: CaregiverServerAllowedActions;
}

export interface CaregiverRevisionRequest {
  idempotencyKey: string;
  expectedRevision: number;
}

export interface CaregiverHelpRequest {
  reasonCode: CaregiverHelpReasonCode;
  idempotencyKey: string;
}

export type CaregiverEndRequest =
  | { kind: "finish" }
  | {
      kind: "abort";
      reasonCode: CaregiverAbortReasonCode;
      expectedRevision: number;
      idempotencyKey: string;
    };

export interface CaregiverHelpReceipt {
  recorded: true;
  requestId: string;
  status: CaregiverSessionStatus;
}

/** 求助四态。`delivered` 只能由通知通道带回执写入，界面上没有写它的入口。 */
export type CaregiverHelpState = "recorded" | "delivered" | "acknowledged" | "resolved";

/** 人能在界面上追加的两态。缺 `delivered` 是有意的，不是漏了。 */
export type CaregiverHelpHumanState = "acknowledged" | "resolved";

export interface CaregiverHelpStatus {
  requestId: string;
  state: CaregiverHelpState;
  notifyChannelConfigured: boolean;
  /** 没配通知对象时恒 false：那一态结构上到不了。 */
  deliveryReachable: boolean;
  reached: { state: CaregiverHelpState; actorId: string; at: string }[];
}

/**
 * 求助登记之后，屏上该说哪句话。
 *
 * 这是整个功能里最容易说谎的一处：把「已登记」写成「已通知负责人」，
 * 护理员就会站在原地等一个永远不会来的人。所以没有通知通道时，
 * 文案必须明确要求当面去叫人。
 */
export function caregiverHelpPrompt(status: CaregiverHelpStatus): {
  tone: "warn" | "info";
  text: string;
  canRecordArrival: boolean;
  canRecordResolved: boolean;
} {
  // 每一态能给哪些入口，由这一态本身完全决定——状态是"已追加转移里最靠后
  // 的那个"，所以 acknowledged 时不可能还没到过 acknowledged。早先这里按
  // `reached` 再算一遍，那个计算在任何一条分支上都恒为真，是句废话。
  if (status.state === "resolved") {
    return {
      tone: "info", text: "本次求助已处理完毕。",
      canRecordArrival: false, canRecordResolved: false,
    };
  }
  if (status.state === "acknowledged") {
    return {
      tone: "info",
      text: "已有工作人员到场并接手。处理完成后请在本页记一笔。",
      canRecordArrival: false,
      canRecordResolved: true,
    };
  }
  if (!status.deliveryReachable) {
    return {
      tone: "warn",
      text: "已记录，但系统不会通知任何人。请立即去叫负责人。",
      canRecordArrival: true,
      canRecordResolved: true,
    };
  }
  return {
    tone: "warn",
    text: status.state === "delivered"
      ? "通知已送达，正在等待工作人员到场。"
      : "已记录，通知还没确认送到。请同时自己去联系负责人。",
    canRecordArrival: true,
    canRecordResolved: true,
  };
}

/**
 * 页面仅依赖这个注入契约，不直接引用公共 API 客户端。
 * ConsoleShell 的照护员角色分支负责把已鉴权的实现传进来。
 */
export interface CaregiverApi {
  listToday(): Promise<CaregiverToday>;
  startVisitPlan(planId: string, request: CaregiverRevisionRequest): Promise<CaregiverSessionSummary>;
  activateSession(sessionId: string): Promise<CaregiverSessionStatus>;
  startPractice(sessionId: string, request: CaregiverRevisionRequest): Promise<CaregiverSessionStatus>;
  getSessionStatus(sessionId: string): Promise<CaregiverSessionStatus>;
  pauseSession(sessionId: string): Promise<CaregiverSessionStatus>;
  requestHelp(sessionId: string, request: CaregiverHelpRequest): Promise<CaregiverHelpReceipt>;
  getHelpStatus(sessionId: string, requestId: string): Promise<CaregiverHelpStatus>;
  recordHelpDisposition(
    sessionId: string, requestId: string,
    disposition: { state: CaregiverHelpHumanState; note: string },
  ): Promise<CaregiverHelpStatus>;
  takeOverSession(sessionId: string, request: CaregiverRevisionRequest): Promise<CaregiverSessionStatus>;
  endSession(sessionId: string, request: CaregiverEndRequest): Promise<CaregiverSessionStatus>;
}

export interface CaregiverActionAvailability {
  startPractice: boolean;
  pause: boolean;
  help: boolean;
  takeOver: boolean;
  end: boolean;
}

export interface CaregiverStatusPresentation {
  title: string;
  detail: string;
  tone: "info" | "ok" | "warn" | "danger";
}

export function identityIsCaregiverOperator(role: string | null | undefined): boolean {
  return role === "caregiver_operator";
}

export function caregiverSessionIsTerminal(status: CaregiverSessionStatus): boolean {
  return status.runtimeState === "intervention_completed"
    || status.runtimeState === "completed"
    || status.runtimeState === "aborted"
    || status.runtimeState === "failed";
}

/**
 * 服务端权限投影之上再加一层页面失败关闭。
 * 特别是暂停后绝不把“开始练习”变成变相重启。
 */
export function caregiverActionAvailability(status: CaregiverSessionStatus): CaregiverActionAvailability {
  if (caregiverSessionIsTerminal(status)) {
    return { startPractice: false, pause: false, help: false, takeOver: false, end: false };
  }
  return {
    startPractice: status.allowed.startPractice
      && status.operationalDemoReady
      && status.runtimeState === "active"
      && status.practiceState === "not_started"
      && status.patientPresence === "online",
    pause: status.allowed.pause && status.runtimeState === "active",
    help: status.allowed.help
      && (status.runtimeState === "active" || status.runtimeState === "paused"),
    takeOver: status.allowed.takeOver
      && status.takeoverReady
      && status.runtimeState === "paused"
      && status.practiceState === "paused",
    end: status.allowed.end
      && (status.runtimeState === "active" || status.runtimeState === "paused"),
  };
}

export function caregiverStatusPresentation(status: CaregiverSessionStatus): CaregiverStatusPresentation {
  if (status.runtimeState === "intervention_completed" || status.runtimeState === "completed") {
    return {
      title: "本次练习已结束",
      detail: "后续整理由负责人完成。",
      tone: "ok",
    };
  }
  if (status.runtimeState === "aborted") {
    return {
      title: "本次已提前结束",
      detail: "系统已停止自动练习。",
      tone: "warn",
    };
  }
  if (status.runtimeState === "failed") {
    return {
      title: "本次已停止",
      detail: "请保持老人舒适，并联系负责人。",
      tone: "danger",
    };
  }
  if (!status.operationalDemoReady) {
    return {
      title: "本次只能暂停或结束",
      detail: "本次不能开始练习，请联系负责人。",
      tone: "warn",
    };
  }
  if (status.practiceState === "taken_over") {
    return {
      title: "已由工作人员接管",
      detail: "系统已停止自动练习。",
      tone: "warn",
    };
  }
  if (status.runtimeState === "paused" || status.practiceState === "paused") {
    return {
      title: "练习已暂停",
      detail: status.takeoverReady
        ? "设备已经安全停下，可以接管或结束本次。"
        : "设备正在安全收尾，请稍候",
      tone: "warn",
    };
  }
  if (status.practiceState === "running") {
    return {
      title: "练习进行中",
      detail: "请留在老人旁边观察，有需要可随时暂停。",
      tone: "ok",
    };
  }
  if (status.practiceState === "starting" || status.practiceState === "pausing") {
    return {
      title: status.practiceState === "starting" ? "正在开始练习" : "正在安全暂停",
      detail: "请稍等，页面会自动更新。",
      tone: "info",
    };
  }
  if (status.patientPresence !== "online") {
    return {
      title: "等待老人画面",
      detail: "请在另一个窗口打开老人画面，连接后才能开始。",
      tone: "info",
    };
  }
  if (status.practiceState === "error") {
    return {
      title: "练习还没有安全开始",
      detail: "请保持页面打开，系统会继续确认状态。",
      tone: "danger",
    };
  }
  return {
    title: "已准备好",
    detail: "老人画面已连接，可以开始练习。",
    tone: "info",
  };
}

export function caregiverPatientPresenceLabel(presence: CaregiverPatientPresence): string {
  if (presence === "online") return "老人画面已打开";
  if (presence === "offline") return "老人画面还没打开";
  return "正在确认老人画面";
}

export function makeCaregiverEndRequest(
  selection: CaregiverEndSelection,
  status: CaregiverSessionStatus,
  idempotencyKey: string,
): CaregiverEndRequest {
  if (selection === "finish") return { kind: "finish" };
  return {
    kind: "abort",
    reasonCode: selection,
    expectedRevision: status.runtimeRevision,
    idempotencyKey,
  };
}
