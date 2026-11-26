import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import {
  presenceViewFrom,
  type PatientPresenceState,
  type PatientPresenceView,
  type PresencePayload,
} from "./presenceView";

export type { PatientPresenceState, PatientPresenceView };

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
      api.getConsoleState(sessionId)
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
            // 只有"路由不存在"(旧后端)才算不支持。"场次不存在"的 404 说明本场此刻不在
            // 任何床旁槽,受试者端不可能在本场在线:按未连接处理,录音入口的在场门禁照拦。
            setPresence(null);
            setUnsupported(!error.detail.includes("场次"));
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
    return presenceViewFrom({ sessionId, checking, unsupported, unavailable, presence });
  }, [checking, clock, presence, sessionId, unavailable, unsupported]);
}
