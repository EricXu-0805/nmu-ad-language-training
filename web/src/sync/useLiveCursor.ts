// 老人端只读订阅 hook。从不 advance/判分/写游标——只反映操作端广播的当前状态。
// 初始从 localStorage 镜像恢复(迟到订阅/刷新),之后靠 channel 实时更新。
import { useEffect, useState } from "react";
import { bus } from "./bus";
import type { CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

export interface LiveState {
  session?: SessionMsg;
  cursor?: CursorMsg;
  rapportStep?: RapportMsg;
}

export function useLiveCursor(): LiveState {
  const [state, setState] = useState<LiveState>(() => {
    const snap = bus.snapshot();
    return { session: snap.session, cursor: snap.cursor, rapportStep: snap.rapportStep };
  });

  useEffect(() => {
    return bus.subscribe((msg: SyncMsg) => {
      setState((prev) => {
        if (msg.type === "session") return { session: msg, cursor: undefined, rapportStep: undefined };
        if (msg.type === "cursor") return { ...prev, cursor: msg };
        if (msg.type === "rapportStep") return { ...prev, rapportStep: msg };
        return prev; // audioSaved 不改老人端可视状态
      });
    });
  }, []);

  return state;
}
