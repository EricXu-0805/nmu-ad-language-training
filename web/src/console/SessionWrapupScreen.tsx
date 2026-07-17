import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { blobStore } from "../audio/blobStore";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { StatusPill } from "../components/StatusPill";
import { useToast } from "../components/ToastContext";
import { useSessionJournal } from "../hooks/useSessionJournal";
import { useAudioSaved } from "../sync/useCursorWriter";
import { ratioPct } from "../lib/format";
import type { AudioAsset, ExportResult, ScoreReconstruction, Session, SessionPlan } from "../types";

// 场次收尾:完整度计 + 评分只读重建 + 音频删除闸门 + 去标识导出。
export function SessionWrapupScreen({ session, onBack, onDone }: { session: Session; onBack?: () => void; onDone?: () => void }) {
  const toast = useToast();
  const { journal, upsertAudio, hydrateFromServer } = useSessionJournal(session.session_id);
  const [journalRecovery, setJournalRecovery] = useState<"loading" | "ready" | "error">("loading");
  const [journalRecoveryError, setJournalRecoveryError] = useState<string | null>(null);
  const [journalRetry, setJournalRetry] = useState(0);

  // 接住迟到的录音回报:录音中点了"场次收尾",老人端保存发生在训练/关系屏卸载之后——
  // 不接就成为音频闸门列不出的孤儿音频(DB 有行、本机有字节,却没人能走删除闸门)。
  useAudioSaved((m) => {
    // live state 会保留最近一条回报；别场次/旧协议缺 sessionId 的回报绝不能混入本场 journal。
    if (m.sessionId !== session.session_id) return;
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
  const [planErr, setPlanErr] = useState<string | null>(null);
  const [exp, setExp] = useState<ExportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmIncompleteExport, setConfirmIncompleteExport] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const [gateEpoch, setGateEpoch] = useState(0); // 导出/批量操作后驱动闸门各行重取状态

  useEffect(() => {
    let cancelled = false;
    setJournalRecovery("loading");
    setJournalRecoveryError(null);
    api.sessionJournal(session.session_id)
      .then((remote) => {
        if (cancelled) return;
        if (remote.session.session_id !== session.session_id) {
          throw new Error("服务器返回了其他场次的记录，已拒绝恢复");
        }
        hydrateFromServer(remote);
        setJournalRecovery("ready");
      })
      .catch((e) => {
        if (cancelled) return;
        setJournalRecoveryError(e instanceof ApiError ? e.detail : String(e));
        setJournalRecovery("error");
      });
    return () => { cancelled = true; };
  }, [hydrateFromServer, journalRetry, session.session_id]);

  useEffect(() => {
    setScoresErr(null);
    api.sessionScores(session.session_id).then(setScores).catch((e) => setScoresErr(e instanceof ApiError ? e.detail : String(e)));
  }, [session, retryNonce]);
  useEffect(() => {
    setPlan(null);
    setPlanErr(null);
    api.sessionPlan(session.session_id, session.week_no, session.event_line)
      .then(setPlan)
      .catch((e) => setPlanErr(e instanceof ApiError ? e.detail : String(e)));
  }, [session, retryNonce]);

  // 完整度:excluded_items 只列"已建未锁"的题;未动过的计划题要靠 plan 对照日志才可见。
  const lockedTurns = Object.values(journal.turns).filter((t) => t.locked).length;
  const untouched = (plan?.items ?? []).filter((it) => !journal.itemEvents[it.item_id]);

  async function doExport() {
    if (journalRecovery !== "ready") {
      toast("服务器记录尚未完整恢复，导出保持关闭", "danger");
      return;
    }
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
  const journalReady = journalRecovery === "ready";
  const planReady = plan !== null;
  const totalTurns = plan?.total_turns ?? 0;
  const completionPct = planReady ? (totalTurns > 0 ? Math.round((lockedTurns / totalTurns) * 100) : 100) : null;
  const incomplete = planReady && totalTurns > 0 && lockedTurns < totalTurns;
  const untouchedGroups = ["单要素", "双要素", "多要素"].map((taskType) => ({
    taskType,
    items: untouched.filter((item) => item.task_type === taskType),
  })).filter((group) => group.items.length > 0);
  const planHas = (taskType: string) => plan == null || plan.items.some((item) => item.task_type === taskType);

  // 30+ 条音频逐条点"校验"太折磨:一键顺序校验全部 exported 态音频
  const [verifyingAll, setVerifyingAll] = useState(false);
  async function verifyAll() {
    if (verifyingAll || !journalReady) return;
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
    <div className="page-shell page-shell--medium">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">步骤 4 / 4 · 场次收尾</p>
          <h2 className="page-title">核对并导出本场次</h2>
          <p className="page-description">先核对训练完整度与评分，再统一导出本场数据。研究录音只有通过导出、校验和信度复核后才可能删除。</p>
        </div>
        <div className="toolbar">
          <StatusPill tone={journalRecovery === "error" ? "danger" : !journalReady || !planReady ? "muted" : incomplete ? "warn" : "ok"}>
            {journalRecovery === "error" ? "记录恢复失败" : !journalReady || !planReady ? "正在核对记录" : incomplete ? "仍有项目待完成" : "训练记录完整"}
          </StatusPill>
          {/* 收尾不再是单行道:看到漏锁随时回去补 */}
          {onBack && <Button onClick={onBack}>返回{session.week_no === 1 ? "关系建立" : "训练"}</Button>}
          {onDone && <Button variant="primary" onClick={onDone}>完成收尾,切换下一位</Button>}
        </div>
      </header>

      {journalRecovery === "error" && (
        <Alert tone="danger" title="无法恢复服务器中的完整场次记录"
          actions={<Button onClick={() => setJournalRetry((n) => n + 1)}>重新加载记录</Button>}>
          {journalRecoveryError}。恢复完成前系统不会开放导出，也不会显示可能不完整的录音闸门清单。
        </Alert>
      )}
      {journalRecovery === "loading" && (
        <Alert tone="info" title="正在恢复服务器记录">
          正在合并已提交的环节、评分与音频；完成前导出保持关闭。
        </Alert>
      )}

      <section className="form-section">
        <div className="form-section-header">
          <div>
            <h3>训练完成情况</h3>
            <p className="muted">完成度按已人工锁定的训练环节计算。</p>
          </div>
          <StatusPill tone={!journalReady || !planReady ? "muted" : incomplete ? "warn" : "ok"}>
            {!journalReady || completionPct == null ? "正在计算" : `${completionPct}% 完成`}
          </StatusPill>
        </div>
        <div className="metric-grid">
          <div className="metric-card">
            <span className="metric-card__label">已锁定环节</span>
            <strong className="metric-card__value">{lockedTurns}<small> / {totalTurns || "—"}</small></strong>
          </div>
          <div className="metric-card">
            <span className="metric-card__label">未开始题目</span>
            <strong className="metric-card__value">{untouched.length}<small> / {plan?.total_items ?? "—"}</small></strong>
          </div>
          <div className="metric-card">
            <span className="metric-card__label">已登记录音</span>
            <strong className="metric-card__value">{audioIds.length}<small> 条</small></strong>
          </div>
        </div>

        {planErr && (
          <Alert tone="danger" title="无法核对场次计划"
            actions={<Button onClick={() => setRetryNonce((n) => n + 1)}>重新加载</Button>}>
            {planErr}。完整度未知时系统不会开放导出，请先恢复本机服务连接。
          </Alert>
        )}

        {journalReady && untouched.length > 0 && (
          <Alert tone="warn" title={`有 ${untouched.length} 题尚未开始`}
            actions={onBack ? <Button onClick={onBack}>返回训练补齐</Button> : undefined}>
            建议返回训练工作台补齐；如仍需导出，导出文件会明确标记未计入的内容。
            <details style={{ marginTop: 12 }}>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>查看未开始题目清单</summary>
              {untouchedGroups.map((group) => (
                <div key={group.taskType} style={{ marginTop: 10 }}>
                  <strong>{group.taskType} · {group.items.length} 题</strong>
                  <ul>{group.items.map((item) => <li key={item.item_id} className="mono">{item.item_id}</li>)}</ul>
                </div>
              ))}
            </details>
          </Alert>
        )}
      </section>

      <section className="form-section">
        <div className="form-section-header">
          <div>
            <h3>本场评分结果</h3>
            <p className="muted">只汇总已由研究者人工确认并锁定的环节。</p>
          </div>
        </div>
        {scoresErr ? (
          <div className="row wrap">
            <span style={{ color: "var(--c-danger)" }}>评分结果加载失败：{scoresErr}</span>
            <Button onClick={() => setRetryNonce((n) => n + 1)}>重新加载</Button>
          </div>
        ) : !scores ? <p className="muted">正在加载评分结果…</p> : (
          <>
            <div className="metric-grid">
              {planHas("单要素") && <ScoreCard title="单要素命名正确率" summary={scores.single} metric="naming_accuracy" fmt={ratioPct} />}
              {planHas("双要素") && <ScoreCard title="双要素周得分" summary={scores.double} metric="weekly_de_score_percentile" />}
              {planHas("多要素") && <ScoreCard title="多要素关键要素率" summary={scores.multi} metric="weekly_me_score_percentile" />}
            </div>
            {scores.excluded_items.length > 0 && (
              <Alert tone="warn" title={`${scores.excluded_items.length} 项未计入评分`}>
                这些项目仍有环节未锁定。导出不会被阻断，但文件会保留该缺失标记。
                <details style={{ marginTop: 8 }}>
                  <summary style={{ cursor: "pointer", fontWeight: 600 }}>查看未计入项目</summary>
                  <ul>{scores.excluded_items.map((x, i) => <li key={i}>{x}</li>)}</ul>
                </details>
              </Alert>
            )}
          </>
        )}
      </section>

      <section className="form-section">
        <div className="form-section-header">
          <div>
            <h3>数据导出与录音保护</h3>
            <p className="muted">必须按顺序推进；未通过全部闸门的录音不会显示删除操作。</p>
          </div>
        </div>
        <div className="workflow-stepper" aria-label="数据导出与录音处理顺序">
          <div className={`workflow-step${!journalReady || !planReady ? " is-active" : !incomplete ? " is-complete" : ""}`}><span>1</span><span>核对完整度</span></div>
          <div className={`workflow-step${exp ? " is-complete" : journalReady && planReady ? " is-active" : ""}`}><span>2</span><span>整场去标识导出</span></div>
          <div className={`workflow-step${exp ? " is-active" : ""}`}><span>3</span><span>校验录音副本</span></div>
          <div className="workflow-step"><span>4</span><span>信度复核后删除</span></div>
        </div>

        {journalReady && incomplete && (
          <Alert tone="warn" title="当前导出将是不完整场次">
            仍有未锁定环节。请优先返回训练补齐；如研究流程要求此刻导出，缺失项会写入导出结果。
          </Alert>
        )}

        <div className="form-actions">
          <p className="muted grow">系统只提供去标识导出，不包含直接身份信息；整场音频会进入同一受控批次。</p>
          <Button variant="primary" disabled={busy || !planReady || !journalReady}
            onClick={() => incomplete ? setConfirmIncompleteExport(true) : void doExport()}>
            {busy ? "正在生成导出批次…" : !journalReady ? "等待服务器记录恢复…" : !planReady ? "等待完成度检查…" : incomplete ? "导出当前已完成数据" : "导出本场次（去标识）"}
          </Button>
        </div>
        {exp && (
          <div className="card" style={{ background: "var(--c-bg)" }}>
            <div className="row wrap"><StatusPill tone="ok">导出批次 {exp.batch_id}</StatusPill><StatusPill tone={exp.deidentified ? "ok" : "danger"}>{exp.deidentified ? "已去标识" : "包含标识信息"}</StatusPill></div>
            <p>已生成：{Object.entries(exp.sheet_counts).map(([k, v]) => `${k}（${v}）`).join("、")}</p>
            <p className="muted">共 {exp.files.length} 个文件 · 纳入 {exp.audio_touched.length} 条研究录音</p>
            {exp.excluded_items.length > 0 && <p style={{ color: "var(--c-warn)" }}>{exp.excluded_items.length} 项因未锁定而未计入评分</p>}
          </div>
        )}

        <div className="form-section-header" style={{ marginTop: 8 }}>
          <div>
            <h3>研究录音状态</h3>
            <p className="muted">校验针对本次导出的受控副本；原始录音不能逐条绕过整场导出。</p>
          </div>
          {journalReady && audioIds.length > 1 && (
            <Button onClick={verifyAll} disabled={verifyingAll || !journalReady}>{verifyingAll ? "正在批量校验…" : "校验全部已导出录音"}</Button>
          )}
        </div>
        {!journalReady ? <p className="muted">完整录音清单恢复后才会显示。</p> : audioIds.length === 0 ? <p className="muted">本场次没有登记研究录音，无需执行录音闸门。</p> :
          audioIds.map((id) => <AudioGateRow key={id} rawAudioId={id} gateEpoch={gateEpoch} />)}
      </section>

      <ConfirmDialog open={confirmIncompleteExport} title="导出不完整场次？"
        body={`当前仅锁定 ${lockedTurns}/${totalTurns} 个环节。导出文件会保留缺失标记，建议先返回训练补齐；只有研究流程确需中途留档时才继续。`}
        confirmLabel="仍要导出"
        onConfirm={() => { setConfirmIncompleteExport(false); void doExport(); }}
        onCancel={() => setConfirmIncompleteExport(false)} />
    </div>
  );
}

function ScoreCard({ title, summary, metric, fmt }: { title: string; summary: Record<string, unknown> | null; metric: string; fmt?: (n: number) => string }) {
  const v = summary ? (summary[metric] as number | undefined) : undefined;
  return (
    <div className="metric-card">
      <span className="metric-card__label">{title}</span>
      {summary ? (
        <strong className="metric-card__value">{v == null ? "—" : fmt ? fmt(v) : `${v.toFixed(1)}%`}</strong>
      ) : <span className="muted">暂无已锁定题</span>}
    </div>
  );
}

const audioStatusLabel: Record<AudioAsset["status"], string> = {
  recorded: "待整场导出",
  exported: "已导出，待校验",
  checksum_verified: "校验通过，待信度复核",
  reliability_review_done: "信度复核完成",
  deletable: "全部闸门通过",
  deleted: "已删除",
};

// 四步闸门:recorded 只能经完整 session export → exported→checksum_verified→(信度)reliability_review_done→deletable→deleted
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
        <StatusPill tone={tone}>{audioStatusLabel[a.status]}</StatusPill>
        {rowBusy && <span className="muted">处理中…</span>}
        {a.status === "recorded" && <span className="muted">请先执行整场去标识导出</span>}
        {a.status === "exported" && <Button disabled={rowBusy} onClick={() => step(() => api.audioChecksum(rawAudioId))}>校验导出副本</Button>}
        {a.status === "checksum_verified" && a.is_reliability_sample && <Button disabled={rowBusy} onClick={() => step(() => api.audioReliabilityReview(rawAudioId))}>完成人工信度复核</Button>}
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
