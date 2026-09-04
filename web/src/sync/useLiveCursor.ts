// 老人端只读订阅 hook。从不 advance/判分/写游标——只反映操作端广播的当前状态。
// 三源合一:localStorage 镜像(刷新恢复)→ BroadcastChannel(同机秒推)→ 服务端轮询(跨设备兜底)。
// seq 单调判新:轮询只在服务端 seq 前进时应用快照,避免旧快照覆盖 bus 刚推的新状态。
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  DEVICE_CAPABILITY_UPDATED_EVENT,
  DEVICE_PAIR_REQUIRED_EVENT,
  getDeviceCapability,
  handleDeviceAuthorizationFailure,
  selectDeviceCredential,
} from "../api";
import type { LiveStateResponse } from "../types";
import { PATIENT_AUTOPILOT_WAKE_EVENT, liveSnapshotWake } from "./autopilotWake";
import { bus } from "./bus";
import { LiveAuthorizationFence } from "./liveAuthorizationFence";
import { livePollingHasActiveCapability } from "./livePollPolicy";
import {
  parseSyncPayload,
  type CursorMsg,
  type RapportMsg,
  type SessionMsg,
  type SyncMsg,
} from "./messages";

const LIVE_POLL_MS = 800;
const RECONNECT_GRACE_MS = 4500;
const LIVE_FETCH_TIMEOUT_MS = 4000;

class DeviceAuthorizationFailure extends Error {}

// api.getLiveState 目前不接 AbortSignal；老人端在这里保留同样的相对路径、capability 与失效行为，
// 但给真实 fetch 加物理 abort，避免悬挂请求让页面永远误判“仍连接”。
async function fetchLiveState(
  signal: AbortSignal,
  credential: ReturnType<typeof selectDeviceCredential>,
): Promise<LiveStateResponse> {
  const res = await fetch("/live/state", {
    method: "GET",
    credentials: "omit",
    headers: credential.headers,
    cache: "no-store",
    signal,
  });
  const text = await res.text();
  if (!res.ok) {
    const deviceAuthFailed = handleDeviceAuthorizationFailure(res.status, text, credential);
    let data: unknown = null;
    try { if (text) data = JSON.parse(text) as unknown; } catch { /* proxy body */ }
    const detail = data && typeof data === "object" && "detail" in data
      ? JSON.stringify((data as { detail: unknown }).detail)
      : text;
    if (deviceAuthFailed) throw new DeviceAuthorizationFailure(detail);
    throw new ApiError(res.status, detail);
  }
  try { return (text ? JSON.parse(text) : null) as LiveStateResponse; }
  catch { throw new ApiError(res.status, "服务器返回的实时状态格式错误"); }
}

export type LiveConnectionState = "connecting" | "connected" | "reconnecting";

export interface LiveState {
  session?: SessionMsg;
  cursor?: CursorMsg;
  rapportStep?: RapportMsg;
  connection: LiveConnectionState;
}

export function useLiveCursor(): LiveState {
  // localStorage/BroadcastChannel is an untrusted speed path.  Never render its
  // contents until this mount has obtained a successful server projection.
  const [state, setState] = useState<LiveState>({ connection: "connecting" });
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
    // Keep "not yet server-verified" separate from an explicit auth rejection.
    // Network failure while unverified must keep retrying; only a stable device
    // auth response pauses periodic probes until a capability update.
    let serverProjectionAuthorized = false;
    let deviceAuthBlocked = false;
    let authorizedSessionId: string | null = null;
    let pendingCapabilityProbe = false;
    const authorizationFence = new LiveAuthorizationFence();

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

    const unsub = bus.subscribe((msg: SyncMsg) => {
      if (!serverProjectionAuthorized) return;
      // A successful /live/state authorizes exactly its returned session.  A
      // console BroadcastChannel switch is ignored until the server confirms it.
      if (!authorizedSessionId || !("sessionId" in msg)
        || msg.sessionId !== authorizedSessionId) return;
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

    const poll = (allowWhileBlocked = false) => {
      if (cancelled || activePoll || (deviceAuthBlocked && !allowWhileBlocked)) return;
      const credential = selectDeviceCredential();
      if (!livePollingHasActiveCapability(credential)) {
        deviceAuthBlocked = true;
        serverProjectionAuthorized = false;
        authorizedSessionId = null;
        publish({ connection: "reconnecting" });
        if (allowWhileBlocked) {
          queueMicrotask(() => {
            if (!cancelled && !getDeviceCapability()) {
              window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
            }
          });
        }
        return;
      }
      const controller = new AbortController();
      activePoll = controller;
      const contactAtStart = contactVersion;
      const authorizationProbe = authorizationFence.capture(
        credential.record?.capability ?? null);
      const credentialStillSelected = () => authorizationFence.accepts(
        authorizationProbe,
        selectDeviceCredential().record?.capability ?? null,
      );
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, LIVE_FETCH_TIMEOUT_MS);

      fetchLiveState(controller.signal, credential)
        .then((d) => {
          // Abort is not sufficient once a response promise has already resolved.
          // Any active-token event invalidates all earlier probes, including a
          // queued old-token 200 that would otherwise repaint a blanked session.
          if (!credentialStillSelected()) return;
          // Authorization is established only by this successful response, never
          // merely by storing a capability or receiving a browser-local message.
          deviceAuthBlocked = false;
          serverProjectionAuthorized = true;
          markConnected();
          const seq = typeof d.seq === "number" ? d.seq : 0;
          const s = parseSyncPayload("session", d.session) ?? undefined;
          // 跨设备所有权唤醒必须在"seq 未变就返回"之前处理:console 启动服务器 AI
          // 不推进 LiveState.seq,放在 early-return 之后就永远收不到。
          const wake = liveSnapshotWake(d.autopilotWake, s?.sessionId);
          // 卸载后的旧 poll 或已被轮换掉的 capability 都不得再发唤醒：
          // 那会让一个已经离场的凭据把床旁 runner 叫起来。
          if (wake && !cancelled && credentialStillSelected()) {
            window.dispatchEvent(
              new CustomEvent(PATIENT_AUTOPILOT_WAKE_EVENT, { detail: wake }));
          }
          const firstServerSnapshot = !serverSnapshotSeen.current;
          if (!firstServerSnapshot && seq === lastSeq.current) return;   // 无新事
          // 单飞轮询不会乱序；seq 变小意味服务重启/换库，应当作为新服务端 epoch 接受，
          // 不能让旧 localStorage 因为序号更大就永久压住新场次。
          const epochReset = firstServerSnapshot || seq < lastSeq.current;
          serverSnapshotSeen.current = true;
          lastSeq.current = seq;
          const c = parseSyncPayload("cursor", d.cursor) ?? undefined;
          const r = parseSyncPayload("rapportStep", d.rapportStep) ?? undefined;
          const prev = stateRef.current;
          // 首次成功轮询是跨设备真值。服务端已空时必须撤下 localStorage
          // 的旧场次镜像，否则服务重启/换库后老人端会一直显示上一位受试者的题目。
          if (!s) {
            authorizedSessionId = null;
            applied.current = { session: 0, cursor: 0, rapportStep: 0 };
            bus.reset();
            publish({ connection: prev.connection });
            return;
          }
          const serverCursor = c?.sessionId === s.sessionId ? c : undefined;
          const serverRapport = r?.sessionId === s.sessionId ? r : undefined;
          authorizedSessionId = s.sessionId;
          bus.replaceSnapshot({ session: s, cursor: serverCursor, rapportStep: serverRapport });
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
        .catch((error: unknown) => {
          if (!credentialStillSelected()) return;
          if (cancelled || contactVersion !== contactAtStart) return;
          if (error instanceof DeviceAuthorizationFailure) {
            deviceAuthBlocked = true;
            serverProjectionAuthorized = false;
            authorizedSessionId = null;
            pendingCapabilityProbe = false;
            serverSnapshotSeen.current = false;
            lastSeq.current = 0;
            applied.current = { session: 0, cursor: 0, rapportStep: 0 };
            bus.reset();
            publish({ connection: "reconnecting" });
            return;
          }
          // 悬挂到超时是明确失联，立即进入重连并让 PatientShell 触发停麦；
          // 普通瞬时失败仍保留缓冲，避免单个丢包令适老界面闪烁。
          markFailed(timedOut);
        })
        .finally(() => {
          clearTimeout(timeout);
          if (activePoll === controller) activePoll = null;
          if (!cancelled && pendingCapabilityProbe && getDeviceCapability()) {
            pendingCapabilityProbe = false;
            poll(true);
          }
        });
    };
    // No authority request is sent before pairing.  The capability update event
    // below starts exactly one first probe after the PIN exchange succeeds.
    poll(true);
    const timer = setInterval(poll, LIVE_POLL_MS);
    const onOffline = () => {
      activePoll?.abort();
      markFailed(true);
    };
    const onOnline = () => poll();
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    const onCapabilityUpdated = () => {
      const active = getDeviceCapability();
      // Active token install/removal is not authority by itself.  Blank in the
      // same tick (so a TTS/upload-discovered expiry also stops the microphone),
      // close BC, then prove the new state with exactly one server request.
      serverProjectionAuthorized = false;
      authorizationFence.invalidate();
      authorizedSessionId = null;
      serverSnapshotSeen.current = false;
      lastSeq.current = 0;
      applied.current = { session: 0, cursor: 0, rapportStep: 0 };
      bus.reset();
      publish({ connection: "reconnecting" });
      if (active) {
        pendingCapabilityProbe = true;
        if (activePoll) activePoll.abort();
        else {
          pendingCapabilityProbe = false;
          poll(true);
        }
      } else {
        pendingCapabilityProbe = false;
        activePoll?.abort();
        deviceAuthBlocked = true;
        queueMicrotask(() => {
          if (!cancelled && !getDeviceCapability()) {
            window.dispatchEvent(new Event(DEVICE_PAIR_REQUIRED_EVENT));
          }
        });
      }
    };
    window.addEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, onCapabilityUpdated);
    if (!navigator.onLine) markFailed(true);

    return () => {
      cancelled = true;
      activePoll?.abort();
      unsub();
      clearInterval(timer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
      window.removeEventListener(DEVICE_CAPABILITY_UPDATED_EVENT, onCapabilityUpdated);
    };
  }, []);

  return state;
}
