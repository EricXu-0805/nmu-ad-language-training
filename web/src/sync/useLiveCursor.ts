// 老人端只读订阅 hook。从不 advance/判分/写游标——只反映操作端广播的当前状态。
// 三源合一:localStorage 镜像(刷新恢复)→ BroadcastChannel(同机秒推)→ 服务端轮询(跨设备兜底)。
// seq 单调判新:轮询只在服务端 seq 前进时应用快照,避免旧快照覆盖 bus 刚推的新状态。
import { useEffect, useRef, useState } from "react";
import { ApiError, getPin, PIN_REQUIRED_EVENT } from "../api";
import type { LiveStateResponse } from "../types";
import { bus } from "./bus";
import type { CursorMsg, RapportMsg, SessionMsg, SyncMsg } from "./messages";

const LIVE_POLL_MS = 1500;
const RECONNECT_GRACE_MS = 4500;
const LIVE_FETCH_TIMEOUT_MS = 4000;

// api.getLiveState 目前不接 AbortSignal；老人端在这里保留同样的相对路径、PIN 与 401 行为，
// 但给真实 fetch 加物理 abort，避免悬挂请求让页面永远误判“仍连接”。
async function fetchLiveState(signal: AbortSignal): Promise<LiveStateResponse> {
  const pin = getPin();
  const res = await fetch("/live/state", {
    method: "GET",
    headers: pin ? { "X-Console-Pin": pin } : undefined,
    signal,
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) data = JSON.parse(text) as unknown;
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new Event(PIN_REQUIRED_EVENT));
    const detail = data && typeof data === "object" && "detail" in data
      ? String((data as { detail: unknown }).detail)
      : text;
    throw new ApiError(res.status, detail);
  }
  return data as LiveStateResponse;
}

export type LiveConnectionState = "connecting" | "connected" | "reconnecting";

export interface LiveState {
  session?: SessionMsg;
  cursor?: CursorMsg;
  rapportStep?: RapportMsg;
  connection: LiveConnectionState;
}

export function useLiveCursor(): LiveState {
  const [state, setState] = useState<LiveState>(() => {
    const snap = bus.snapshot();
    const sessionId = snap.session?.sessionId;
    return {
      session: snap.session,
      cursor: snap.cursor?.sessionId === sessionId ? snap.cursor : undefined,
      rapportStep: snap.rapportStep?.sessionId === sessionId ? snap.rapportStep : undefined,
      connection: "connecting",
    };
  });
  const lastSeq = useRef(0);
  const serverSnapshotSeen = useRef(false);
  // 消息合并依赖“已应用 wseq”。这些 ref 变更不能放在 React state updater 中：
  // StrictMode 会为检查纯度双调 updater，第一次若先推进 ref，第二次就会把同一条消息误判为旧消息。
  const stateRef = useRef(state);
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
    let cancelled = false;
    let reconnectTimer: number | undefined;
    let activePoll: AbortController | null = null;
    let contactVersion = 0;

    const publish = (next: LiveState) => {
      if (cancelled || next === stateRef.current) return;
      stateRef.current = next;
      setState(next);
    };
    const setConnection = (connection: LiveConnectionState) => {
      const prev = stateRef.current;
      if (prev.connection !== connection) publish({ ...prev, connection });
    };

    const markConnected = () => {
      contactVersion += 1;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = undefined;
      setConnection("connected");
    };
    const markFailed = (immediate = false) => {
      if (cancelled) return;
      if (immediate) {
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
        setConnection("reconnecting");
        return;
      }
      if (reconnectTimer) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        if (!cancelled) setConnection("reconnecting");
      }, RECONNECT_GRACE_MS);
    };

    // 初始镜像也计入已应用序号,防轮询用更旧的服务端快照覆盖它
    const snap = bus.snapshot();
    applied.current = {
      session: snap.session?.wseq ?? 0,
      cursor: snap.cursor?.sessionId === snap.session?.sessionId ? snap.cursor?.wseq ?? 0 : 0,
      rapportStep: snap.rapportStep?.sessionId === snap.session?.sessionId ? snap.rapportStep?.wseq ?? 0 : 0,
    };
    // 这份快照必须同步补发到 state:render→effect 间隙落进镜像的消息(单机叠层挂载时
    // 常态出现)已被 applied 记账,若只记账不发布,后续同 wseq 重达会被 fresh() 判旧丢弃,
    // 画面停在旧游标直到轮询兜底(约一个轮询周期)。
    const sid0 = snap.session?.sessionId;
    setState((prev) => ({
      ...prev,
      session: snap.session,
      cursor: snap.cursor?.sessionId === sid0 ? snap.cursor : undefined,
      rapportStep: snap.rapportStep?.sessionId === sid0 ? snap.rapportStep : undefined,
    }));

    const unsub = bus.subscribe((msg: SyncMsg) => {
      // bus 只送内容,不当连接信号:BroadcastChannel 是浏览器层通道,后端进程死了它照样通,
      // 若据此 markConnected 会压制断线判定——录音保存必失败却不显示"正在重新连接"。
      const prev = stateRef.current;
      if (msg.type === "session") {
        if (!fresh("session", msg.wseq)) return;
        if (prev.session?.sessionId !== msg.sessionId) {
          applied.current.cursor = 0;
          applied.current.rapportStep = 0;
        }
        publish({ session: msg, cursor: undefined, rapportStep: undefined, connection: prev.connection });
      } else if (msg.type === "cursor") {
        if (msg.sessionId === prev.session?.sessionId && fresh("cursor", msg.wseq)) {
          publish({ ...prev, cursor: msg });
        }
      } else if (msg.type === "rapportStep") {
        if (msg.sessionId === prev.session?.sessionId && fresh("rapportStep", msg.wseq)) {
          publish({ ...prev, rapportStep: msg });
        }
      }
      // audioSaved 不改老人端可视状态
    });

    const poll = () => {
      if (cancelled || activePoll) return;
      const controller = new AbortController();
      activePoll = controller;
      const contactAtStart = contactVersion;
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, LIVE_FETCH_TIMEOUT_MS);

      fetchLiveState(controller.signal)
        .then((d) => {
          markConnected();
          const seq = typeof d.seq === "number" ? d.seq : 0;
          const firstServerSnapshot = !serverSnapshotSeen.current;
          if (!firstServerSnapshot && seq === lastSeq.current) return;   // 无新事
          // 单飞轮询不会乱序；seq 变小意味服务重启/换库，应当作为新服务端 epoch 接受，
          // 不能让旧 localStorage 因为序号更大就永久压住新场次。
          const epochReset = firstServerSnapshot || seq < lastSeq.current;
          serverSnapshotSeen.current = true;
          lastSeq.current = seq;
          const s = (d.session as SessionMsg | null) ?? undefined;
          const c = (d.cursor as CursorMsg | null) ?? undefined;
          const r = (d.rapportStep as RapportMsg | null) ?? undefined;
          const prev = stateRef.current;
          // 首次成功轮询是跨设备真值。服务端已空时必须撤下 localStorage
          // 的旧场次镜像，否则服务重启/换库后老人端会一直显示上一位受试者的题目。
          if (!s) {
            applied.current = { session: 0, cursor: 0, rapportStep: 0 };
            publish({ connection: prev.connection });
            return;
          }
          const serverCursor = c?.sessionId === s.sessionId ? c : undefined;
          const serverRapport = r?.sessionId === s.sessionId ? r : undefined;
          // 只有"新纪元"(首次快照/服务重启换库/换场次)才整体重置三类序号与画面。
          if (epochReset || prev.session?.sessionId !== s.sessionId) {
            applied.current = {
              session: s.wseq ?? 0,
              cursor: serverCursor?.wseq ?? 0,
              rapportStep: serverRapport?.wseq ?? 0,
            };
            publish({ session: s, cursor: serverCursor, rapportStep: serverRapport,
                      connection: prev.connection });
            return;
          }
          // 同场次增量快照:逐类只前进。/live/state 的 seq 被任何写入(含老人端自己的
          // audioSaved/patientRec 上报)顶大,而行内游标可能还是旧值——无条件套用会把画面
          // 拉回上一题,并把旧 cursor 里的 selfStart/armed 一并还魂(复审确证的开麦事故)。
          // 服务端 wseq 由单调分配器签发,bus 客户端戳经 observeWseq 对齐同一时钟域,可比。
          let next = prev;
          if (fresh("session", s.wseq)) next = { ...next, session: s };
          if (serverCursor && fresh("cursor", serverCursor.wseq)) next = { ...next, cursor: serverCursor };
          if (serverRapport && fresh("rapportStep", serverRapport.wseq)) next = { ...next, rapportStep: serverRapport };
          if (next !== prev) publish(next);
        })
        .catch(() => {
          if (cancelled || contactVersion !== contactAtStart) return;
          // 悬挂到超时是明确失联，立即进入重连并让 PatientShell 触发停麦；
          // 普通瞬时失败仍保留缓冲，避免单个丢包令适老界面闪烁。
          markFailed(timedOut);
        })
        .finally(() => {
          clearTimeout(timeout);
          if (activePoll === controller) activePoll = null;
        });
    };
    poll();
    const timer = setInterval(poll, LIVE_POLL_MS);
    const onOffline = () => {
      activePoll?.abort();
      markFailed(true);
    };
    const onOnline = () => poll();
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    if (!navigator.onLine) markFailed(true);

    return () => {
      cancelled = true;
      activePoll?.abort();
      unsub();
      clearInterval(timer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, []);

  return state;
}
