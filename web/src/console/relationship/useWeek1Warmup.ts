import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import type { SessionRuntimeState } from "../../types";
import { bus } from "../../sync/bus";
import { observeWseq } from "../../sync/wseq";
import { flushLiveWrites } from "../../sync/useCursorWriter";
import type { WarmupReceipt } from "../../sync/warmupRequest";
import { warmupTargetIdentity, type WarmupAction, type WarmupRequest, type WarmupTopic, type Week1WarmupContent } from "../../week1Warmup";

export function useWeek1Warmup(sessionId: string, runtime: SessionRuntimeState | null) {
  const [content, setContent] = useState<Week1WarmupContent | null>(null);
  const [contentError, setContentError] = useState<string | null>(null);
  const [contentRetry, setContentRetry] = useState(0);
  const [receipt, setReceipt] = useState<WarmupReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const pending = useRef<WarmupRequest | null>(null);
  const busyRef = useRef(false);
  const epoch = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    epoch.current += 1;
    pending.current = null;
    busyRef.current = false;
    setReceipt(null);
    setBusy(false);
    setProblem(null);
    setUncertain(false);
    return () => { mounted.current = false; epoch.current += 1; };
  }, [sessionId]);
  useEffect(() => {
    let disposed = false;
    setContentError(null);
    api.week1WarmupContent().then((value) => { if (!disposed) setContent(value); })
      .catch(() => { if (!disposed) setContentError("互动内容未能加载，请重试；正式关系建立仍可使用。"); });
    return () => { disposed = true; };
  }, [contentRetry]);
  const runtimeStep = runtime?.sessionId === sessionId ? runtime.rapportStep : null;
  const receiptIsNewer = Boolean(receipt && receipt.rapportStep.sessionId === sessionId
    && receipt.wseq > (runtimeStep?.wseq ?? 0));
  const step = receiptIsNewer ? receipt!.rapportStep : runtimeStep;
  const displayedRevision = receiptIsNewer ? receipt!.runtimeRevision : runtime?.revision ?? 0;
  const warmup = step?.warmup;
  const blocking = Boolean(warmup) || busy || uncertain;
  const cancel = () => {
    epoch.current += 1;
    pending.current = null;
    busyRef.current = false;
    setBusy(false);
    setUncertain(false);
    setProblem(null);
  };
  const execute = async (request: WarmupRequest, generation: number): Promise<WarmupReceipt | null> => {
    try {
      const next = await api.week1Warmup(sessionId, request);
      if (!mounted.current || epoch.current !== generation) return null;
      observeWseq(next.wseq);
      setReceipt(next);
      pending.current = null;
      setUncertain(false);
      setProblem(null);
      // The same-machine path receives only the exact validated server receipt.
      try { bus.post(next.rapportStep); } catch { /* server polling remains authoritative if browser storage is unavailable */ }
      return next;
    } catch (error) {
      if (!mounted.current || epoch.current !== generation) return null;
      const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408;
      if (rejected) pending.current = null;
      setUncertain(!rejected);
      setProblem(rejected ? (error instanceof ApiError ? error.detail : "状态已变化，请重新核对后操作")
        : "操作结果尚未确认。请点“核对并重试刚才的操作”，不要重复选择主题。");
      return null;
    } finally {
      if (mounted.current && epoch.current === generation) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };
  const act = async (action: WarmupAction, topic: WarmupTopic | undefined, before: () => void): Promise<WarmupReceipt | null> => {
    if (busyRef.current || pending.current) return null;
    const expectedWseq = step?.wseq ?? 0;
    const expectedRevision = displayedRevision;
    const expectedTarget = warmupTargetIdentity(sessionId, expectedRevision, step);
    busyRef.current = true;
    setBusy(true);
    setProblem(null);
    before(); // invalidate all older automatic callbacks before any await
    const generation = ++epoch.current;
    try {
      await flushLiveWrites();
      if (!mounted.current || generation !== epoch.current) return null;
      const currentRuntime = await api.getSessionRuntime(sessionId);
      if (!mounted.current || generation !== epoch.current) return null;
      if (currentRuntime.sessionId !== sessionId || currentRuntime.status !== "active") {
        throw new Error("场次已暂停或结束，请先核对场次状态");
      }
      if (warmupTargetIdentity(currentRuntime.sessionId, currentRuntime.revision, currentRuntime.rapportStep) !== expectedTarget) {
        throw new Error("互动位置已变化，已取消刚才的操作。请核对刷新后的内容，再重新选择。");
      }
      const request: WarmupRequest = {
        action, ...(action === "start" && topic ? { topic } : {}),
        expectedWseq, expectedRevision,
        idempotencyKey: crypto.randomUUID(),
      };
      pending.current = request;
      return await execute(request, generation);
    } catch (error) {
      if (mounted.current && generation === epoch.current) {
        setProblem(error instanceof Error ? error.message : String(error));
        busyRef.current = false;
        setBusy(false);
      }
      return null;
    }
  };
  const retry = async (before: () => void): Promise<WarmupReceipt | null> => {
    if (busyRef.current || !pending.current) return null;
    busyRef.current = true;
    setBusy(true);
    before();
    return execute(pending.current, ++epoch.current);
  };
  return { content, contentError, reloadContent: () => setContentRetry((n) => n + 1), warmup,
    blocking, busy, problem, uncertain, act, retry, cancel };
}
