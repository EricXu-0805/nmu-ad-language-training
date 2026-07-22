const CONSOLE_STATE_EXACT_KEY = "nmu:console:state";
const CONSOLE_STATE_PREFIX = "nmu:console:state:";

const SENSITIVE_EXACT_KEYS = new Set([
  "nmu:pin",
  "nmu:device-capability:v1",
  "nmu:device-recovery-capabilities:v1",
  "nmu:device-id:v1",
  CONSOLE_STATE_EXACT_KEY,
  "nmu:tts:log",
  "nmu:session",
  "nmu:cursor",
  "nmu:rapport",
]);

const SENSITIVE_KEY_PREFIXES = [
  "nmu:journal:",
  "nmu:profile:",
  // Console persistence is account-scoped.  Every current and legacy scoped
  // key can contain the selected patient plus the complete session snapshot,
  // so logout must clear the whole namespace rather than only the old
  // unscoped key above.
  CONSOLE_STATE_PREFIX,
  "nmu:console:apFailure:",
];

export interface StorageLike {
  readonly length: number;
  key(index: number): string | null;
  removeItem(key: string): void;
}

export function isSensitiveResearchStorageKey(key: string): boolean {
  return SENSITIVE_EXACT_KEYS.has(key)
    || SENSITIVE_KEY_PREFIXES.some((prefix) => key.startsWith(prefix));
}

export function clearSensitiveResearchState(storage: StorageLike): number {
  const keys: string[] = [];
  try {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key && isSensitiveResearchStorageKey(key)) keys.push(key);
    }
  } catch {
    return 0;
  }
  let cleared = 0;
  for (const key of keys) {
    try { storage.removeItem(key); cleared += 1; } catch { /* continue clearing other keys */ }
  }
  return cleared;
}

export function clearConsoleWorkspaceState(
  storage: StorageLike = localStorage,
): number {
  const keys: string[] = [];
  try {
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key === CONSOLE_STATE_EXACT_KEY || key?.startsWith(CONSOLE_STATE_PREFIX)) {
        keys.push(key);
      }
    }
  } catch {
    return 0;
  }
  let cleared = 0;
  for (const key of keys) {
    try { storage.removeItem(key); cleared += 1; } catch { /* continue clearing other scopes */ }
  }
  return cleared;
}

export function clearResearchBrowserState(
  local: StorageLike = localStorage,
  session: StorageLike = sessionStorage,
): number {
  // IndexedDB 音频恢复库刻意不在此函数作用域。未取得服务端上传回执的录音
  // 必须继续保留；退出只清 Web Storage 中的身份、游标、日志与文本缓存。
  return clearSensitiveResearchState(local) + clearSensitiveResearchState(session);
}

export function journalForLocalStorage<T extends {
  turns: Record<string, { asrText?: string; confirmedText?: string }>;
}>(journal: T): T {
  const turns = Object.fromEntries(Object.entries(journal.turns).map(([key, turn]) => {
    const { asrText: _asrText, confirmedText: _confirmedText, ...safe } = turn;
    return [key, safe];
  }));
  return { ...journal, turns } as T;
}
