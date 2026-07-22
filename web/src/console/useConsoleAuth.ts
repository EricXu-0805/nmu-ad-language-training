import { useCallback, useEffect, useRef, useState } from "react";
import { ACCOUNT_REAUTH_REQUIRED_EVENT, api, ApiError } from "../api";
import { clearResearchBrowserState } from "../security/localSensitiveState";
import type { AuthConfig, AuthIdentity } from "../types";
import {
  handleConfirmedAccountAuthenticationLoss,
  isUnauthenticatedResponse,
  parseAuthConfig,
  parseAuthIdentity,
  type ConsoleAuthMode,
} from "./authPolicy";

interface ConsoleAuthState {
  mode: ConsoleAuthMode;
  config: AuthConfig | null;
  identity: AuthIdentity | null;
  failure: string | null;
}

const INITIAL_AUTH_STATE: ConsoleAuthState = {
  mode: "loading",
  config: null,
  identity: null,
  failure: null,
};

function authFailureMessage(stage: string, error: unknown): string {
  if (error instanceof ApiError) return `${stage}：${error.detail}`;
  if (error instanceof Error && error.message.trim()) return `${stage}：${error.message}`;
  return `${stage}，请检查服务连接后重试`;
}

// console 认证门：只有 config 成功解析，且账号模式下 auth/me 明确成功时才返回 ok。
// auth/me 仅 401 可解释为“需要登录”；网络、超时、代理 HTML、畸形 JSON 均进入 error。
export function useConsoleAuth() {
  const [state, setState] = useState<ConsoleAuthState>(INITIAL_AUTH_STATE);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const attempt = ++generation.current;
    setState(INITIAL_AUTH_STATE);
    try {
      const config = parseAuthConfig(await api.authConfig() as unknown);
      if (!config.accounts_enabled) {
        if (attempt === generation.current) {
          setState({ mode: "ok", config, identity: null, failure: null });
        }
        return;
      }

      try {
        const identity = parseAuthIdentity(await api.authMe() as unknown);
        if (attempt === generation.current) {
          setState({ mode: "ok", config, identity, failure: null });
        }
      } catch (error) {
        if (attempt !== generation.current) return;
        if (isUnauthenticatedResponse(error)) {
          handleConfirmedAccountAuthenticationLoss(() => {
            setState({ mode: "login", config, identity: null, failure: null });
          });
        } else {
          setState({
            mode: "error",
            config: null,
            identity: null,
            failure: authFailureMessage("研究者身份检查失败", error),
          });
        }
      }
    } catch (error) {
      if (attempt === generation.current) {
        setState({
          mode: "error",
          config: null,
          identity: null,
          failure: authFailureMessage("认证配置检查失败", error),
        });
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => { generation.current += 1; };
  }, [refresh]);

  const accountsEnabled = state.config?.accounts_enabled === true;
  useEffect(() => {
    if (!accountsEnabled) return;
    const onReauth = () => {
      handleConfirmedAccountAuthenticationLoss(() => { void refresh(); });
    };
    window.addEventListener(ACCOUNT_REAUTH_REQUIRED_EVENT, onReauth);
    return () => window.removeEventListener(ACCOUNT_REAUTH_REQUIRED_EVENT, onReauth);
  }, [accountsEnabled, refresh]);

  const logout = useCallback(async () => {
    const config = state.config;
    // 先撤下工作台并清本地敏感状态，再尝试服务器登出。清理函数只接触 Web Storage，
    // 不会打开或删除 IndexedDB 中尚未证实上传的音频恢复副本。
    const logoutAttempt = ++generation.current;
    setState(INITIAL_AUTH_STATE);
    clearResearchBrowserState();
    try {
      await api.authLogout();
      if (logoutAttempt === generation.current && config?.accounts_enabled) {
        setState({ mode: "login", config, identity: null, failure: null });
      }
    } catch (error) {
      if (logoutAttempt === generation.current) {
        setState({
          mode: "error",
          config: null,
          identity: null,
          failure: authFailureMessage("服务器未确认退出", error),
        });
      }
    }
  }, [state.config]);

  return {
    mode: state.mode,
    identity: state.identity,
    accountsEnabled,
    failure: state.failure,
    refresh,
    logout,
  };
}
