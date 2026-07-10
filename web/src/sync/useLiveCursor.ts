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
  // 每类消息各记最后应用的写序号:bus 秒推与轮询快照双源竞态时,迟到的旧内容一律丢弃,
  // 画面绝不回跳到上一题/上一级线索(无 wseq 的旧消息按 0 处理,谁都能覆盖它)。
  const applied = useRef({ session: 0, cursor: 0, rapportStep: 0 });
  const fresh = (kind: keyof typeof applied.current, wseq: number | undefined): boolean => {
    const w = wseq ?? 0;
    if (w !== 0 && w <= applied.current[kind]) return false;
    applied.current[kind] = Math.max(applied.current[kind], w);
    return true;
  };

  useEffect(() => {
    // 初始镜像也计入已应用序号,防轮询用更旧的服务端快照覆盖它
    const snap = bus.snapshot();
    applied.current = {
      session: snap.session?.wseq ?? 0,
      cursor: snap.cursor?.wseq ?? 0,
      rapportStep: snap.rapportStep?.wseq ?? 0,
    };

    const unsub = bus.subscribe((msg: SyncMsg) => {
      setState((prev) => {
        if (msg.type === "session") {
          if (!fresh("session", msg.wseq)) return prev;
          return { session: msg, cursor: undefined, rapportStep: undefined };
        }
        if (msg.type === "cursor") return fresh("cursor", msg.wseq) ? { ...prev, cursor: msg } : prev;
        if (msg.type === "rapportStep") return fresh("rapportStep", msg.wseq) ? { ...prev, rapportStep: msg } : prev;
        return prev; // audioSaved 不改老人端可视状态
      });
    });

    const timer = setInterval(() => {
      api.getLiveState()
        .then((d) => {
          if (d.seq <= lastSeq.current) return;   // 无新事
          lastSeq.current = d.seq;
          const s = (d.session as SessionMsg | null) ?? undefined;
          const c = (d.cursor as CursorMsg | null) ?? undefined;
          const r = (d.rapportStep as RapportMsg | null) ?? undefined;
          setState((prev) => {
            let next = prev;
            if (s && fresh("session", s.wseq)) next = { session: s, cursor: undefined, rapportStep: undefined }; // 新场次握手:旧游标一并清
            if (c && fresh("cursor", c.wseq)) next = { ...next, cursor: c };
            if (r && fresh("rapportStep", r.wseq)) next = { ...next, rapportStep: r };
            return next;
          });
        })
        .catch(() => {});                          // 离线/后端未起:bus 仍可用
    }, LIVE_POLL_MS);

    return () => { unsub(); clearInterval(timer); };
  }, []);

  return state;
}
