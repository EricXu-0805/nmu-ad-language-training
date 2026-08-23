// 操作端唯一写者 hook。双通道:BroadcastChannel(同机秒推)+ 服务端 /live/state(跨设备真值)。
// 全局队列保证换屏/换场时旧 hook 的慢请求不会在新场握手之后落库。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type InteractionPresentationDraft,
  type InteractionPresentationReceipt,
  type InteractionPresentationRequest,
} from "../api";
import { deliverAuthoritativeAudioSaved } from "./audioSavedAuthority";
import { bus } from "./bus";
import { requireServerWseq } from "./liveWriteAck";
import {
  liveWriteRetryDecision,
  type LiveWriteRetryKind,
} from "./liveWriteRetryPolicy";
import { readAuthoritativePatientRec } from "./patientRecAuthority";
import {
  buildExactInteractionPresentationRequest,
  exactRequestMatchesDraft,
  parseInteractionPresentationReceipt,
} from "./interactionPresentationRequest";
import {
  parseSyncPayload,
  type AudioSavedMsg,
  type CursorMsg,
  type PatientRecMsg,
  type RapportMsg,
  type SessionMsg,
  type SyncMsg,
} from "./messages";
import { nextWseq, observeWseq } from "./wseq";

const LIVE_POLL_MS = 2000;
type LiveWriteKind = LiveWriteRetryKind;

let globalWriteQueue: Promise<void> = Promise.resolve();
let activeConsoleSessionId: string | null = null;
let activeConsoleGeneration = 0;
let liveWriteSafetyEpoch = 0;
const safetyBlockedSessions = new Set<string>();
let activeLiveWrite: {
  sessionId: string;
  safetyEpoch: number;
  controller: AbortController;
} | null = null;

function broadcastCommitted(kind: LiveWriteKind, payload: object, wseq: number): void {
  const committed = { ...payload, wseq };
  if (kind === "session") bus.post({ type: "session", ...committed } as SessionMsg);
  else if (kind === "cursor") bus.post({ type: "cursor", ...committed } as CursorMsg);
  else bus.post({ type: "rapportStep", ...committed } as RapportMsg);
}

// 等当前已入队的普通 live 写全部落库，仅供“移交服务器控制”等有序交接。
// 安全暂停绝不能调用它；暂停必须抢占此队列并立即走权威 /pause。
export function flushLiveWrites(): Promise<void> {
  return globalWriteQueue.then(() => undefined, () => undefined);
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.detail : String(error);
}

export function useCursorWriter(sessionId: string) {
  const [syncError, setSyncError] = useState<string | null>(null);
  const failedWrite = useRef<{ kind: LiveWriteKind; payload: object } | null>(null);
  // A timeout has unknown server disposition. Keep the exact CAS body (including
  // revision/wseq) under its stable key so an explicit retry cannot mutate the
  // idempotency hash by fetching a newer snapshot.
  const pendingInteractionPresentations = useRef<Map<string, InteractionPresentationRequest>>(new Map());

  // 自定义 hook 内的 effect 先于调用方后续 effect 执行，因此首个握手入队前
  // activeConsoleSessionId 已就位。StrictMode 重放同一 session 时不会把已入队首写判旧。
  useEffect(() => {
    failedWrite.current = null;
    pendingInteractionPresentations.current.clear();
    setSyncError(null);
    const generation = ++activeConsoleGeneration;
    activeConsoleSessionId = sessionId;
    return () => {
      // 先让调用方同一次卸载 cleanup 有机会把 idle/thanks 收麦写加入队列，
      // 再等队尾落库后失效本 writer。generation 防 StrictMode 旧 cleanup 清空新 setup。
      queueMicrotask(() => {
        const tail = globalWriteQueue;
        void tail.finally(() => {
          if (activeConsoleGeneration === generation && activeConsoleSessionId === sessionId) {
            activeConsoleSessionId = null;
          }
        });
      });
    };
  }, [sessionId]);

  const enqueue = useCallback((kind: LiveWriteKind, payload: object): Promise<boolean> => {
    const writeSessionId = sessionId;
    const writeSafetyEpoch = liveWriteSafetyEpoch;
    const write = async (): Promise<boolean> => {
      if (activeConsoleSessionId !== writeSessionId
          || liveWriteSafetyEpoch !== writeSafetyEpoch
          || safetyBlockedSessions.has(writeSessionId)) return false;
      const controller = new AbortController();
      const active = { sessionId: writeSessionId, safetyEpoch: writeSafetyEpoch, controller };
      activeLiveWrite = active;
      try {
        const ack = await api.putLiveState(kind, payload, controller.signal);
        if (activeConsoleSessionId !== writeSessionId
            || liveWriteSafetyEpoch !== writeSafetyEpoch
            || safetyBlockedSessions.has(writeSessionId)) return false;
        // 只有服务器成功落库并签发 wseq 后，同机快通道才能放行。
        // 否则终态/撤回/未授权的 armed 会先在本机打开麦克风，再被 HTTP 409 迟到拒绝。
        const serverWseq = requireServerWseq(ack.wseq);
        observeWseq(serverWseq);
        broadcastCommitted(kind, payload, serverWseq);
        // Writes are globally ordered. A later ACK also proves that any earlier
        // ambiguous handshake already established sufficient server context.
        failedWrite.current = null;
        setSyncError(null);
        return true;
      } catch (error) {
        if (activeConsoleSessionId !== writeSessionId
            || liveWriteSafetyEpoch !== writeSafetyEpoch
            || safetyBlockedSessions.has(writeSessionId)) return false;
        if (error instanceof ApiError && error.status === 409) {
          // 语义拒绝(已暂停/已锁分等策略冲突):同一载荷重发永远失败,重试循环会锁死控制条。
          // 丢弃被拒载荷让服务端真值接管(暂停投影/锁分收回由服务端游标下发);
          // 只有暂停竞态的拒绝静默放行,其余仍亮警示但重试不再重发毒载荷。
          failedWrite.current = null;
          if (!error.detail.includes("已暂停")) setSyncError(errorMessage(error));
          return false;
        }
        failedWrite.current = { kind, payload };
        setSyncError(errorMessage(error));
        return false;
      } finally {
        if (activeLiveWrite === active) activeLiveWrite = null;
      }
    };
    const result = globalWriteQueue.then(write, write);
    globalWriteQueue = result.then(() => undefined, () => undefined);
    return result;
  }, [sessionId]);

  const postSession = useCallback((m: Omit<SessionMsg, "type" | "wseq">) => {
    if (activeConsoleSessionId !== sessionId || m.sessionId !== sessionId) return Promise.resolve(false);
    const stamped = { ...m, wseq: nextWseq() };
    return enqueue("session", stamped);
  }, [enqueue, sessionId]);
  const postCursor = useCallback((m: Omit<CursorMsg, "type" | "wseq" | "sessionId">) => {
    if (activeConsoleSessionId !== sessionId) return Promise.resolve(false);
    const stamped = { ...m, sessionId, wseq: nextWseq() };
    return enqueue("cursor", stamped);
  }, [enqueue, sessionId]);
  const postRapport = useCallback((m: Omit<RapportMsg, "type" | "wseq" | "sessionId">) => {
    if (activeConsoleSessionId !== sessionId) return Promise.resolve(false);
    const stamped = { ...m, sessionId, wseq: nextWseq() };
    return enqueue("rapportStep", stamped);
  }, [enqueue, sessionId]);
  const beginSafetyPause = useCallback(() => {
    // 先在同源老人端建立一个不可被旧游标解除的本地关麦闩，再撤销本 writer
    // 的排队/在途请求。服务器 /pause 不走此队列，可立刻竞争并最终签发 paused 投影。
    safetyBlockedSessions.add(sessionId);
    liveWriteSafetyEpoch += 1;
    failedWrite.current = null;
    bus.post({ type: "safetyStop", sessionId });
    const active = activeLiveWrite;
    if (active?.sessionId === sessionId) {
      active.controller.abort(new DOMException("场次开始安全暂停", "AbortError"));
    }
  }, [sessionId]);
  const releaseSafetyPause = useCallback(() => {
    // 只在服务器 resume 已 ACK 后调用；老人端自己的闩仍要等它先观察到 paused、
    // 再观察到 active 的权威投影才会解除。
    safetyBlockedSessions.delete(sessionId);
  }, [sessionId]);
  const publishCommittedCursor = useCallback((
    cursor: Omit<CursorMsg, "type">,
    acknowledgedWseq: number,
  ): boolean => {
    if (activeConsoleSessionId !== sessionId || cursor.sessionId !== sessionId) return false;
    const serverWseq = requireServerWseq(acknowledgedWseq);
    if (cursor.wseq !== serverWseq) return false;
    const parsed = parseSyncPayload("cursor", {
      ...cursor,
      sessionId,
      wseq: serverWseq,
    });
    if (!parsed) return false;
    observeWseq(serverWseq);
    bus.post(parsed);
    setSyncError(null);
    return true;
  }, [sessionId]);
  const commitInteractionPresentation = useCallback((
    body: InteractionPresentationDraft,
    shouldCommit: () => boolean = () => true,
  ): Promise<InteractionPresentationReceipt | null> => {
    const writeSessionId = sessionId;
    const writeSafetyEpoch = liveWriteSafetyEpoch;
    const write = async (): Promise<InteractionPresentationReceipt | null> => {
      // The action may have been invalidated while waiting behind an earlier
      // cursor write.  Cancel it before any evidence can reach the server.
      if (activeConsoleSessionId !== writeSessionId
          || liveWriteSafetyEpoch !== writeSafetyEpoch
          || safetyBlockedSessions.has(writeSessionId)
          || !shouldCommit()) return null;
      const controller = new AbortController();
      const active = { sessionId: writeSessionId, safetyEpoch: writeSafetyEpoch, controller };
      activeLiveWrite = active;
      let receipt: InteractionPresentationReceipt;
      try {
        let exactBody = pendingInteractionPresentations.current.get(body.idempotency_key);
        if (exactBody) {
          if (!exactRequestMatchesDraft(exactBody, body)) {
            throw new Error("同一原子呈现幂等键不能绑定另一份操作");
          }
        } else {
          // This GET occurs only after the operation reaches the global queue
          // head. Same-tab live writes therefore cannot invalidate its CAS pair
          // between snapshot and POST; another tab is still rejected by server CAS.
          const runtime = await api.getSessionRuntime(writeSessionId, controller.signal);
          if (activeConsoleSessionId !== writeSessionId
              || liveWriteSafetyEpoch !== writeSafetyEpoch
              || safetyBlockedSessions.has(writeSessionId)
              || !shouldCommit()) return null;
          exactBody = buildExactInteractionPresentationRequest(
            body, runtime, writeSessionId);
          pendingInteractionPresentations.current.set(body.idempotency_key, exactBody);
        }
        try {
          const rawReceipt = await api.commitInteractionPresentation(
            writeSessionId, exactBody, controller.signal);
          receipt = parseInteractionPresentationReceipt(
            rawReceipt, writeSessionId, exactBody);
        } catch (error) {
          // 4xx is a definitive rejection, so the exact body has no uncertain
          // network disposition to replay. Timeouts/5xx retain it unchanged.
          if (error instanceof ApiError && error.status >= 400
              && error.status < 500 && error.status !== 408) {
            pendingInteractionPresentations.current.delete(body.idempotency_key);
          }
          throw error;
        }
      } finally {
        if (activeLiveWrite === active) activeLiveWrite = null;
      }
      // If pause/wrap-up began during the request, the server transaction may
      // legitimately have won first.  Do not flash its cursor locally; the
      // queued safety transition will immediately become authoritative.
      if (activeConsoleSessionId !== writeSessionId
          || liveWriteSafetyEpoch !== writeSafetyEpoch
          || safetyBlockedSessions.has(writeSessionId)
          || !shouldCommit()) return null;
      if (!publishCommittedCursor(receipt.cursor, receipt.wseq)) {
        throw new Error("原子交互呈现回执未通过本场次游标校验");
      }
      pendingInteractionPresentations.current.delete(body.idempotency_key);
      return receipt;
    };
    const result = globalWriteQueue.then(write, write);
    globalWriteQueue = result.then(() => undefined, () => undefined);
    return result;
  }, [publishCommittedCursor, sessionId]);
  const retrySync = useCallback(() => {
    const failure = failedWrite.current;
    if (!failure) {
      setSyncError(null);
      return;
    }
    if (liveWriteRetryDecision(failure.kind) === "restart_context") {
      setSyncError("场次连接未完成确认，旧指令已停止执行；请安全退出本场训练后重新进入");
      return;
    }
    setSyncError(null);
    void enqueue(failure.kind, failure.payload);
  }, [enqueue]);
  const resetSession = useCallback(() => bus.reset(), []);
  return {
    postSession,
    postCursor,
    postRapport,
    beginSafetyPause,
    releaseSafetyPause,
    publishCommittedCursor,
    commitInteractionPresentation,
    resetSession,
    syncError,
    retrySync,
  };
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

// 操作端订阅服务器 patientRec 投影。BroadcastChannel 只负责秒级唤醒拉取，绝不直接
// 成为设备失败/开麦真值；这样 ACK 前的本机消息或伪造消息都不能改变控制台状态。
export function usePatientRec(sessionId: string): PatientRecMsg | null {
  const [rec, setRec] = useState<PatientRecMsg | null>(null);
  const sidRef = useRef(sessionId);
  sidRef.current = sessionId;
  useEffect(() => { setRec(null); }, [sessionId]);
  useEffect(() => {
    // 工作台登录页、准备区和今日队列没有当前场次；此时既没有可绑定的
    // patientRec 真值，也不应每两秒读取全局 live 状态。
    if (!sessionId) return;
    let cancelled = false;
    let inFlight = false;
    let rerun = false;
    const refreshAuthoritative = () => {
      if (cancelled) return;
      if (inFlight) {
        rerun = true;
        return;
      }
      inFlight = true;
      api.getConsoleState()
        .then((state) => {
          if (cancelled) return;
          const message = readAuthoritativePatientRec(state.patientRec, sidRef.current);
          if (message) setRec(message);
          else if (state.patientRec == null) setRec(null);
        })
        .catch(() => {})
        .finally(() => {
          inFlight = false;
          if (!cancelled && rerun) {
            rerun = false;
            refreshAuthoritative();
          }
        });
    };
    const unsub = bus.subscribe((msg: SyncMsg) => {
      if (msg.type === "patientRec") refreshAuthoritative();
    });
    refreshAuthoritative();
    const timer = setInterval(refreshAuthoritative, LIVE_POLL_MS);
    return () => {
      cancelled = true;
      unsub();
      clearInterval(timer);
    };
  }, [sessionId]);
  return rec;
}

// 操作端订阅老人端录音回报。BroadcastChannel 只是同机秒级 wake hint；
// handler/自动推进只消费服务端单调 AudioCaptureReceipt，轮询间连续两条也不会被单槽覆盖。
export function useAudioSaved(handler: (m: AudioSavedMsg) => void): void {
  const seen = useRef<Set<string>>(new Set());
  const afterSeqBySession = useRef<Map<string, number>>(new Map());
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let rerun = false;

    const refreshAuthoritative = () => {
      if (cancelled) return;
      if (inFlight) {
        // 如果 hint 追上了已在路上的轮询，该请求可能在服务端回执落库前取到旧值。
        // 记一次补拉，避免丢掉这个唤醒。
        rerun = true;
        return;
      }
      inFlight = true;
      api.getConsoleState()
        .then(async (state) => {
          if (cancelled) return;
          const session = parseSyncPayload("session", state.session);
          if (!session) return;
          const sessionId = session.sessionId;
          let cursor = afterSeqBySession.current.get(sessionId) ?? 0;
          try {
            // 一页满了就继续，且只在逐条通过严格 schema 后推进 cursor。
            for (let pageNo = 0; pageNo < 20; pageNo += 1) {
              const page = await api.audioReceipts(sessionId, cursor, 500);
              if (cancelled) return;
              if (page.session_id !== sessionId || page.after_seq !== cursor
                  || !Number.isSafeInteger(page.last_seq) || page.last_seq < cursor) {
                throw new Error("服务端录音收据页游标无效");
              }
              let expectedGreaterThan = cursor;
              for (const receipt of page.receipts) {
                if (!Number.isSafeInteger(receipt.server_seq)
                    || receipt.server_seq <= expectedGreaterThan
                    || receipt.session_id !== sessionId) {
                  throw new Error("服务端录音收据顺序无效");
                }
                const message = parseSyncPayload("audioSaved", {
                  rawAudioId: receipt.raw_audio_id,
                  durationSeconds: receipt.duration_seconds,
                  byteCount: receipt.byte_count,
                  checksum: receipt.checksum,
                  turnKey: receipt.turn_key,
                  sessionId: receipt.session_id,
                  containsDirectIdentifier: receipt.contains_direct_identifier,
                });
                if (!message) throw new Error("服务端录音收据字段无效");
                expectedGreaterThan = receipt.server_seq;
                if (!seen.current.has(message.rawAudioId)) {
                  seen.current.add(message.rawAudioId);
                  handlerRef.current(message);
                }
              }
              cursor = page.last_seq;
              afterSeqBySession.current.set(sessionId, cursor);
              if (page.receipts.length < 500) break;
            }
          } catch (error) {
            // 只对明确的旧后端 404 保留单槽兼容；新后端的临时/鉴权/完整性失败
            // 一律停住等待下一次权威账本拉取，不能拿单槽猜测补事件。
            if (error instanceof ApiError && error.status === 404) {
              deliverAuthoritativeAudioSaved(state.audioSaved, seen.current, (message) => {
                handlerRef.current(message);
              });
            }
          }
        })
        .catch(() => {})
        .finally(() => {
          inFlight = false;
          if (!cancelled && rerun) {
            rerun = false;
            refreshAuthoritative();
          }
        });
    };
    const unsub = bus.subscribe((msg: SyncMsg) => {
      if (msg.type === "audioSaved") refreshAuthoritative();
    });
    refreshAuthoritative();
    const timer = setInterval(refreshAuthoritative, LIVE_POLL_MS);
    return () => {
      cancelled = true;
      unsub();
      clearInterval(timer);
    };
  }, []);
}
