// 操作端唯一写者 hook。双通道:BroadcastChannel(同机秒推)+ 服务端 /live/state(跨设备真值)。
// 服务端写失败不阻塞现场(fire-and-forget)——同机模式 bus 已足够,内网模式下轮询会追上。
import { useCallback, useEffect, useRef } from "react";
import { api } from "../api";
import { bus } from "./bus";
import type { AudioSavedMsg, CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

const LIVE_POLL_MS = 2000;

export function useCursorWriter() {
  const postSession = useCallback((m: Omit<SessionMsg, "type">) => {
    bus.post({ type: "session", ...m });
    api.putLiveState("session", m).catch(() => {});
  }, []);
  const postCursor = useCallback((m: Omit<CursorMsg, "type">) => {
    bus.post({ type: "cursor", ...m });
    api.putLiveState("cursor", m).catch(() => {});
  }, []);
  const postRapport = useCallback((m: Omit<RapportMsg, "type">) => {
    bus.post({ type: "rapportStep", ...m });
    api.putLiveState("rapportStep", m).catch(() => {});
  }, []);
  const resetSession = useCallback(() => bus.reset(), []);
  return { postSession, postCursor, postRapport, resetSession };
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
