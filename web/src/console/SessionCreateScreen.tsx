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
  // null=还没从后端拿到:拿到前不放行建场次。硬编码回退会让场次带着未经证实的版本建出,
  // 错误要到训练屏三方断言才爆——暴露点比发生点晚一整屏。
  const [bankVersion, setBankVersion] = useState<string | null>(null);
  const [bankErr, setBankErr] = useState(false);
  const [retryBank, setRetryBank] = useState(0);
  const [busy, setBusy] = useState(false);

  // 后端题库版本断言(不等则 fail-closed 不放行)
  useEffect(() => {
    setBankErr(false);
    api.itemBank().then((b) => setBankVersion(b.version_id)).catch(() => setBankErr(true));
  }, [retryBank]);

  const oddCombo =
    (weekNo === 1 && phase !== "关系建立") ||
    (weekNo >= 2 && eventLine === "关系建立环节");

  async function submit() {
    if (!pid.trim()) { toast("请填写受试者编号", "warn"); return; }
    if (!phase || !eventLine) { toast("请选择 phase_type 与 event_line", "warn"); return; }
    if (!bankVersion) { toast("题库版本尚未确认,暂不能建场次", "warn"); return; }
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
      if (e instanceof ApiError && e.status === 409) {
        // 场次已存在(如误清屏后想续做):直接取回进入,不再是撞墙
        try {
          const existing = await api.getTrainSession(sid);
          toast(`场次 ${sid} 已存在,直接进入续做`, "ok");
          onStarted(existing);
        } catch { toast("场次已存在但取回失败,请重试", "danger"); }
      }
      else if (e instanceof ApiError && e.status === 404) toast("该受试者未建档,请先建档", "danger");
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
        <Field label="场次编号 session_id" hint="填已存在的编号=续做该场次"><TextInput value={sid} onChange={(e) => setSid(e.target.value)} /></Field>
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
          <div className="row wrap">
            {bankVersion ? <StatusPill tone="primary">🔒 {bankVersion}</StatusPill>
              : bankErr ? (<><StatusPill tone="danger">题库版本获取失败</StatusPill><Button onClick={() => setRetryBank((n) => n + 1)}>重试</Button></>)
              : <StatusPill tone="muted">获取题库版本中…</StatusPill>}
          </div>
        </Field>

        {oddCombo && (
          <div className="card" style={{ background: "var(--c-warn-bg)", border: "1px solid var(--c-warn)", color: "var(--c-warn)" }}>
            不常见组合(周次 × 事件线),请确认无误——此为软提示,不阻断。
          </div>
        )}
        {weekNo === 1 && <p className="muted">第 1 周为关系建立:无评分题,进入关系建立控制台。</p>}
      </div>

      <Button variant="primary" disabled={busy || !bankVersion} onClick={submit}>
        {busy ? "创建中…" : bankVersion ? "开始场次" : "等待题库版本…"}
      </Button>
    </div>
  );
}
