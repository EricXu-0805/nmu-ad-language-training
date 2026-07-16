import { useCallback, useEffect, useState } from "react";
import { api, ApiError, PIN_UPDATED_EVENT } from "../api";
import { observeWseq } from "../sync/wseq";
import type { SessionRuntimeState } from "../types";

function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

export function useSessionRuntime(sessionId: string) {
  const [runtime, setRuntime] = useState<SessionRuntimeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api.getSessionRuntime(sessionId);
      observeWseq(next.cursor?.wseq);
      observeWseq(next.rapportStep?.wseq);
      setRuntime(next);
      setError(null);
      return next;
    } catch (e) {
      setError(messageOf(e));
      return null;
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void refresh();
    const onPinUpdated = () => { void refresh(); };
    window.addEventListener(PIN_UPDATED_EVENT, onPinUpdated);
    return () => window.removeEventListener(PIN_UPDATED_EVENT, onPinUpdated);
  }, [refresh]);

  const setPaused = useCallback(async (paused: boolean) => {
    if (busy) return null;
    setBusy(true);
    try {
      const next = paused ? await api.pauseSession(sessionId) : await api.resumeSession(sessionId);
      observeWseq(next.cursor?.wseq);
      observeWseq(next.rapportStep?.wseq);
      setRuntime(next);
      setError(null);
      return next;
    } catch (e) {
      setError(messageOf(e));
      return null;
    } finally {
      setBusy(false);
    }
  }, [busy, sessionId]);

  return {
    runtime,
    loading,
    busy,
    error,
    paused: runtime?.status === "paused",
    refresh,
    pause: () => setPaused(true),
    resume: () => setPaused(false),
  };
}
