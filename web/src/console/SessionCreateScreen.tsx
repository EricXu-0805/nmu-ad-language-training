import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
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
  const [supportedTrainingWeeks, setSupportedTrainingWeeks] = useState<number[]>([2]);
  const [bankQcStatus, setBankQcStatus] = useState<string>("unknown");
  const [researchReady, setResearchReady] = useState(false);
  const [bankErr, setBankErr] = useState<string | null>(null);
  const [retryBank, setRetryBank] = useState(0);
  const [busy, setBusy] = useState(false);

  // 后端题库版本断言(不等则 fail-closed 不放行)
  useEffect(() => {
    setBankVersion(null);
    setBankErr(null);
    setBankQcStatus("unknown");
    setResearchReady(false);
    api.itemBank().then((b) => {
      if (b.errors.length > 0) throw new Error(`题库校验失败：${b.errors.join("；")}`);
      // 兼容旧后端缺字段时仅承认既有第 2 周；绝不把 3–8 周按猜测放行。
      const raw = b.supported_training_weeks ?? [2];
      const weeks = [...new Set(raw.filter((w) => Number.isInteger(w) && w >= 2 && w <= 8))].sort((a, z) => a - z);
      setSupportedTrainingWeeks(weeks);
      setBankQcStatus(b.qc_status ?? "unknown");
      setResearchReady(b.ready_for_research === true);
      setBankVersion(b.version_id);
    }).catch((e) => setBankErr(e instanceof Error ? e.message : "题库信息获取失败"));
  }, [retryBank]);

  const weekSupported = weekNo === 1 || supportedTrainingWeeks.includes(weekNo);
  const bankQcLabel = bankQcStatus === "draft" ? "草稿" : bankQcStatus === "unknown" ? "待核对" : bankQcStatus;

  const oddCombo =
    (weekNo === 1 && phase !== "关系建立") ||
    (weekNo >= 2 && eventLine === "关系建立环节");

  async function submit() {
    if (!pid.trim()) { toast("请填写受试者研究编号", "warn"); return; }
    if (!phase || !eventLine) { toast("请选择训练阶段与任务类型", "warn"); return; }
    if (!bankVersion) { toast("内容版本尚未确认，暂不能建立场次", "warn"); return; }
    if (!weekSupported) { toast(`第 ${weekNo} 周内容尚未完成结构化与双人冻结，当前不可开场`, "warn"); return; }
    setBusy(true);
    try {
      await api.getPatient(pid); // 患者须已建档
      const s: Session = {
        session_id: sid, patient_id: pid, week_no: weekNo,
        phase_type: phase, event_line: eventLine, item_bank_version_id: bankVersion,
      };
      const created = await api.createSession(s);
      toast(`场次 ${created.session_id} 已建立`, "ok");
      onStarted(created);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // 场次已存在(如误清屏后想续做):直接取回进入,不再是撞墙
        try {
          const existing = await api.getTrainSession(sid);
          toast(`场次 ${sid} 已存在，已进入继续操作`, "ok");
          onStarted(existing);
        } catch { toast("场次已存在，但读取失败，请重试", "danger"); }
      }
      else if (e instanceof ApiError && e.status === 404) toast("未找到该受试者档案，请先完成建档", "danger");
      else toast(e instanceof ApiError ? e.detail : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">步骤 2 / 4 · 建立场次</p>
          <h2 className="page-title">设置本次训练安排</h2>
          <p className="page-description">确认受试者、周次和任务类型。系统会把本场次与当前题库版本固定绑定，避免后续内容混用。</p>
        </div>
        <StatusPill tone={researchReady ? "ok" : bankVersion ? "warn" : "muted"}>
          {researchReady ? "研究可用" : bankVersion ? "仅限模拟" : "正在核对内容"}
        </StatusPill>
      </header>

      <div className="form-layout">
        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>受试者与场次</h3>
              <p className="muted">场次编号已自动生成；仅在恢复既有场次时修改。</p>
            </div>
          </div>
          <div className="form-grid">
            <Field label="受试者研究编号" required>
              <TextInput value={pid} onChange={(e) => setPid(e.target.value)} required />
            </Field>
            <Field label="场次编号" hint="输入已存在的场次编号，可继续该场次" required>
              <TextInput value={sid} onChange={(e) => setSid(e.target.value)} required />
            </Field>
          </div>
        </section>

        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>训练安排</h3>
              <p className="muted">周次、训练阶段和任务类型会分别保存；请按研究计划逐项确认。</p>
            </div>
            <StatusPill tone={!bankVersion ? "muted" : weekSupported ? "ok" : "danger"}>
              {!bankVersion ? "正在核对周次" : weekSupported ? `第 ${weekNo} 周可运行` : `第 ${weekNo} 周未开放`}
            </StatusPill>
          </div>
          <div className="form-grid">
            <Field label="训练周次（1–8 周）" required>
              <TextInput type="number" min={1} max={8} value={weekNo} required
                onChange={(e) => setWeekNo(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} />
            </Field>
            <Field label="训练阶段" hint="显式记录，不由周次自动推断" required>
              <EnumSelect options={PHASE_TYPES} value={phase} onChange={(v) => setPhase(v as PhaseType)} placeholder="请选择训练阶段" required />
            </Field>
            <Field label="任务类型" required>
              <EnumSelect options={EVENT_LINES} value={eventLine} onChange={(v) => setEventLine(v as EventLine)} placeholder="请选择任务类型" required />
            </Field>
          </div>

          {oddCombo && (
            <Alert tone="warn" title="周次与任务类型需要复核">
              当前组合不属于常规训练安排。系统暂不阻断，请确认这是研究计划要求。
            </Alert>
          )}
          {weekNo === 1 && <p className="muted">第 1 周将进入关系建立流程，不显示训练评分。</p>}
          {!weekSupported && weekNo >= 3 && (
            <Alert tone="danger" title={`第 ${weekNo} 周尚不可开场`}>
              材料已收到，但尚未完成结构化与双人冻结。系统已保持关闭，避免误用其他周题目。
            </Alert>
          )}
          {!weekSupported && weekNo === 2 && (
            <Alert tone="danger" title="当前内容服务未开放第 2 周">
              题库尚未声明第 2 周已完成结构化与冻结，系统已保持关闭。
            </Alert>
          )}
        </section>

        <section className="form-section">
          <div className="form-section-header">
            <div>
              <h3>内容与质控检查</h3>
              <p className="muted">题库状态以本机后端返回结果为准；检查未完成时不能建立场次。</p>
            </div>
          </div>
          <div className="session-context-bar">
            <div className="session-context-item grow">
              <span className="muted">当前题库版本</span>
              {bankVersion ? <strong className="mono">{bankVersion}</strong> : <strong>尚未确认</strong>}
            </div>
            <div className="session-context-item grow">
              <span className="muted">已接入流程</span>
              <strong>{bankVersion ? `第 1 周${supportedTrainingWeeks.map((w) => `、第 ${w} 周`).join("") || ""}` : "等待内容检查"}</strong>
            </div>
            <StatusPill tone={researchReady ? "ok" : bankVersion ? "warn" : "muted"}>质控：{bankQcLabel}</StatusPill>
          </div>

          {bankErr && (
            <Alert tone="danger" title="题库信息获取失败"
              actions={<Button onClick={() => setRetryBank((n) => n + 1)}>重新检查</Button>}>
              {bankErr}
            </Alert>
          )}
          {!bankVersion && !bankErr && <StatusPill tone="muted">正在获取题库版本…</StatusPill>}

          {bankVersion && !researchReady && (
            <Alert tone="warn" title="当前仅限开发与模拟预演">
              题库质控状态为“{bankQcLabel}”，不得用于真实受试者入组或正式数据采集。
            </Alert>
          )}
        </section>
      </div>

      <div className="form-actions">
        <p className="muted grow">建立后将进入{weekNo === 1 ? "关系建立" : "训练"}工作台。</p>
        <Button variant="primary" disabled={busy || !bankVersion || !weekSupported} onClick={submit}>
          {busy ? "正在建立场次…" : !bankVersion ? "等待内容检查…" : weekSupported ? (researchReady ? "建立场次并开始" : "建立模拟场次") : "当前周次不可开场"}
        </Button>
      </div>
    </div>
  );
}
