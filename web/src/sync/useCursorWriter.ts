// 操作端唯一写者 hook。双通道:BroadcastChannel(同机秒推)+ 服务端 /live/state(跨设备真值)。
// 全局队列保证换屏/换场时旧 hook 的慢请求不会在新场握手之后落库。
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { bus } from "./bus";
import type { AudioSavedMsg, CursorMsg, PatientRecMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";
import { nextWseq, observeWseq } from "./wseq";

const LIVE_POLL_MS = 2000;
type LiveWriteKind = "session" | "cursor" | "rapportStep";

let globalWriteQueue: Promise<void> = Promise.resolve();
let activeConsoleSessionId: string | null = null;

// 等当前已入队的 live 写全部落库。暂停/恢复等"直连 HTTP、不进队列"的控制请求
// 必须先排空队列再发,否则 pause 先落地会把排队中的收麦游标 409 掉(复审确证的死锁根因)。
export function flushLiveWrites(): Promise<void> {
  return globalWriteQueue.then(() => undefined, () => undefined);
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

export function useCursorWriter(sessionId: string) {
  const [syncError, setSyncError] = useState<string | null>(null);
  const lastSession = useRef<object | null>(null);
  const lastCursor = useRef<object | null>(null);
  const lastRapport = useRef<object | null>(null);

  // 自定义 hook 内的 effect 先于调用方后续 effect 执行，因此首个握手入队前
  // activeConsoleSessionId 已就位。StrictMode 重放同一 session 时不会把已入队首写判旧。
  useEffect(() => { activeConsoleSessionId = sessionId; }, [sessionId]);

  const enqueue = useCallback((kind: LiveWriteKind, payload: object) => {
    const writeSessionId = sessionId;
    const write = async () => {
      if (activeConsoleSessionId !== writeSessionId) return;
      try {
        const ack = await api.putLiveState(kind, payload);
        if (activeConsoleSessionId !== writeSessionId) return;
        observeWseq(ack.wseq);
        setSyncError(null);
      } catch (error) {
        if (activeConsoleSessionId !== writeSessionId) return;
        if (error instanceof ApiError && error.status === 409) {
          // 语义拒绝(已暂停/已锁分等策略冲突):同一载荷重发永远失败,重试循环会锁死控制条。
          // 丢弃被拒载荷让服务端真值接管(暂停投影/锁分收回由服务端游标下发);
          // 只有暂停竞态的拒绝静默放行,其余仍亮警示但重试不再重发毒载荷。
          if (lastSession.current === payload) lastSession.current = null;
          if (lastCursor.current === payload) lastCursor.current = null;
          if (lastRapport.current === payload) lastRapport.current = null;
          if (!error.detail.includes("已暂停")) setSyncError(errorMessage(error));
          return;
        }
        setSyncError(errorMessage(error));
      }
    };
    globalWriteQueue = globalWriteQueue.then(write, write);
  }, [sessionId]);

  const postSession = useCallback((m: Omit<SessionMsg, "type" | "wseq">) => {
    if (activeConsoleSessionId !== sessionId || m.sessionId !== sessionId) return;
    const stamped = { ...m, wseq: nextWseq() };
    lastSession.current = stamped;
    bus.post({ type: "session", ...stamped });
    enqueue("session", stamped);
  }, [enqueue, sessionId]);
  const postCursor = useCallback((m: Omit<CursorMsg, "type" | "wseq" | "sessionId">) => {
    if (activeConsoleSessionId !== sessionId) return;
    const stamped = { ...m, sessionId, wseq: nextWseq() };
    lastCursor.current = stamped;
    bus.post({ type: "cursor", ...stamped });
    enqueue("cursor", stamped);
  }, [enqueue, sessionId]);
  const postRapport = useCallback((m: Omit<RapportMsg, "type" | "wseq" | "sessionId">) => {
    if (activeConsoleSessionId !== sessionId) return;
    const stamped = { ...m, sessionId, wseq: nextWseq() };
    lastRapport.current = stamped;
    bus.post({ type: "rapportStep", ...stamped });
    enqueue("rapportStep", stamped);
  }, [enqueue, sessionId]);
  const retrySync = useCallback(() => {
    setSyncError(null);
    if (lastSession.current) enqueue("session", lastSession.current);
    if (lastCursor.current) enqueue("cursor", lastCursor.current);
    if (lastRapport.current) enqueue("rapportStep", lastRapport.current);
  }, [enqueue]);
  const resetSession = useCallback(() => bus.reset(), []);
  return { postSession, postCursor, postRapport, resetSession, syncError, retrySync };
}

// 停止录音后的回报看门狗:arm→真实开录之间没有回执,若超时没等到 audioSaved,
// 说明这段可能是零字节(麦克风权限被拒/armed 消息丢失/arm-stop 间隔被轮询快照吞掉)——
// 必须让研究者当场知道,而不是场次收尾时才发现"暂无登记音频"。
export function useSaveWatchdog(onTimeout: () => void, ms = 8000) {
  const timer = useRef<number | null>(null);
  const cb = useRef(onTimeout);
  cb.current = onTimeout;
  const clear = useCallback(() => {
    if (timer.current != null) { clearTimeout(timer.current); timer.current = null; }
  }, []);
  const start = useCallback(() => {
    clear();
    timer.current = window.setTimeout(() => { timer.current = null; cb.current(); }, ms);
  }, [clear, ms]);
  useEffect(() => clear, [clear]);
  return { start, clear };
}

// 操作端订阅老人端麦克风真值上报(patientRec):自助开录时操作端唯一的感知渠道。
// 这是"最新状态"不是事件流:bus 秒推 + 轮询兜底,跨场次的上报一律丢弃。
export function usePatientRec(sessionId: string): PatientRecMsg | null {
  const [rec, setRec] = useState<PatientRecMsg | null>(null);
  const sidRef = useRef(sessionId);
  sidRef.current = sessionId;
  useEffect(() => {
    const apply = (m: PatientRecMsg) => { if (m.sessionId === sidRef.current) setRec(m); };
    const unsub = bus.subscribe((msg: SyncMsg) => {
      if (msg.type === "patientRec") apply(msg);
    });
    const timer = setInterval(() => {
      api.getConsoleState()
        .then((d) => {
          const m = (d as { patientRec?: PatientRecMsg | null }).patientRec;
          if (m && typeof m.active === "boolean") apply({ ...m, type: "patientRec" });
        })
        .catch(() => {});
    }, LIVE_POLL_MS);
    return () => { unsub(); clearInterval(timer); };
  }, []);
  return rec;
}

// 操作端订阅老人端录音回报:bus(同机)+ 服务端轮询(跨设备),按 rawAudioId 去重。
export function useAudioSaved(handler: (m: AudioSavedMsg) => void): void {
  const seen = useRef<Set<string>>(new Set());
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    const deliver = (m: AudioSavedMsg) => {
      if (!m.rawAudioId || seen.current.has(m.rawAudioId)) return;
      seen.current.add(m.rawAudioId);
      handlerRef.current(m);
    };
    const unsub = bus.subscribe((msg: SyncMsg) => {
      if (msg.type === "audioSaved") deliver(msg);
    });
    const timer = setInterval(() => {
      api.getConsoleState()
        .then((d) => {
          const a = d.audioSaved as AudioSavedMsg | null;
          if (a && a.rawAudioId) deliver({ ...a, type: "audioSaved" });
        })
        .catch(() => {});
    }, LIVE_POLL_MS);
    return () => { unsub(); clearInterval(timer); };
  }, []);
}
