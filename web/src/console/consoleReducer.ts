// 操作端会话状态机(单 useReducer)。显式导航,不用 URL 路由防误触前进后退丢状态(简单优先)。
import type { Session } from "../types";

export type ConsoleScreen = "intake" | "sessionNew" | "training" | "relationship" | "wrapup";

export interface ConsoleState {
  screen: ConsoleScreen;
  patientId: string | null;
  session: Session | null;
}

export type ConsoleAction =
  | { t: "goIntake" }
  | { t: "patientReady"; patientId: string }
  | { t: "goSessionNew"; patientId?: string }
  | { t: "sessionStarted"; session: Session }
  | { t: "goWrapup" }
  | { t: "backToSession" }
  | { t: "reset" };

export const initialConsole: ConsoleState = { screen: "intake", patientId: null, session: null };

const sessionScreen = (s: Session): ConsoleScreen => (s.week_no === 1 ? "relationship" : "training");

export function consoleReducer(s: ConsoleState, a: ConsoleAction): ConsoleState {
  switch (a.t) {
    case "goIntake":
      return { ...s, screen: "intake" };
    case "patientReady":
      return { ...s, patientId: a.patientId, screen: "sessionNew" };
    case "goSessionNew":
      return { ...s, patientId: a.patientId ?? s.patientId, screen: "sessionNew" };
    case "sessionStarted":
      // week1 走关系建立屏(plan 无评分题);week≥2 走训练判分屏。
      return { ...s, session: a.session, screen: sessionScreen(a.session) };
    case "goWrapup":
      return { ...s, screen: "wrapup" };
    case "backToSession":
      // 收尾屏看到有漏锁 → 能回去补锁,不再是单行道
      return s.session ? { ...s, screen: sessionScreen(s.session) } : s;
    case "reset":
      return initialConsole;
  }
}

// 会话状态持久化:误刷新/误关标签页不再丢当前场次(作业日志本就在 localStorage,这里补屏幕位置)。
const PERSIST_KEY = "nmu:console:state";

export function loadConsoleState(): ConsoleState {
  try {
    const raw = localStorage.getItem(PERSIST_KEY);
    if (!raw) return initialConsole;
    const s = JSON.parse(raw) as ConsoleState;
    if (!s || typeof s !== "object" || !["intake", "sessionNew", "training", "relationship", "wrapup"].includes(s.screen)) return initialConsole;
    if ((s.screen === "training" || s.screen === "relationship" || s.screen === "wrapup") && !s.session) return initialConsole;
    return s;
  } catch { return initialConsole; }
}

export function persistConsoleState(s: ConsoleState): void {
  try { localStorage.setItem(PERSIST_KEY, JSON.stringify(s)); } catch { /* 存不进也不阻塞操作 */ }
}
