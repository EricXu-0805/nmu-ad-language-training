// 受试者端在场状态的纯展示推导:hook 只管轮询,这里只管把事实翻成不说谎的话。
export type PatientPresenceState =
  "checking" | "online" | "offline" | "unseen" | "unsupported" | "unavailable";

export interface PatientPresenceView {
  state: PatientPresenceState;
  screenLabel: string;
  lastSeenLabel: string | null;
  /** 原始 screen 事实:只有在线态才断言(离线后屏上是什么没人知道)。 */
  screen: string | null;
}

export interface PresencePayload {
  session_id?: string;
  screen?: string;
  last_seen_at?: string | null;
  online?: boolean;
}

const SCREEN_LABELS: Record<string, string> = {
  waiting: "等待开始",
  loading: "正在准备题目",
  rapport: "关系建立中",
  present: "正在看题",
  record: "正在回答",
  thanks: "正在看结束提示",
  paused: "已显示暂停提示",
  complete: "已完成",
  error: "等待研究者处理",
};

export function relativeSeen(value: string | null | undefined, now = Date.now()): string | null {
  if (!value) return null;
  const ms = now - new Date(value).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "刚刚有响应";
  if (ms < 12_000) return "刚刚有响应";
  if (ms < 60_000) return `约 ${Math.max(1, Math.round(ms / 1000))} 秒前有响应`;
  return `约 ${Math.max(1, Math.round(ms / 60_000))} 分钟前有响应`;
}

export function presenceViewFrom(input: {
  sessionId: string | null | undefined;
  checking: boolean;
  unsupported: boolean;
  unavailable: boolean;
  presence: PresencePayload | null;
  now?: number;
}): PatientPresenceView {
  const { sessionId, checking, unsupported, unavailable, presence } = input;
  const now = input.now ?? Date.now();
  if (!sessionId) return { state: "unseen", screenLabel: "尚未建立场次", lastSeenLabel: null, screen: null };
  if (checking) return { state: "checking", screenLabel: "正在确认受试者端", lastSeenLabel: null, screen: null };
  if (unsupported) return { state: "unsupported", screenLabel: "该版本不支持受试者端在线状态", lastSeenLabel: null, screen: null };
  if (unavailable) return { state: "unavailable", screenLabel: "暂时无法读取双端状态", lastSeenLabel: relativeSeen(presence?.last_seen_at, now), screen: null };
  if (!presence) return { state: "unseen", screenLabel: "等待受试者端打开本场次", lastSeenLabel: null, screen: null };
  if (presence.online !== true) {
    // 设备断开后没人知道屏上是什么:绝不再断言「正在看题/正在看结束提示」。
    return {
      state: "offline",
      screenLabel: "已断开，画面未知",
      lastSeenLabel: relativeSeen(presence.last_seen_at, now),
      screen: null,
    };
  }
  return {
    state: "online",
    screenLabel: SCREEN_LABELS[presence.screen ?? ""] ?? "受试者端已打开",
    lastSeenLabel: relativeSeen(presence.last_seen_at, now),
    screen: presence.screen ?? null,
  };
}
