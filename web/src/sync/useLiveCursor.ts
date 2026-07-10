// 老人端只读订阅 hook。从不 advance/判分/写游标——只反映操作端广播的当前状态。
// 三源合一:localStorage 镜像(刷新恢复)→ BroadcastChannel(同机秒推)→ 服务端轮询(跨设备兜底)。
// seq 单调判新:轮询只在服务端 seq 前进时应用快照,避免旧快照覆盖 bus 刚推的新状态。
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { bus } from "./bus";
import type { CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

const LIVE_POLL_MS = 1500;

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
  const lastSeq = useRef(0);

  useEffect(() => {
    const unsub = bus.subscribe((msg: SyncMsg) => {
      setState((prev) => {
        if (msg.type === "session") return { session: msg, cursor: undefined, rapportStep: undefined };
        if (msg.type === "cursor") return { ...prev, cursor: msg };
        if (msg.type === "rapportStep") return { ...prev, rapportStep: msg };
        return prev; // audioSaved 不改老人端可视状态
      });
    });

    const timer = setInterval(() => {
      api.getLiveState()
        .then((d) => {
          if (d.seq <= lastSeq.current) return;   // 无新事
          lastSeq.current = d.seq;
          setState({
            session: (d.session as SessionMsg | null) ?? undefined,
            cursor: (d.cursor as CursorMsg | null) ?? undefined,
            rapportStep: (d.rapportStep as RapportMsg | null) ?? undefined,
          });
        })
        .catch(() => {});                          // 离线/后端未起:bus 仍可用
    }, LIVE_POLL_MS);

    return () => { unsub(); clearInterval(timer); };
  }, []);

  return state;
}
