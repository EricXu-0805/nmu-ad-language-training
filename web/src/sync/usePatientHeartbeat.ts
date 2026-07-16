import { useEffect, useRef } from "react";
import { api, ApiError } from "../api";
import type { PatientHeartbeatRequest, PatientPresenceScreen } from "../types";

const HEARTBEAT_MS = 5000;

/**
 * 老人端最小在场确认。换场时旧 interval 会先清理，新场次立即发第一拍；
 * 屏幕或操作端写序号变化时也立即确认，因此研究者端看到的是“老人端已显示到哪里”。
 *
 * 旧后端没有该路由时会返回 404/405：本场关闭额外心跳，但不影响既有 live/state
 * 与 BroadcastChannel 链路，也不把“不支持新能力”伪装成断线。
 */
export function usePatientHeartbeat(
  sessionId: string | undefined,
  screen: PatientPresenceScreen,
  cursorWseq?: number,
): void {
  const unsupportedSessions = useRef<Set<string>>(new Set());
  const latest = useRef({ sessionId, screen, cursorWseq });
  latest.current = { sessionId, screen, cursorWseq };
  const inFlight = useRef(false);
  const lastAttemptedSignature = useRef<string | null>(null);
  const queuedForce = useRef(false);
  const sendRef = useRef<(force?: boolean) => void>(() => {});
  const mounted = useRef(true);

  const sendLatest = async (force = false) => {
    const current = latest.current;
    if (!mounted.current || !current.sessionId || unsupportedSessions.current.has(current.sessionId)) return;
    const signature = `${current.sessionId}\n${current.screen}\n${current.cursorWseq ?? ""}`;
    if (inFlight.current) {
      if (force) queuedForce.current = true;
      return;
    }
    if (!force && signature === lastAttemptedSignature.current) return;

    inFlight.current = true;
    lastAttemptedSignature.current = signature;
    const body: PatientHeartbeatRequest = {
      session_id: current.sessionId,
      screen: current.screen,
    };
    if (typeof current.cursorWseq === "number" && Number.isFinite(current.cursorWseq)) {
      body.cursor_wseq = current.cursorWseq;
    }
    try {
      await api.patientHeartbeat(body);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) {
        // 只有"路由不存在"(旧后端)才永久关闭本场心跳;"场次不存在"的 404 是业务态
        // (换库/建场竞态),下一拍可能就恢复,闩死会让在场判定整场假离线。
        if (!error.detail.includes("场次")) unsupportedSessions.current.add(current.sessionId);
      }
      // 409 表示服务器已换场；live/state 轮询会拿到新握手。其余网络波动也由
      // useLiveCursor 的重连状态统一呈现，这里不另起一套相互打架的错误 UI。
    } finally {
      inFlight.current = false;
      if (mounted.current) {
        const next = latest.current;
        const nextSignature = next.sessionId ? `${next.sessionId}\n${next.screen}\n${next.cursorWseq ?? ""}` : null;
        const shouldForce = queuedForce.current;
        queuedForce.current = false;
        // 场次/画面在请求途中变化时串行补发，避免旧请求迟到后覆盖新的 screen ack。
        if (nextSignature && (nextSignature !== signature || shouldForce)) sendRef.current(shouldForce);
      }
    }
  };
  sendRef.current = (force = false) => { void sendLatest(force); };

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    sendRef.current();
    const timer = window.setInterval(() => sendRef.current(true), HEARTBEAT_MS);
    return () => clearInterval(timer);
  }, [sessionId]);

  // 画面或命令序号变化即确认；同一签名不会与场次 effect 重复发送。
  useEffect(() => { sendRef.current(); }, [sessionId, screen, cursorWseq]);
}
