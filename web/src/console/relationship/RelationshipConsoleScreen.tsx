import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/Toast";
import { useWeek1Script } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { useAudioSaved, useCursorWriter, useSaveWatchdog } from "../../sync/useCursorWriter";
import type { RecState } from "../../sync/messages";
import type { Session } from "../../types";

// week1 关系建立驱动屏:plan 无评分题,本就无判分 UI(与 scoring 零 import)。
// 逐节广播 rapportStep 推进老人端;录音同训练屏 arm/stop 模式(老人端 VOX 实采字节)。
// 自我介绍段 containsDirectIdentifier=true 随消息带到老人端登记(导出侧整段红线)。
export function RelationshipConsoleScreen({ session, onWrapup }: { session: Session; onWrapup: () => void }) {
  const toast = useToast();
  const { script } = useWeek1Script();
  const { postSession, postRapport, resetSession } = useCursorWriter();
  const { upsertAudio } = useSessionJournal(session.session_id);
  const [sectionIdx, setSectionIdx] = useState(0);
  const [recState, setRecState] = useState<RecState>("idle");
  const section = script?.sections[sectionIdx];
  const isSelfIntro = Boolean(section?.section_key?.includes("自我介绍"));
  const recSeq = useRef(0);
  const watchdog = useSaveWatchdog(() =>
    toast("8 秒未收到老人端录音回报——可能没录上(麦克风权限/网络)。请检查老人端后重新示意录音。", "danger"));

  useEffect(() => {
    resetSession();
    postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "rapport", itemBankVersionId: session.item_bank_version_id });
  }, [session, postSession, resetSession]);

  // 老人端录音落库回报 → 记入作业日志(收尾屏音频闸门据此列出)
  useAudioSaved((m) => {
    watchdog.clear();
    upsertAudio(m.rawAudioId, {
      turnKey: m.turnKey,
      containsDirectIdentifier: m.containsDirectIdentifier ?? false,
      isReliabilitySample: false,
      lastStatus: "recorded",
    });
    if (recState !== "idle") {
      // 老人自己按"我说好了"停的:把 idle 写回镜像/服务端真值源,否则 armed 残留会让老人端刷新后自动开麦。
      postRapport({ sectionKey: section?.section_key ?? "", questionIdx: 0, recording: "idle", recSeq: recSeq.current });
    }
    setRecState("idle");
    toast(`录音已保存${m.containsDirectIdentifier ? "(含直接标识,导出将红线)" : ""}:${m.rawAudioId.slice(0, 12)}… · ${m.durationSeconds.toFixed(1)}s`, "ok");
  });

  if (!script) return <p>加载第 1 周脚本…</p>;

  const broadcast = (idx: number) => {
    const s = script.sections[idx];
    if (recState !== "idle") watchdog.start(); // 跳节即停:老人端收到 idle 会自动收尾保存当前段,回报也要有人等
    setRecState("idle");
    postRapport({ sectionKey: s?.section_key ?? "", questionIdx: 0, recording: "idle", recSeq: recSeq.current });
  };

  const armRecording = () => {
    recSeq.current += 1; // 每次 arm 新序号:老人自停后 armed→armed 重发才能重触发老人端
    setRecState("armed");
    postRapport({ sectionKey: section?.section_key ?? "", questionIdx: 0, recording: "armed", recSeq: recSeq.current, containsDirectIdentifier: isSelfIntro });
  };
  const stopRecording = () => {
    setRecState("idle");
    watchdog.start();
    postRapport({ sectionKey: section?.section_key ?? "", questionIdx: 0, recording: "idle", recSeq: recSeq.current, containsDirectIdentifier: isSelfIntro });
  };

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
        {/* 收尾前必先停录:否则本屏卸载后无人发 idle,老人端麦克风持续开着 */}
        <Button onClick={() => { if (recState !== "idle") stopRecording(); onWrapup(); }}>场次收尾 →</Button>
      </div>
      <p className="muted">驱动固定话术分节推进老人端;本屏无判分、不采集评分。画像仅落老人端本地。</p>

      <div className="card col">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <StatusPill tone="primary">节 {sectionIdx + 1}/{script.sections.length}</StatusPill>
          <span>{section?.title ?? section?.section_key}</span>
        </div>
        {section?.lines && <ul>{section.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>}
        {isSelfIntro && <p className="muted">本节为自我介绍:录音将标"含直接标识",导出时整段红线。</p>}

        <div className="row wrap">
          {recState === "idle"
            ? <Button variant="primary" onClick={armRecording}>示意老人录音{isSelfIntro ? "(含标识)" : ""}</Button>
            : <Button variant="danger" onClick={stopRecording}>停止录音</Button>}
          {recState !== "idle" && <StatusPill tone="danger">录音中…</StatusPill>}
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
