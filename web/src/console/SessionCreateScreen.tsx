import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { newSessionId } from "../lib/ids";
import { EVENT_LINES, PHASE_TYPES } from "../types";
import type { EventLine, PhaseType, Session } from "../types";

// 显式绑定四元组建场次(绝不由 week 推导);绑冻结题库版本;建前做版本一致性断言。
export function SessionCreateScreen({ patientId, onStarted }: {
  patientId: string | null;
  onStarted: (s: Session) => void;
}) {
  const toast = useToast();
  const [pid, setPid] = useState(patientId ?? "");
  const [sid, setSid] = useState(newSessionId());
  const [weekNo, setWeekNo] = useState(2);
  const [phase, setPhase] = useState<PhaseType | null>("正式训练");
  const [eventLine, setEventLine] = useState<EventLine | null>("正式训练");
  const [bankVersion, setBankVersion] = useState("wk2-v1-20260707");
  const [busy, setBusy] = useState(false);

  // 后端题库版本断言(不等则 fail-closed 不放行)
  useEffect(() => {
    api.itemBank().then((b) => setBankVersion(b.version_id)).catch(() => {});
  }, []);

  const oddCombo =
    (weekNo === 1 && phase !== "关系建立") ||
    (weekNo >= 2 && eventLine === "关系建立环节");

  async function submit() {
    if (!pid.trim()) { toast("请填写受试者编号", "warn"); return; }
    if (!phase || !eventLine) { toast("请选择 phase_type 与 event_line", "warn"); return; }
    setBusy(true);
    try {
      await api.getPatient(pid); // 患者须已建档
      const s: Session = {
        session_id: sid, patient_id: pid, week_no: weekNo,
        phase_type: phase, event_line: eventLine, item_bank_version_id: bankVersion,
      };
      const created = await api.createSession(s);
      toast(`场次 ${created.session_id} 已建`, "ok");
      onStarted(created);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) toast("该受试者未建档,请先建档", "danger");
      else toast(e instanceof ApiError ? e.detail : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="col" style={{ maxWidth: 720 }}>
      <h2>建场次 · 显式绑定</h2>
      <div className="card col">
        <Field label="受试者编号"><TextInput value={pid} onChange={(e) => setPid(e.target.value)} /></Field>
        <Field label="场次编号 session_id"><TextInput value={sid} onChange={(e) => setSid(e.target.value)} /></Field>
        <Field label="周次 week_no (1–8)">
          <TextInput type="number" min={1} max={8} value={weekNo}
            onChange={(e) => setWeekNo(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} />
        </Field>
        <Field label="相位 phase_type(显式绑定,不由周次推导)">
          <EnumSelect options={PHASE_TYPES} value={phase} onChange={(v) => setPhase(v as PhaseType)} />
        </Field>
        <Field label="事件线 event_line">
          <EnumSelect options={EVENT_LINES} value={eventLine} onChange={(v) => setEventLine(v as EventLine)} />
        </Field>
        <Field label="题库版本(冻结,后端为准)">
          <div className="row"><StatusPill tone="primary">🔒 {bankVersion}</StatusPill></div>
        </Field>

        {oddCombo && (
          <div className="card" style={{ background: "var(--c-warn-bg)", border: "1px solid var(--c-warn)", color: "var(--c-warn)" }}>
            不常见组合(周次 × 事件线),请确认无误——此为软提示,不阻断。
          </div>
        )}
        {weekNo === 1 && <p className="muted">第 1 周为关系建立:无评分题,进入关系建立控制台。</p>}
      </div>

      <Button variant="primary" disabled={busy} onClick={submit}>{busy ? "创建中…" : "开始场次"}</Button>
    </div>
  );
}
