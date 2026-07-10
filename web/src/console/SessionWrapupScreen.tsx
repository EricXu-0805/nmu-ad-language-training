import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { blobStore } from "../audio/blobStore";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/Toast";
import { useSessionJournal } from "../hooks/useSessionJournal";
import { useAudioSaved } from "../sync/useCursorWriter";
import { ratioPct } from "../lib/format";
import type { AudioAsset, ExportResult, ScoreReconstruction, Session, SessionPlan } from "../types";

// 场次收尾:完整度计 + 评分只读重建 + 音频删除闸门 + 去标识导出。
export function SessionWrapupScreen({ session, onBack }: { session: Session; onBack?: () => void }) {
  const toast = useToast();
  const { journal, upsertAudio } = useSessionJournal(session.session_id);

  // 接住迟到的录音回报:录音中点了"场次收尾",老人端保存发生在训练/关系屏卸载之后——
  // 不接就成为音频闸门列不出的孤儿音频(DB 有行、本机有字节,却没人能走删除闸门)。
  useAudioSaved((m) => {
    if (journal.audios[m.rawAudioId]) return; // 轮询会重放最后一条回报:已记过的不算迟到
    upsertAudio(m.rawAudioId, {
      turnKey: m.turnKey,
      containsDirectIdentifier: m.containsDirectIdentifier ?? false,
      isReliabilitySample: false,
      lastStatus: "recorded",
    });
    toast(`补记迟到录音:${m.rawAudioId.slice(0, 12)}…(已列入音频闸门)`, "info");
  });
  const [scores, setScores] = useState<ScoreReconstruction | null>(null);
  const [scoresErr, setScoresErr] = useState<string | null>(null); // 失败≠加载中:给重试,不给永远的"加载中…"
  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [exp, setExp] = useState<ExportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const [gateEpoch, setGateEpoch] = useState(0); // 导出/批量操作后驱动闸门各行重取状态

  useEffect(() => {
    setScoresErr(null);
    api.sessionScores(session.session_id).then(setScores).catch((e) => setScoresErr(e instanceof ApiError ? e.detail : String(e)));
  }, [session, retryNonce]);
  useEffect(() => { api.sessionPlan(session.session_id, session.week_no, session.event_line).then(setPlan).catch(() => setPlan(null)); }, [session, retryNonce]);

  // 完整度:excluded_items 只列"已建未锁"的题;未动过的计划题要靠 plan 对照日志才可见。
  const lockedTurns = Object.values(journal.turns).filter((t) => t.locked).length;
  const untouched = (plan?.items ?? []).filter((it) => !journal.itemEvents[it.item_id]);

  async function doExport() {
    setBusy(true);
    try {
      const r = await api.exportSession(session.session_id, true); // 只暴露去标识通道
      setExp(r);
      setGateEpoch((n) => n + 1); // 导出把 recorded→exported:闸门各行立即重取,不显示过期按钮
      toast(`已导出批次 ${r.batch_id}(去标识)`, "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setBusy(false); }
  }

  const audioIds = Object.keys(journal.audios);

  // 30+ 条音频逐条点"校验"太折磨:一键顺序校验全部 exported 态音频
  const [verifyingAll, setVerifyingAll] = useState(false);
  async function verifyAll() {
    if (verifyingAll) return;
    setVerifyingAll(true);
    let ok = 0, fail = 0;
    for (const id of audioIds) {
      try { const a = await api.getAudio(id); if (a.status === "exported") { await api.audioChecksum(id); ok++; } }
      catch { fail++; }
    }
    setGateEpoch((n) => n + 1);
    toast(`批量校验完成:${ok} 通过${fail ? `,${fail} 失败(见各行状态)` : ""}`, fail ? "warn" : "ok");
    setVerifyingAll(false);
  }

  return (
    <div className="col" style={{ maxWidth: 820 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 style={{ margin: 0 }}>场次收尾 · {session.session_id}</h2>
        {/* 收尾不再是单行道:看到漏锁随时回去补 */}
        {onBack && <Button onClick={onBack}>← 回{session.week_no === 1 ? "关系建立" : "训练"}屏继续</Button>}
      </div>

      {plan && plan.total_turns > 0 && (
        <div className="card col">
          <h3>完整度</h3>
          <div className="row wrap">
            <StatusPill tone={lockedTurns >= plan.total_turns ? "ok" : "primary"}>
              已锁定 {lockedTurns} / 计划 {plan.total_turns} 环节
            </StatusPill>
            <StatusPill tone={untouched.length === 0 ? "ok" : "danger"}>
              未开始 {untouched.length} / {plan.total_items} 题
            </StatusPill>
          </div>
          {untouched.length > 0 && (
            <p className="muted" style={{ margin: 0 }}>
              未开始:{untouched.map((it) => `${it.item_id}(${it.task_type})`).join("、")} —— 建议回训练屏完成后再导出。
            </p>
          )}
        </div>
      )}

      <div className="card col">
        <h3>过程性评分(从已锁定环节值重建 · 单一事实源)</h3>
        {scoresErr ? (
          <div className="row wrap">
            <span style={{ color: "var(--c-danger)" }}>加载失败:{scoresErr}</span>
            <Button onClick={() => setRetryNonce((n) => n + 1)}>重试</Button>
          </div>
        ) : !scores ? <p>加载中…</p> : (
          <>
            <div className="wrap">
              <ScoreCard title="单要素命名正确率" summary={scores.single} metric="naming_accuracy" fmt={ratioPct} />
              <ScoreCard title="双要素周得分(百分制)" summary={scores.double} metric="weekly_de_score_percentile" />
              <ScoreCard title="多要素关键要素率" summary={scores.multi} metric="weekly_me_score_percentile" />
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
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>音频删除闸门(护栏1 · 未过闸门不出现删除钮)</h3>
          {audioIds.length > 1 && (
            <Button onClick={verifyAll} disabled={verifyingAll}>{verifyingAll ? "批量校验中…" : "校验全部已导出"}</Button>
          )}
        </div>
        {audioIds.length === 0 ? <p className="muted">本场次暂无登记音频。</p> :
          audioIds.map((id) => <AudioGateRow key={id} rawAudioId={id} gateEpoch={gateEpoch} />)}
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
function AudioGateRow({ rawAudioId, gateEpoch = 0 }: { rawAudioId: string; gateEpoch?: number }) {
  const toast = useToast();
  const [a, setA] = useState<AudioAsset | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ok" | "missing" | "error">("loading");
  const [rowBusy, setRowBusy] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);

  const refresh = useCallback(() => {
    setLoadState("loading");
    api.getAudio(rawAudioId)
      .then((x) => { setA(x); setLoadState("ok"); })
      .catch((e) => { setA(null); setLoadState(e instanceof ApiError && e.status === 404 ? "missing" : "error"); });
  }, [rawAudioId]);
  useEffect(() => { refresh(); }, [refresh, gateEpoch]);

  async function step(fn: () => Promise<AudioAsset>) {
    if (rowBusy) return; // 在途锁:慢网下连点不重复触发状态迁移
    setRowBusy(true);
    try { setA(await fn()); toast("闸门状态已推进", "ok"); }
    catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); refresh(); }
    finally { setRowBusy(false); }
  }
  async function remove() {
    if (rowBusy) return;
    setRowBusy(true);
    try {
      await api.deleteAudio(rawAudioId);
      await blobStore.del(rawAudioId); // 服务端放行后才镜像删本地字节
      toast("音频已删除(服务端放行 + 本地字节清除)", "ok");
      refresh();
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setRowBusy(false); }
  }

  if (loadState === "loading") return <div className="row muted">{rawAudioId.slice(0, 16)}…:读取状态中…</div>;
  if (loadState === "error") return <div className="row muted">{rawAudioId.slice(0, 16)}…:状态获取失败 <Button onClick={refresh}>重试</Button></div>;
  if (loadState === "missing" || !a) return <div className="row muted">{rawAudioId.slice(0, 16)}…:未在服务端登记</div>;
  const tone = a.status === "deletable" ? "ok" : a.status === "deleted" ? "muted" : "primary";
  return (
    <div className="row" style={{ justifyContent: "space-between", borderBottom: "1px solid var(--c-line-soft)", paddingBottom: 8 }}>
      <div className="col" style={{ gap: 2 }}>
        <span className="mono">{rawAudioId.slice(0, 16)}…</span>
        <span className="muted">{a.is_reliability_sample ? "信度样本" : "常规"}{a.contains_direct_identifier ? " · 含直接标识" : ""}</span>
      </div>
      <div className="row wrap">
        <StatusPill tone={tone}>{a.status}</StatusPill>
        {rowBusy && <span className="muted">处理中…</span>}
        {a.status === "recorded" && <Button disabled={rowBusy} onClick={() => step(() => api.audioExport(rawAudioId))}>导出</Button>}
        {a.status === "exported" && <Button disabled={rowBusy} onClick={() => step(() => api.audioChecksum(rawAudioId))}>校验</Button>}
        {a.status === "checksum_verified" && a.is_reliability_sample && <Button disabled={rowBusy} onClick={() => step(() => api.audioReliabilityReview(rawAudioId))}>信度复核</Button>}
        {/* 删除是唯一不可恢复动作:锁分都有确认层,删录音更要 */}
        {a.status === "deletable" && <Button variant="danger" disabled={rowBusy} onClick={() => setConfirmDel(true)}>删除</Button>}
      </div>
      <ConfirmDialog open={confirmDel} title="删除这条研究录音?"
        body={`${rawAudioId.slice(0, 16)}… 已过全部闸门(导出+校验${a.is_reliability_sample ? "+信度复核" : ""})。删除将同时清掉服务端与本机字节,不可恢复。`}
        confirmLabel="确认删除"
        onConfirm={() => { setConfirmDel(false); void remove(); }}
        onCancel={() => setConfirmDel(false)} />
    </div>
  );
}
