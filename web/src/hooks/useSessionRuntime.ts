import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { observeWseq } from "../sync/wseq";
import type { SessionRuntimeState } from "../types";
import { isSessionTerminalStatus } from "../sessionLifecycle";

const RUNTIME_POLL_MS = 2_000;
function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

export function useSessionRuntime(sessionId: string) {
  const [runtime, setRuntime] = useState<SessionRuntimeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const inFlight = useRef<{ sessionId: string; promise: Promise<SessionRuntimeState | null> } | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const fetchRuntime = useCallback((foreground: boolean): Promise<SessionRuntimeState | null> => {
    const pending = inFlight.current;
    if (pending?.sessionId === sessionId) return pending.promise;
    if (foreground) setLoading(true);
    const request = api.getSessionRuntime(sessionId)
      .then((next) => {
        if (!mounted.current || sessionIdRef.current !== sessionId) return null;
        observeWseq(next.cursor?.wseq);
        observeWseq(next.rapportStep?.wseq);
        // 暂停/完成等写请求可能与轮询 GET 交错；旧 revision 不得把新终态覆盖回 active。
        setRuntime((previous) => previous && previous.sessionId === next.sessionId && previous.revision >= next.revision
          ? previous
          : next);
        setError(null);
        return next;
      })
      .catch((cause: unknown) => {
        if (mounted.current && sessionIdRef.current === sessionId) setError(messageOf(cause));
        return null;
      })
      .finally(() => {
        if (inFlight.current?.promise === request) inFlight.current = null;
        if (foreground && mounted.current && sessionIdRef.current === sessionId) setLoading(false);
      });
    inFlight.current = { sessionId, promise: request };
    return request;
  }, [sessionId]);

  const refresh = useCallback(() => fetchRuntime(true), [fetchRuntime]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void fetchRuntime(false); }, RUNTIME_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [fetchRuntime, refresh]);

  const setPaused = useCallback(async (paused: boolean) => {
    if (busy) return null;
    if (runtime && isSessionTerminalStatus(runtime.status)) {
      setError(`场次已进入终态 ${runtime.status}，只能查看，不能再暂停或恢复`);
      return null;
    }
    setBusy(true);
    try {
      const next = paused ? await api.pauseSession(sessionId) : await api.resumeSession(sessionId);
      observeWseq(next.cursor?.wseq);
      observeWseq(next.rapportStep?.wseq);
      setRuntime((previous) => previous && previous.sessionId === next.sessionId && previous.revision >= next.revision
        ? previous
        : next);
      setError(null);
      return next;
    } catch (e) {
      setError(messageOf(e));
      return null;
    } finally {
      setBusy(false);
    }
  }, [busy, runtime, sessionId]);

  const currentRuntime = runtime?.sessionId === sessionId ? runtime : null;
  const terminalStatus = currentRuntime && isSessionTerminalStatus(currentRuntime.status) ? currentRuntime.status : null;

  return {
    runtime,
    loading,
    busy,
    error,
    paused: currentRuntime?.status === "paused",
    terminal: terminalStatus !== null,
    terminalStatus,
    refresh,
    pause: () => setPaused(true),
    resume: () => setPaused(false),
  };
}
