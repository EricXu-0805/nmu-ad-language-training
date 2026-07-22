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
      historyDescription: "按受试者查看已最终完成的场次。进入后训练与研究真值保持只读，导出仍须通过服务器完整性门禁。",
      historyOpenLabel: "查看已完成场次",
      historyCloseLabel: "收起已完成场次",
    };
  }
  return {
    completedOnly: false,
    kicker: "工作台 · 今日已审核安排",
    title: "到场后的今日工作",
    description: "正式评估待办与每周训练分区展示。先核对未收尾或进行中的评估；新训练仍只能从已审核且日期已到的安排一键开始。",
    prepActionLabel: input.canStart ? "去准备区安排" : "查看受试者档案",
    readOnlyTitle: "当前账号为只读角色",
    readOnlyDescription: "可以核对今日安排与历史场次，但不能启动或续做训练。",
    historyTitle: "异常恢复与未完复核",
    historyDescription: "用于已经启动但因刷新、断网或设备中断未能继续的场次，以及干预结束后尚未完成的研究复核。新训练必须从上方已审核安排开始。",
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
