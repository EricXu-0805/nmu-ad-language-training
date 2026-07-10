import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { blobStore } from "../audio/blobStore";
import { Button } from "../components/Button";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { useSessionJournal } from "../hooks/useSessionJournal";
import { pct } from "../lib/format";
import type { AudioAsset, ExportResult, ScoreReconstruction, Session } from "../types";

// 场次收尾:评分只读重建 + 音频删除闸门 + 去标识导出。
export function SessionWrapupScreen({ session }: { session: Session }) {
  const toast = useToast();
  const { journal } = useSessionJournal(session.session_id);
  const [scores, setScores] = useState<ScoreReconstruction | null>(null);
  const [exp, setExp] = useState<ExportResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.sessionScores(session.session_id).then(setScores).catch((e) => toast(String(e), "danger")); }, [session, toast]);

  async function doExport() {
    setBusy(true);
    try {
      const r = await api.exportSession(session.session_id, true); // 只暴露去标识通道
      setExp(r);
      toast(`已导出批次 ${r.batch_id}(去标识)`, "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setBusy(false); }
  }

  const audioIds = Object.keys(journal.audios);

  return (
    <div className="col" style={{ maxWidth: 820 }}>
      <h2>场次收尾 · {session.session_id}</h2>

      <div className="card col">
        <h3>过程性评分(从已锁定环节值重建 · 单一事实源)</h3>
        {!scores ? <p>加载中…</p> : (
          <>
            <div className="wrap">
              <ScoreCard title="单要素" summary={scores.single} metric="naming_accuracy" fmt={pct} />
              <ScoreCard title="双要素" summary={scores.double} metric="weekly_de_score_percentile" />
              <ScoreCard title="多要素" summary={scores.multi} metric="weekly_me_score_percentile" />
            </div>
            {scores.excluded_items.length > 0 && (
              <div className="card" style={{ background: "var(--c-warn-bg)", border: "1px solid var(--c-warn)", color: "var(--c-warn)" }}>
                <strong>{scores.excluded_items.length} 项未计入(有未锁定环节)</strong>
                <ul style={{ margin: "8px 0 0", paddingLeft: 20 }}>{scores.excluded_items.map((x, i) => <li key={i}>{x}</li>)}</ul>
                <p className="muted">不阻断导出,但建议回训练屏补锁后再导。</p>
              </div>
            )}
          </>
        )}
      </div>

      <div className="card col">
        <h3>音频删除闸门(护栏1 · 未过闸门不出现删除钮)</h3>
        {audioIds.length === 0 ? <p className="muted">本场次暂无登记音频。</p> :
          audioIds.map((id) => <AudioGateRow key={id} rawAudioId={id} />)}
      </div>

      <div className="card col">
        <h3>去标识化导出</h3>
        <p className="muted">默认走去标识通道(不带直接标识符)。导出会触发音频闸门第一关(recorded→exported)。</p>
        <Button variant="primary" disabled={busy} onClick={doExport}>{busy ? "导出中…" : "导出本场次(去标识)"}</Button>
        {exp && (
          <div className="card" style={{ background: "var(--c-bg)" }}>
            <div className="row wrap"><StatusPill tone="ok">批次 {exp.batch_id}</StatusPill><StatusPill tone={exp.deidentified ? "ok" : "danger"}>{exp.deidentified ? "已去标识" : "含标识"}</StatusPill></div>
            <p>生成表:{Object.entries(exp.sheet_counts).map(([k, v]) => `${k}(${v})`).join("、")}</p>
            <p className="muted">文件:{exp.files.length} 个 · 触发导出音频:{exp.audio_touched.length} 条</p>
            {exp.excluded_items.length > 0 && <p style={{ color: "var(--c-warn)" }}>{exp.excluded_items.length} 项未计入评分</p>}
          </div>
        )}
      </div>
    </div>
  );
}

function ScoreCard({ title, summary, metric, fmt }: { title: string; summary: Record<string, unknown> | null; metric: string; fmt?: (n: number) => string }) {
  const v = summary ? (summary[metric] as number | undefined) : undefined;
  return (
    <div className="card" style={{ minWidth: 200 }}>
      <div className="muted">{title}</div>
      {summary ? (
        <div style={{ fontSize: "1.8em", fontWeight: 700 }}>{v == null ? "—" : fmt ? fmt(v) : `${v.toFixed(1)}%`}</div>
      ) : <div className="muted">无已锁定题</div>}
    </div>
  );
}

// 四步闸门:recorded→exported→checksum_verified→(信度)reliability_review_done→deletable→deleted
function AudioGateRow({ rawAudioId }: { rawAudioId: string }) {
  const toast = useToast();
  const [a, setA] = useState<AudioAsset | null>(null);

  const refresh = useCallback(() => { api.getAudio(rawAudioId).then(setA).catch(() => setA(null)); }, [rawAudioId]);
  useEffect(() => { refresh(); }, [refresh]);

  async function step(fn: () => Promise<AudioAsset>) {
    try { setA(await fn()); toast("闸门状态已推进", "ok"); }
    catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }
  async function remove() {
    try {
      await api.deleteAudio(rawAudioId);
      await blobStore.del(rawAudioId); // 服务端放行后才镜像删本地字节
      toast("音频已删除(服务端放行 + 本地字节清除)", "ok");
      refresh();
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  if (!a) return <div className="row muted">{rawAudioId}:未在服务端登记</div>;
  const tone = a.status === "deletable" ? "ok" : a.status === "deleted" ? "muted" : "primary";
  return (
    <div className="row" style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line)", paddingBottom: 8 }}>
      <div className="col" style={{ gap: 2 }}>
        <span className="mono">{rawAudioId.slice(0, 16)}…</span>
        <span className="muted">{a.is_reliability_sample ? "信度样本" : "常规"}{a.contains_direct_identifier ? " · 含直接标识" : ""}</span>
      </div>
      <div className="row wrap">
        <StatusPill tone={tone}>{a.status}</StatusPill>
        {a.status === "recorded" && <Button onClick={() => step(() => api.audioExport(rawAudioId))}>导出</Button>}
        {a.status === "exported" && <Button onClick={() => step(() => api.audioChecksum(rawAudioId))}>校验</Button>}
        {a.status === "checksum_verified" && a.is_reliability_sample && <Button onClick={() => step(() => api.audioReliabilityReview(rawAudioId))}>信度复核</Button>}
        {a.status === "deletable" && <Button variant="danger" onClick={remove}>删除</Button>}
      </div>
    </div>
  );
}
