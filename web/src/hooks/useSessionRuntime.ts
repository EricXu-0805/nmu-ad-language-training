import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { observeWseq } from "../sync/wseq";
import type { SessionRuntimeState } from "../types";
import { isSessionTerminalStatus } from "../sessionLifecycle";
import { pollFailed, pollSucceeded, type PollHealth } from "./runtimePollPolicy.ts";
import { ratchetRevisionFloor, type RuntimeRevisionFloor } from "./runtimeRevisionFloor.ts";

const RUNTIME_POLL_MS = 1_000;
function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

// 暂停/恢复这类动作的结果单独返回,不写进轮询错误槽:那个槽每一拍成功轮询都会
// 清空(拒因一闪即逝),消费方还会把它当「恢复失败」锁死整个人工面板。
export type RuntimeActionResult =
  | { ok: true; runtime: SessionRuntimeState }
  | { ok: false; message: string };

export function useSessionRuntime(sessionId: string) {
  const [runtime, setRuntime] = useState<SessionRuntimeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const inFlight = useRef<{ sessionId: string; promise: Promise<SessionRuntimeState | null> } | null>(null);
  // 当前 session 已证实过的单调最高 revision。在每一次成功回执的 Promise
  // continuation 内、setRuntime 之前同步棘轮——不依赖下一次 React 渲染/effect
  // 才能看见，poll、refresh、pause/resume 都走同一份；外部（重同步的直读）也
  // 通过 reportRevision 汇入同一个下限，读写共用一个 per-session 单调事实。
  const revisionFloorRef = useRef<RuntimeRevisionFloor>({ sessionId, revision: 0 });
  const pollHealth = useRef<PollHealth>(pollSucceeded(Date.now()));
  if (revisionFloorRef.current.sessionId !== sessionId) {
    revisionFloorRef.current = { sessionId, revision: 0 };
    pollHealth.current = pollSucceeded(Date.now());
  }
  const reportRevision = useCallback((candidateSessionId: string, revision: number) => {
    revisionFloorRef.current = ratchetRevisionFloor(revisionFloorRef.current, candidateSessionId, revision);
  }, []);

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
        // 棘轮必须在这个 continuation 内、setRuntime 之前同步发生——调用方
        // （例如重同步事务）读下限时不能等下一次 React 渲染才看得到这次回执。
        reportRevision(next.sessionId, next.revision);
        // 暂停/完成等写请求可能与轮询 GET 交错；旧 revision 不得把新终态覆盖回 active。
        setRuntime((previous) => previous && previous.sessionId === next.sessionId && previous.revision >= next.revision
          ? previous
          : next);
        pollHealth.current = pollSucceeded(Date.now());
        setError(null);
        return next;
      })
      .catch((cause: unknown) => {
        if (mounted.current && sessionIdRef.current === sessionId) {
          // 单次背景丢包不亮错:错误一旦呈现,消费方会锁死人工面板并向老人端
          // 撤游标(正在进行的自助录音会被掐断)。距上次成功超过宽限窗才呈现。
          if (pollFailed(pollHealth.current, Date.now(), foreground)) {
            setError(messageOf(cause));
          }
        }
        return null;
      })
      .finally(() => {
        if (inFlight.current?.promise === request) inFlight.current = null;
        if (foreground && mounted.current && sessionIdRef.current === sessionId) setLoading(false);
      });
    inFlight.current = { sessionId, promise: request };
    return request;
  }, [sessionId, reportRevision]);

  const refresh = useCallback(() => fetchRuntime(true), [fetchRuntime]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void fetchRuntime(false); }, RUNTIME_POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [fetchRuntime, refresh]);

  const setPaused = useCallback(async (paused: boolean): Promise<RuntimeActionResult | null> => {
    if (busy) return null;
    if (runtime && isSessionTerminalStatus(runtime.status)) {
      const statusLabel = runtime.status === "completed" ? "已完成"
        : runtime.status === "aborted" ? "已中止"
        : runtime.status === "intervention_completed" ? "现场训练已结束"
        : runtime.status === "failed" ? "已失败"
        : runtime.status;
      return { ok: false, message: `本场训练${statusLabel}，只能查看，不能再暂停或恢复` };
    }
    setBusy(true);
    try {
      const next = paused ? await api.pauseSession(sessionId) : await api.resumeSession(sessionId);
      observeWseq(next.cursor?.wseq);
      observeWseq(next.rapportStep?.wseq);
      reportRevision(next.sessionId, next.revision);
      setRuntime((previous) => previous && previous.sessionId === next.sessionId && previous.revision >= next.revision
        ? previous
        : next);
      setError(null);
      return { ok: true, runtime: next };
    } catch (e) {
      return { ok: false, message: messageOf(e) };
    } finally {
      setBusy(false);
    }
  }, [busy, runtime, sessionId, reportRevision]);

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
    // 背景轮询与重同步的直读共用同一个 per-session 单调下限：getRevisionFloor
    // 同步读 ref，不等任何一次渲染；reportRevision 让外部读数也棘轮进同一份。
    getRevisionFloor: () => (revisionFloorRef.current.sessionId === sessionId ? revisionFloorRef.current.revision : 0),
    reportRevision,
  };
}
