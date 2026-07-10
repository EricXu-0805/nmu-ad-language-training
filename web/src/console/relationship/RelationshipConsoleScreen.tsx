import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/Toast";
import { useWeek1Script } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { newAudioId } from "../../lib/ids";
import { useCursorWriter } from "../../sync/useCursorWriter";
import type { Session } from "../../types";

// week1 关系建立驱动屏:plan 无评分题,本就无判分 UI(与 scoring 零 import)。
// 逐节广播 rapportStep 推进老人端;自我介绍段录音 contains_direct_identifier=true(导出侧整段红线)。
export function RelationshipConsoleScreen({ session, onWrapup }: { session: Session; onWrapup: () => void }) {
  const toast = useToast();
  const { script } = useWeek1Script();
  const { postSession, postRapport, resetSession } = useCursorWriter();
  const { upsertAudio } = useSessionJournal(session.session_id);
  const [sectionIdx, setSectionIdx] = useState(0);

  useEffect(() => {
    resetSession();
    postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "rapport", itemBankVersionId: session.item_bank_version_id });
  }, [session, postSession, resetSession]);

  if (!script) return <p>加载第 1 周脚本…</p>;
  const section = script.sections[sectionIdx];
  const isSelfIntro = section?.section_key?.includes("自我介绍");

  const broadcast = (idx: number) => {
    const s = script.sections[idx];
    postRapport({ sectionKey: s?.section_key ?? "", questionIdx: 0, recording: "idle" });
  };

  async function armSelfIntroAudio() {
    // 自我介绍段:登记含直接标识音频(姓名/年龄),导出侧 mask_text 整段红线。
    const rid = newAudioId();
    try {
      await api.createAudio({ raw_audio_id: rid, session_id: session.session_id, contains_direct_identifier: true });
      upsertAudio(rid, { turnKey: "关系建立·自我介绍", containsDirectIdentifier: true, isReliabilitySample: false, lastStatus: "recorded" });
      toast(`已登记自我介绍录音(含直接标识,导出将红线):${rid.slice(0, 12)}…`, "info");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  async function recordProxyNaming() {
    // 关系建立周"代说物品名"属允许(无警告),仅记介入。
    try {
      await api.recordAbnormal(session.session_id, { intervention_type: "代说物品名", note: "关系建立周·允许" });
      toast("已记录介入:代说物品名(关系建立周允许)", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  return (
    <div className="col" style={{ maxWidth: 720 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>关系建立 · 第 1 周</h2>
        <Button onClick={onWrapup}>场次收尾 →</Button>
      </div>
      <p className="muted">驱动固定话术分节推进老人端;本屏无判分、不采集评分。画像仅落老人端本地。</p>

      <div className="card col">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <StatusPill tone="primary">节 {sectionIdx + 1}/{script.sections.length}</StatusPill>
          <span>{section?.title ?? section?.section_key}</span>
        </div>
        {section?.lines && <ul>{section.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>}

        <div className="row wrap">
          {isSelfIntro && <Button onClick={armSelfIntroAudio}>登记自我介绍录音(含标识)</Button>}
          <Button onClick={recordProxyNaming}>记录:代说物品名(允许)</Button>
        </div>
      </div>

      <div className="row">
        <Button disabled={sectionIdx === 0} onClick={() => { const i = sectionIdx - 1; setSectionIdx(i); broadcast(i); }}>← 上一节</Button>
        <Button variant="primary" disabled={sectionIdx >= script.sections.length - 1}
          onClick={() => { const i = sectionIdx + 1; setSectionIdx(i); broadcast(i); }}>下一节 →</Button>
      </div>
    </div>
  );
}
