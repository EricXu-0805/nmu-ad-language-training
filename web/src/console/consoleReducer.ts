// 操作端工作台状态机(单 useReducer)。三区:准备(登记受试者)/训练(选人一键开训)/分析(数据后台)。
// 显式导航,不用 URL 路由防误触前进后退丢状态(简单优先)。场次编号是后台概念,前端流程不呈现。
import type { Session } from "../types";

export type ConsoleArea = "prep" | "run" | "analyze";
// run 区内部小流程:选人 → 建/续场次 → 训练/关系建立 → 收尾。
export type RunScreen = "picker" | "sessionNew" | "training" | "relationship" | "wrapup";

export interface ConsoleState {
  area: ConsoleArea;
  screen: RunScreen;             // 仅 run 区有意义
  patientId: string | null;      // run 区选中的受试者
  session: Session | null;       // run 区当前场次(区切走不丢,切回原样)
}

export type ConsoleAction =
  | { t: "setArea"; area: ConsoleArea }
  | { t: "runPickSubject"; patientId: string }
  | { t: "goRunPicker" }
  | { t: "sessionStarted"; session: Session }
  | { t: "goWrapup" }
  | { t: "backToSession" }
  | { t: "resetRun" };

export const initialConsole: ConsoleState = { area: "run", screen: "picker", patientId: null, session: null };

const sessionScreen = (s: Session): RunScreen => (s.week_no === 1 ? "relationship" : "training");

export function consoleReducer(s: ConsoleState, a: ConsoleAction): ConsoleState {
  switch (a.t) {
    case "setArea":
      return { ...s, area: a.area };
    case "runPickSubject":
      // 选新受试者:必清残留场次——否则顶栏量表/异常抽屉(按 session/patientId 绑)会把新人的
      // 记录写到上一位受试者(跨受试者数据错配)。picker/sessionNew 语义=无当前场次。
      return { ...s, area: "run", patientId: a.patientId, session: null, screen: "sessionNew" };
    case "goRunPicker":
      // 回选人台=放下当前场次(fail-closed 退出/返回选人);彻底清人和场次,防旧场次残留。
      return { ...s, area: "run", screen: "picker", session: null, patientId: null };
    case "sessionStarted":
      // week1 走关系建立屏(plan 无评分题);week≥2 走训练判分屏。
      return { ...s, area: "run", session: a.session, screen: sessionScreen(a.session) };
    case "goWrapup":
      return { ...s, screen: "wrapup" };
    case "backToSession":
      // 收尾屏看到有漏锁 → 能回去补锁,不再是单行道
      return s.session ? { ...s, screen: sessionScreen(s.session) } : s;
    case "resetRun":
      // 收尾完/切人:清场次回到选人列表,area 留在 run
      return { ...s, session: null, patientId: null, screen: "picker" };
  }
}

// 工作台状态持久化:误刷新/误关标签页不再丢当前场次(作业日志本就在 localStorage,这里补屏幕位置)。
const PERSIST_KEY = "nmu:console:state";
const AREAS = ["prep", "run", "analyze"];
const RUN_SCREENS = ["picker", "sessionNew", "training", "relationship", "wrapup"];

export function loadConsoleState(): ConsoleState {
  try {
    const raw = localStorage.getItem(PERSIST_KEY);
    if (!raw) return initialConsole;
    const s = JSON.parse(raw) as ConsoleState;
    if (!s || typeof s !== "object" || !AREAS.includes(s.area) || !RUN_SCREENS.includes(s.screen)) return initialConsole;
    if ((s.screen === "training" || s.screen === "relationship" || s.screen === "wrapup") && !s.session) {
      return { ...initialConsole, area: s.area };
    }
    // picker/sessionNew 语义=无当前场次:持久化里若残留 session(旧格式/异常写入),一并丢弃,
    // 防重挂载后顶栏抽屉绑到已放下的场次。
    if ((s.screen === "picker" || s.screen === "sessionNew") && s.session) {
      return { ...s, session: null };
    }
    return s;
  } catch { return initialConsole; }
}

export function persistConsoleState(s: ConsoleState): void {
  try { localStorage.setItem(PERSIST_KEY, JSON.stringify(s)); } catch { /* 存不进也不阻塞操作 */ }
}
