import { useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { useToast } from "../../components/Toast";
import { ABNORMAL_TYPES, CUE_INTERVENTIONS, INTERVENTION_TYPES } from "../../types";
import type { PhaseType } from "../../types";

// 记异常/介入。正式训练周的代说物品名/称呼 → 前端即时红条 + 预勾影响判分有效性(镜像后端强制)。
export function AbnormalDrawer({ sessionId, phaseType, currentItemEventId, open, onClose }: {
  sessionId: string;
  phaseType: PhaseType | null;
  currentItemEventId?: number | null;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const [intervention, setIntervention] = useState<string | null>(null);
  const [abnormal, setAbnormal] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const cueBreach = phaseType === "正式训练" && intervention != null &&
    (CUE_INTERVENTIONS as readonly string[]).includes(intervention);

  if (!open) return null;

  async function submit() {
    if (!intervention && !abnormal) { toast("请选择异常类型或介入类型", "warn"); return; }
    setBusy(true);
    try {
      await api.recordAbnormal(sessionId, {
        item_event_id: currentItemEventId ?? null,
        abnormal_type: abnormal,
        intervention_type: intervention,
        affects_scoring_validity: cueBreach,   // 后端仍会对越界介入强制置位,这里前端先如实反映
        note: note || null,
      });
      toast("已记录异常/介入", "ok");
      setIntervention(null); setAbnormal(null); setNote("");
      onClose();
    } catch (e) {
      toast(e instanceof ApiError ? e.detail : String(e), "danger");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.35)", zIndex: 900, display: "flex", justifyContent: "flex-end" }}>
      <div className="col" style={{ width: 460, maxWidth: "92vw", height: "100%", background: "var(--c-bg)", padding: "var(--sp-5)", overflowY: "auto" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>记录异常 / 介入</h2>
          <Button onClick={onClose}>关闭</Button>
        </div>
        <p className="muted">当前相位:{phaseType ?? "—"}{currentItemEventId ? ` · 关联题事件 #${currentItemEventId}` : " · 未关联具体题"}</p>

        <Field label="介入类型(研究者对训练过程的介入)">
          <EnumSelect options={INTERVENTION_TYPES} value={intervention} onChange={setIntervention} />
        </Field>
        <Field label="异常类型(受试者/环境异常)">
          <EnumSelect options={ABNORMAL_TYPES} value={abnormal} onChange={setAbnormal} />
        </Field>

        {cueBreach && (
          <div className="card" style={{ background: "var(--c-danger-bg)", border: "2px solid var(--c-danger)", color: "var(--c-danger)" }}>
            正式训练周「{intervention}」属线索性越界介入 → 将标记<strong>影响该题判分有效性</strong>(后端强制,不可撤销)。
          </div>
        )}

        <Field label="备注(可选)">
          <TextInput value={note} onChange={(e) => setNote(e.target.value)} placeholder="现场情况说明" />
        </Field>

        <Button variant="primary" disabled={busy} onClick={submit}>{busy ? "记录中…" : "记录"}</Button>
      </div>
    </div>
  );
}
