// 操作端唯一写者 hook。推进游标 / 广播握手与 rapport 步进 / 订阅老人端回传的 audioSaved。
import { useCallback, useEffect } from "react";
import { bus } from "./bus";
import type { AudioSavedMsg, CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

export function useCursorWriter() {
  const postSession = useCallback((m: Omit<SessionMsg, "type">) => bus.post({ type: "session", ...m }), []);
  const postCursor = useCallback((m: Omit<CursorMsg, "type">) => bus.post({ type: "cursor", ...m }), []);
  const postRapport = useCallback((m: Omit<RapportMsg, "type">) => bus.post({ type: "rapportStep", ...m }), []);
  const resetSession = useCallback(() => bus.reset(), []);
  return { postSession, postCursor, postRapport, resetSession };
}

// 操作端订阅老人端回传(录音完成 → 建 turn)。
export function useAudioSaved(handler: (m: AudioSavedMsg) => void): void {
  useEffect(() => {
    return bus.subscribe((msg: SyncMsg) => {
      if (msg.type === "audioSaved") handler(msg);
    });
  }, [handler]);
}
