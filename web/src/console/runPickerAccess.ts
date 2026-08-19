import type { Session } from "../types";

export interface RunPickerPresentation {
  completedOnly: boolean;
  kicker: string;
  title: string;
  description: string;
  prepActionLabel: string;
  readOnlyTitle: string;
  readOnlyDescription: string;
  historyTitle: string;
  historyDescription: string;
  historyOpenLabel: string;
  historyCloseLabel: string;
}

export function runPickerPresentation(input: {
  canStart: boolean;
  canViewCompleted: boolean;
}): RunPickerPresentation {
  const completedOnly = !input.canStart && input.canViewCompleted;
  if (completedOnly) {
    return {
      completedOnly: true,
      kicker: "数据后台 · 已完成场次",
      title: "查看已完成场次与受控导出",
      description: "这里只进入已经最终完成的场次，用于只读核查、去标识导出与录音保全；不会启动或续做床旁训练。",
      prepActionLabel: "查看受试者档案",
      readOnlyTitle: "当前账号用于数据查看与治理",
      readOnlyDescription: "不能启动、续做或复核训练；下方“已完成场次与受控导出”已自动展开。",
      historyTitle: "已完成场次与受控导出",
      historyDescription: "按受试者查看已最终完成的场次，进入后只能查看，不能修改。",
      historyOpenLabel: "查看已完成场次",
      historyCloseLabel: "收起已完成场次",
    };
  }
  return {
    completedOnly: false,
    kicker: "工作台 · 今日已审核安排",
    title: "今日训练",
    description: "先处理待办评估；训练从下方今日队列一键开始。",
    prepActionLabel: input.canStart ? "去准备区安排" : "查看受试者档案",
    readOnlyTitle: "当前账号为只读角色",
    readOnlyDescription: "可以核对今日安排与历史场次，但不能启动或续做训练。",
    historyTitle: "异常恢复与未完复核",
    historyDescription: "训练中断后从这里继续，也可找到待复核的场次。",
    historyOpenLabel: "打开恢复入口",
    historyCloseLabel: "收起恢复入口",
  };
}

export function sessionsVisibleForRunPicker(
  sessions: readonly Session[],
  completedOnly: boolean,
): Session[] {
  return completedOnly
    ? sessions.filter((session) => session.runtime_status === "completed")
    : [...sessions];
}
