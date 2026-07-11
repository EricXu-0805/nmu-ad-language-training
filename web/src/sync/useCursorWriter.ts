// 操作端唯一写者 hook。双通道:BroadcastChannel(同机秒推)+ 服务端 /live/state(跨设备真值)。
// 服务端写失败不阻塞现场(fire-and-forget)——同机模式 bus 已足够,内网模式下轮询会追上。
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { bus } from "./bus";
import type { AudioSavedMsg, CursorMsg, PatientRecMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

const LIVE_POLL_MS = 2000;

// 写序号:以启动时刻打底、每次广播递增——操作端页面刷新后计数必然大于旧值,老人端不会拒收新写者。
let wseqCounter = Date.now();
const nextWseq = () => ++wseqCounter;

export function useCursorWriter() {
  const postSession = useCallback((m: Omit<SessionMsg, "type" | "wseq">) => {
    const stamped = { ...m, wseq: nextWseq() };
    bus.post({ type: "session", ...stamped });
    api.putLiveState("session", stamped).catch(() => {});
  }, []);
  const postCursor = useCallback((m: Omit<CursorMsg, "type" | "wseq">) => {
    const stamped = { ...m, wseq: nextWseq() };
    bus.post({ type: "cursor", ...stamped });
    api.putLiveState("cursor", stamped).catch(() => {});
  }, []);
  const postRapport = useCallback((m: Omit<RapportMsg, "type" | "wseq">) => {
    const stamped = { ...m, wseq: nextWseq() };
    bus.post({ type: "rapportStep", ...stamped });
    api.putLiveState("rapportStep", stamped).catch(() => {});
  }, []);
  const resetSession = useCallback(() => bus.reset(), []);
  return { postSession, postCursor, postRapport, resetSession };
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
      api.getLiveState()
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
      api.getLiveState()
        .then((d) => {
          const a = d.audioSaved as AudioSavedMsg | null;
          if (a && a.rawAudioId) deliver({ ...a, type: "audioSaved" });
        })
        .catch(() => {});
    }, LIVE_POLL_MS);
    return () => { unsub(); clearInterval(timer); };
  }, []);
}
