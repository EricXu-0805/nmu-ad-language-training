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
  | { t: "reset" };

export const initialConsole: ConsoleState = { screen: "intake", patientId: null, session: null };

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
      return { ...s, session: a.session, screen: a.session.week_no === 1 ? "relationship" : "training" };
    case "goWrapup":
      return { ...s, screen: "wrapup" };
    case "reset":
      return initialConsole;
  }
}
