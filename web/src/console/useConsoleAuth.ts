import { useCallback, useEffect, useState } from "react";
import { api, PIN_REQUIRED_EVENT } from "../api";
import type { AuthConfig, AuthIdentity } from "../types";

export type ConsoleAuthMode = "loading" | "login" | "ok";

// console 认证门。三种后端形态自适应:
//   accounts_enabled → 走账号登录门(mode=login 直到登录成功);
//   仅 pin_enabled    → 不挡路,由 PinPrompt 处理(mode=ok);
//   全开(回环开发)   → 不挡路(mode=ok)。
// 会话中途过期(某请求 401 广播 PIN_REQUIRED_EVENT)→ 重新评估,账号模式退回登录门。
export function useConsoleAuth() {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [identity, setIdentity] = useState<AuthIdentity | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const cfg = await api.authConfig();
      setConfig(cfg);
      if (cfg.accounts_enabled) {
        try { setIdentity(await api.authMe()); } catch { setIdentity(null); }
      } else {
        setIdentity(null);
      }
    } catch {
      // 配置读不到(后端未起/离线):不挡路,按无账号处理——PIN/开放模式仍能走。
      setConfig(null); setIdentity(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const onReauth = () => { void refresh(); };
    window.addEventListener(PIN_REQUIRED_EVENT, onReauth);
    return () => window.removeEventListener(PIN_REQUIRED_EVENT, onReauth);
  }, [refresh]);

  const logout = useCallback(async () => {
    try { await api.authLogout(); } catch { /* 忽略:本地照样清 */ }
    setIdentity(null);
    await refresh();
  }, [refresh]);

  const accountsEnabled = !!config?.accounts_enabled;
  const mode: ConsoleAuthMode = !ready ? "loading" : (accountsEnabled && !identity) ? "login" : "ok";
  return { mode, identity, accountsEnabled, refresh, logout };
}
