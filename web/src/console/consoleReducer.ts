// 操作端工作台状态机(单 useReducer)。三区:准备(登记受试者)/训练(选人一键开训)/分析(数据后台)。
// 显式导航,不用 URL 路由防误触前进后退丢状态(简单优先)。场次编号是后台概念,前端流程不呈现。
import type { AuthIdentity, Session } from "../types";
import { isSessionTerminalStatus } from "../sessionLifecycle.ts";
import { parsePatientSessionList } from "../visitPlans.ts";
import { bedsideProtocolRoute } from "./protocolAdmission.ts";

export type ConsoleArea = "prep" | "run" | "analyze";
// run 区只消费测试前已审核安排；床旁不再填周次/阶段/场次号。
export type RunScreen = "picker" | "training" | "relationship" | "unsupported" | "wrapup";

export interface ConsoleState {
  area: ConsoleArea;
  screen: RunScreen;             // 仅 run 区有意义
  patientId: string | null;      // run 区选中的受试者
  session: Session | null;       // run 区当前场次(区切走不丢,切回原样)
}

export type ConsoleAction =
  | { t: "setArea"; area: ConsoleArea }
  | { t: "goRunPicker" }
  | { t: "sessionStarted"; session: Session }
  | { t: "sessionRuntimeUpdated"; status: NonNullable<Session["runtime_status"]> }
  | { t: "goWrapup" }
  | { t: "backToSession" }
  | { t: "resetRun" };

export const initialConsole: ConsoleState = { area: "run", screen: "picker", patientId: null, session: null };

const sessionScreen = (s: Session): RunScreen =>
  isSessionTerminalStatus(s.runtime_status)
    ? "wrapup"
    : bedsideProtocolRoute(s).screen;

export function consoleReducer(s: ConsoleState, a: ConsoleAction): ConsoleState {
  switch (a.t) {
    case "setArea":
      return { ...s, area: a.area };
    case "goRunPicker":
      // 回选人台=放下当前场次(fail-closed 退出/返回选人);彻底清人和场次,防旧场次残留。
      return { ...s, area: "run", screen: "picker", session: null, patientId: null };
    case "sessionStarted":
      // 干预完成/最终完成/中止/失败均不重挂训练驾驶员。
      return { ...s, area: "run", session: a.session, screen: sessionScreen(a.session) };
    case "sessionRuntimeUpdated":
      return s.session ? { ...s, session: { ...s.session, runtime_status: a.status } } : s;
    case "goWrapup":
      return { ...s, screen: "wrapup" };
    case "backToSession":
      // 只有床旁 active/paused 可回现场；任何终态绝不返回训练。
      return s.session && !isSessionTerminalStatus(s.session.runtime_status)
        ? { ...s, screen: sessionScreen(s.session) }
        : s;
    case "resetRun":
      // 收尾完/切人:清场次回到选人列表,area 留在 run
      return { ...s, session: null, patientId: null, screen: "picker" };
  }
}

// 工作台状态持久化:误刷新/误关标签页不再丢当前场次(作业日志本就在 localStorage,这里补屏幕位置)。
const LEGACY_PERSIST_KEY = "nmu:console:state";
const PERSIST_PREFIX = "nmu:console:state:";
const CACHE_VERSION = 2;
const AREAS = new Set<ConsoleArea>(["prep", "run", "analyze"]);
const RUN_SCREENS = new Set<RunScreen>(["picker", "training", "relationship", "unsupported", "wrapup"]);
const STATE_KEYS = ["area", "screen", "patientId", "session"] as const;
const ENVELOPE_KEYS = ["version", "identityScope", "state"] as const;
const SAFE_ACCOUNT = /^[^\p{Cc}\p{Cf}]{1,128}$/u;

type ConsoleIdentityScope = Pick<AuthIdentity, "username"> | null;
type UnknownRecord = Record<string, unknown>;

interface PersistedConsoleEnvelope {
  version: typeof CACHE_VERSION;
  identityScope: string;
  state: ConsoleState;
}

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function exactKeys(value: UnknownRecord, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length
    && keys.every((key) => Object.hasOwn(value, key));
}

function identityScopeOf(identity: ConsoleIdentityScope): string | null {
  if (identity === null) return "local:M0";
  const username = (identity as { username?: unknown }).username;
  if (typeof username !== "string" || username.trim() !== username
      || !SAFE_ACCOUNT.test(username)) return null;
  return `account:${username}`;
}

function persistenceKey(scope: string): string {
  return `${PERSIST_PREFIX}${encodeURIComponent(scope)}`;
}

function legacyScopedKey(scope: string): string {
  const oldScope = scope === "local:M0" ? "LOCAL-M0" : scope.slice("account:".length);
  return `${PERSIST_PREFIX}${encodeURIComponent(oldScope)}`;
}

function parsePersistedState(value: unknown): ConsoleState {
  const row = record(value);
  if (!row || !exactKeys(row, STATE_KEYS)
      || typeof row.area !== "string" || !AREAS.has(row.area as ConsoleArea)
      || typeof row.screen !== "string" || !RUN_SCREENS.has(row.screen as RunScreen)
      || !(row.patientId === null || typeof row.patientId === "string")) {
    throw new Error("工作台缓存状态不符合严格契约");
  }
  const area = row.area as ConsoleArea;
  const screen = row.screen as RunScreen;
  if (row.session === null) {
    if (screen !== "picker" || row.patientId !== null) {
      throw new Error("工作台缓存缺少可绑定的权威场次");
    }
    return { area, screen, patientId: null, session: null };
  }
  if (screen === "picker") throw new Error("选人台缓存不得携带旧场次");

  const candidate = record(row.session);
  const candidatePatientId = candidate?.patient_id;
  if (typeof candidatePatientId !== "string") {
    throw new Error("工作台缓存场次未绑定受试者");
  }
  const session = parsePatientSessionList([row.session], candidatePatientId)[0];
  if (!session || (row.patientId !== null && row.patientId !== session.patient_id)) {
    throw new Error("工作台缓存的受试者与场次不一致");
  }
  const terminal = isSessionTerminalStatus(session.runtime_status);
  const expectedScreen = terminal ? "wrapup" : bedsideProtocolRoute(session).screen;
  if (screen !== expectedScreen) {
    throw new Error("工作台缓存的场次状态与页面矛盾");
  }
  return {
    area,
    screen,
    patientId: row.patientId as string | null,
    session,
  };
}

function parseEnvelope(value: unknown, expectedScope: string): ConsoleState {
  const row = record(value);
  if (!row || !exactKeys(row, ENVELOPE_KEYS)
      || row.version !== CACHE_VERSION || row.identityScope !== expectedScope) {
    throw new Error("工作台缓存未绑定当前账号或版本已失效");
  }
  return parsePersistedState(row.state);
}

function envelopeForState(s: ConsoleState, scope: string): PersistedConsoleEnvelope {
  const normalized: ConsoleState = {
    area: s.area,
    screen: s.screen,
    patientId: s.patientId,
    session: s.session
      ? { ...s.session, runtime_status: s.session.runtime_status ?? "active" }
      : null,
  };
  return {
    version: CACHE_VERSION,
    identityScope: scope,
    state: parsePersistedState(normalized),
  };
}

export function loadConsoleState(identity: ConsoleIdentityScope = null): ConsoleState {
  const scope = identityScopeOf(identity);
  try {
    // 旧键没有账号命名空间，可能属于上一个登录者；升级后只删除，绝不迁移或重挂。
    localStorage.removeItem(LEGACY_PERSIST_KEY);
    if (scope === null) return initialConsole;
    localStorage.removeItem(legacyScopedKey(scope));
    const key = persistenceKey(scope);
    const raw = localStorage.getItem(key);
    if (!raw) return initialConsole;
    try {
      return parseEnvelope(JSON.parse(raw) as unknown, scope);
    } catch {
      // 缓存只是快路径；损坏、旧版或越账号数据一律清除，
      // 绝不在床旁界面上尝试容错恢复。
      localStorage.removeItem(key);
      return initialConsole;
    }
  } catch { return initialConsole; }
}

export function persistConsoleState(s: ConsoleState, identity: ConsoleIdentityScope = null): void {
  const scope = identityScopeOf(identity);
  if (scope === null) return;
  const key = persistenceKey(scope);
  try {
    localStorage.setItem(key, JSON.stringify(envelopeForState(s, scope)));
  } catch {
    // 不可序列化的内部状态也不得留下旧缓存供下次误挂。
    try { localStorage.removeItem(key); } catch { /* 存储不可用时不阻塞当前服务端流程 */ }
  }
}

export function clearConsoleState(identity: ConsoleIdentityScope = null): void {
  try {
    localStorage.removeItem(LEGACY_PERSIST_KEY);
    const scope = identityScopeOf(identity);
    if (scope !== null) {
      localStorage.removeItem(legacyScopedKey(scope));
      localStorage.removeItem(persistenceKey(scope));
    }
  } catch { /* 清理失败不伪装成服务端已登出；全局敏感状态清理还会再次兜底 */ }
}
