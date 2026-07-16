import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";

export type PatientPresenceState = "checking" | "online" | "offline" | "unseen" | "unsupported" | "unavailable";

export interface PatientPresenceView {
  state: PatientPresenceState;
  screenLabel: string;
  lastSeenLabel: string | null;
}

interface PresencePayload {
  session_id?: string;
  screen?: string;
  last_seen_at?: string | null;
  online?: boolean;
}

const SCREEN_LABELS: Record<string, string> = {
  waiting: "等待开始",
  loading: "正在准备题目",
  rapport: "关系建立中",
  present: "正在看题",
  record: "正在回答",
  thanks: "正在看结束提示",
  paused: "已显示暂停提示",
  complete: "已完成",
  error: "等待研究者处理",
};

function relativeSeen(value?: string | null): string | null {
  if (!value) return null;
  const ms = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "刚刚有响应";
  if (ms < 12_000) return "刚刚有响应";
  if (ms < 60_000) return `约 ${Math.max(1, Math.round(ms / 1000))} 秒前有响应`;
  return `约 ${Math.max(1, Math.round(ms / 60_000))} 分钟前有响应`;
}

export function usePatientPresence(sessionId?: string | null): PatientPresenceView {
  const [presence, setPresence] = useState<PresencePayload | null>(null);
  const [checking, setChecking] = useState(Boolean(sessionId));
  const [unavailable, setUnavailable] = useState(false);
  const [unsupported, setUnsupported] = useState(false);
  // 即使后端 payload 没变化，也要让“多久前响应”随时间更新。
  const [clock, setClock] = useState(0);

  useEffect(() => {
    setPresence(null);
    setChecking(Boolean(sessionId));
    setUnavailable(false);
    setUnsupported(false);
    if (!sessionId) return;
    let cancelled = false;
    let inFlight = false;
    const poll = () => {
      if (inFlight) return;
      inFlight = true;
      api.getConsoleState()
        .then((data) => {
          if (cancelled) return;
          if (!Object.prototype.hasOwnProperty.call(data, "patientPresence")) {
            setPresence(null);
            setUnsupported(true);
            setUnavailable(false);
            setChecking(false);
            return;
          }
          const next = (data as { patientPresence?: PresencePayload | null }).patientPresence ?? null;
          setPresence(next?.session_id === sessionId ? next : null);
          setUnsupported(false);
          setUnavailable(false);
          setChecking(false);
        })
        .catch((error) => {
          if (cancelled) return;
          if (error instanceof ApiError && error.status === 404) {
            setPresence(null);
            setUnsupported(true);
            setUnavailable(false);
            setChecking(false);
            return;
          }
          setUnsupported(false);
          setUnavailable(true);
          setChecking(false);
        })
        .finally(() => { inFlight = false; });
    };
    poll();
    const pollTimer = window.setInterval(poll, 3_000);
    const clockTimer = window.setInterval(() => setClock((n) => n + 1), 5_000);
    return () => { cancelled = true; clearInterval(pollTimer); clearInterval(clockTimer); };
  }, [sessionId]);

  return useMemo(() => {
    void clock;
    if (!sessionId) return { state: "unseen", screenLabel: "尚未建立场次", lastSeenLabel: null };
    if (checking) return { state: "checking", screenLabel: "正在确认患者端", lastSeenLabel: null };
    if (unsupported) return { state: "unsupported", screenLabel: "该版本不支持患者端在线状态", lastSeenLabel: null };
    if (unavailable) return { state: "unavailable", screenLabel: "暂时无法读取双端状态", lastSeenLabel: relativeSeen(presence?.last_seen_at) };
    if (!presence) return { state: "unseen", screenLabel: "等待患者端打开本场次", lastSeenLabel: null };
    return {
      state: presence.online ? "online" : "offline",
      screenLabel: SCREEN_LABELS[presence.screen ?? ""] ?? "患者端已打开",
      lastSeenLabel: relativeSeen(presence.last_seen_at),
    };
  }, [checking, clock, presence, sessionId, unavailable, unsupported]);
}
