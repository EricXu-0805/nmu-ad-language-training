import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Button } from "../components/Button";
import { EnumSelect, Field, TextInput } from "../components/Field";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { SCALE_PHASES } from "../types";
import type { ScaleResult } from "../types";

// 前后测量表录入抽屉(M2 必做:写 scale_result)。量表选型待 PI,scale_name 自由填(CETI/CADL 等)。
// 顶栏随时可开:前测在建场次前录、后测在收尾后录,同一入口。
export function ScaleDrawer({ patientId, open, onClose }: {
  patientId: string;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const [phase, setPhase] = useState<string | null>("前测");
  const [scaleName, setScaleName] = useState("");
  const [subscale, setSubscale] = useState("");
  const [score, setScore] = useState("");
  const [assessor, setAssessor] = useState("");
  const [rows, setRows] = useState<ScaleResult[]>([]);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api.listScales(patientId).then(setRows).catch(() => setRows([]));
  }, [patientId]);
  useEffect(() => { if (open) refresh(); }, [open, refresh]);

  if (!open) return null;

  async function submit() {
    if (!phase || !scaleName.trim()) { toast("请选相位并填量表名", "warn"); return; }
    const scoreNum = score.trim() === "" ? null : Number(score);
    if (scoreNum !== null && Number.isNaN(scoreNum)) { toast("分数须为数字(或留空)", "warn"); return; }
    setBusy(true);
    try {
      await api.createScale(patientId, {
        phase_type: phase, scale_name: scaleName.trim(),
        subscale: subscale.trim() || null, score: scoreNum, assessor_id: assessor.trim() || null,
      });
      toast("量表结果已录入", "ok");
      setScaleName(""); setSubscale(""); setScore("");
      refresh();
    } catch (e) {
      toast(e instanceof ApiError ? e.detail : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.35)", zIndex: 900, display: "flex", justifyContent: "flex-end" }}>
      <div className="col fade-in" onClick={(e) => e.stopPropagation()} style={{ width: 460, maxWidth: "92vw", height: "100%", background: "var(--c-bg)", padding: "var(--sp-5)", overflowY: "auto" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>量表录入 · {patientId}</h2>
          <Button onClick={onClose}>关闭</Button>
        </div>
        <p className="muted">前/后测/随访结果写入 scale_result 容器;量表选型待 PI,名称先自由填(如 CETI、CADL)。</p>

        <div className="card col">
          <Field label="相位">
            <EnumSelect options={SCALE_PHASES} value={phase} onChange={setPhase} />
          </Field>
          <Field label="量表名 scale_name">
            <TextInput value={scaleName} onChange={(e) => setScaleName(e.target.value)} placeholder="如 CETI / CADL / 未训练词命名" />
          </Field>
          <Field label="分量表 subscale(可选)">
            <TextInput value={subscale} onChange={(e) => setSubscale(e.target.value)} />
          </Field>
          <Field label="分数 score(可留空)">
            <TextInput inputMode="decimal" value={score} onChange={(e) => setScore(e.target.value)} placeholder="如 42" />
          </Field>
          <Field label="评估者 assessor_id(可选)">
            <TextInput value={assessor} onChange={(e) => setAssessor(e.target.value)} placeholder="如 A1(勿写真名)" />
          </Field>
          <Button variant="primary" disabled={busy} onClick={submit}>{busy ? "录入中…" : "录入"}</Button>
        </div>

        <div className="card col">
          <h3>已录 {rows.length} 条</h3>
          {rows.length === 0 && <p className="muted">暂无量表结果。</p>}
          {rows.map((r) => (
            <div key={r.id} className="row" style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line)", paddingBottom: 6 }}>
              <span><StatusPill tone="primary">{r.phase_type}</StatusPill> {r.scale_name}{r.subscale ? ` · ${r.subscale}` : ""}</span>
              <strong className="mono">{r.score ?? "—"}</strong>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
