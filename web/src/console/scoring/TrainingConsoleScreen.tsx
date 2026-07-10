import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import { Button } from "../../components/Button";
import { EnumSelect, Field, TextInput } from "../../components/Field";
import { StatusPill } from "../../components/StatusPill";
import { useToast } from "../../components/Toast";
import { assertVersionsMatch, findDouble, findSingle, lookupCue, useItemBankBundle } from "../../content/bundle";
import { useSessionJournal } from "../../hooks/useSessionJournal";
import { useAudioSaved, useCursorWriter } from "../../sync/useCursorWriter";
import type { PlanItem, PlanTurn, Session, SessionPlan } from "../../types";
import { assertPortraitFree, isRelationRole } from "./vm";

// week≥2 判分主屏:左题目游标列 + 右环节工作卡(转写→确认→AI初评→锁分)。
// 唯一游标写者,推进老人端;★本文件及本目录禁 import 任何画像源(oxlint 守卫 + vm 运行时断言)。
export function TrainingConsoleScreen({ session, onWrapup }: { session: Session; onWrapup: () => void }) {
  const toast = useToast();
  const { bundle } = useItemBankBundle();
  const { journal, upsertItem, upsertTurn, upsertAudio, setCursor } = useSessionJournal(session.session_id);
  const { postSession, postCursor, resetSession } = useCursorWriter();

  const [plan, setPlan] = useState<SessionPlan | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [itemIdx, setItemIdx] = useState(journal.cursor.itemIdx);
  const [turnIdx, setTurnIdx] = useState(journal.cursor.turnIdx);
  const [aiEnabled, setAiEnabled] = useState(true);
  const [reviewerId, setReviewerId] = useState("R1");
  const [recordingAllowed, setRecordingAllowed] = useState<boolean | null>(true); // null=未评→仍允许提示录音
  const [recState, setRecState] = useState<"idle" | "armed">("idle");
  const [cueLevel, setCueLevel] = useState<0 | 1 | 2 | 3>(0);      // 已发给老人端的线索等级(只升不降)
  const [savingTurn, setSavingTurn] = useState(false);
  // 当前环节工作态
  const [work, setWork] = useState<WorkState>(emptyWork());
  const [pendingAudio, setPendingAudio] = useState<Record<string, { rawAudioId: string; duration: number }>>({});

  // 拉取计划 + 版本三方断言
  useEffect(() => {
    resetSession();
    api.sessionPlan(session.session_id, session.week_no, session.event_line)
      .then(async (p) => {
        const bank = await api.itemBank();
        // bundle 可能还没到,尽力断言 plan==后端;bundle 到位后再断言一次
        if (p.item_bank_version_id !== bank.version_id) {
          throw new Error(`题库版本不一致:计划=${p.item_bank_version_id} 后端=${bank.version_id}`);
        }
        setPlan(p);
        postSession({ sessionId: session.session_id, weekNo: session.week_no, eventLine: session.event_line, mode: "task", itemBankVersionId: p.item_bank_version_id });
      })
      .catch((e) => setErr(String(e)));
  }, [session, postSession, resetSession]);

  useEffect(() => {
    if (plan && bundle) {
      try { assertVersionsMatch(bundle.item_bank_version_id, plan.item_bank_version_id, plan.item_bank_version_id); }
      catch (e) { setErr(String(e)); }
    }
  }, [plan, bundle]);

  // 受试者是否允许录音——不允许则禁用"示意录音"(护栏:no-recording 受试者不得被 arm)
  useEffect(() => {
    api.getPatient(session.patient_id).then((pt) => setRecordingAllowed(pt.recording_allowed ?? null)).catch(() => {});
  }, [session.patient_id]);

  const item: PlanItem | undefined = plan?.items[itemIdx];
  const planTurn: PlanTurn | undefined = item?.turns[turnIdx];
  const turnK = item && planTurn ? `${item.item_id}#${planTurn.turn_seq}` : "";

  // 光标变化 → 重置工作态(从 journal 恢复布尔/已锁)+ 广播老人端
  useEffect(() => {
    if (!item || !planTurn) return;
    const jt = journal.turns[turnK];
    setWork({
      turnId: jt?.turnId ?? null, asrText: jt?.asrText ?? "", confirmed: jt?.confirmedText ?? "", ai: null,
      locked: jt?.locked ?? false, savedAsr: jt?.asrSaved ?? false, savedConfirm: jt?.confirmed ?? false,
    });
    setRecState("idle");
    setCueLevel(0);
    setCursor(itemIdx, turnIdx);
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel: 0, recording: "idle" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemIdx, turnIdx, plan]);

  // 老人端录完 → 建 turn 的音频入参在此暂存(按 turnKey)
  useAudioSaved(useCallback((m) => {
    setPendingAudio((prev) => ({ ...prev, [m.turnKey]: { rawAudioId: m.rawAudioId, duration: m.durationSeconds } }));
    // 记入 journal.audios,收尾屏音频闸门才能列出并驱动导出/校验/信度/删除。
    upsertAudio(m.rawAudioId, { turnKey: m.turnKey, containsDirectIdentifier: false, isReliabilitySample: false, lastStatus: "recorded" });
    setRecState("idle");
    toast(`老人端录音已保存(${m.durationSeconds.toFixed(1)}s)`, "info");
  }, [toast, upsertAudio]));

  const advance = () => {
    if (!plan || !item) return;
    if (turnIdx + 1 < item.turns.length) setTurnIdx(turnIdx + 1);
    else if (itemIdx + 1 < plan.items.length) { setItemIdx(itemIdx + 1); setTurnIdx(0); }
    else toast("已到最后一个环节,可前往场次收尾", "ok");
  };

  // 示意老人端录音(VOX):arm→老人端自动开始录音;stop→老人端停止并保存后回传 audioSaved。
  // 携带当前 cueLevel,录音启停不清老人端已显示的线索。
  const armRecording = () => {
    if (!planTurn) return;
    setRecState("armed");
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "armed" });
  };
  const stopRecording = () => {
    if (!planTurn) return;
    setRecState("idle");
    postCursor({ screen: "record", itemIdx, turnIdx, responseRole: planTurn.response_role, cueLevel, recording: "stopped" });
  };

  // 发分级线索给老人端(0→3 只升不降;线索文本永远取自版本锁定题库,操作端不产话术)。
  const sendCue = (level: 1 | 2 | 3) => {
    if (!planTurn || level <= cueLevel) return;
    setCueLevel(level);
    postCursor({ screen: "present", itemIdx, turnIdx, responseRole: planTurn.response_role,
                 cueLevel: level, recording: recState === "armed" ? "armed" : "idle" });
  };

  async function ensureItemEvent(): Promise<number | null> {
    if (!item) return null;
    const existing = journal.itemEvents[item.item_id];
    if (existing) return existing.itemEventId;
    const ie = await api.createItem(session.session_id, { item_id: item.item_id, task_type: item.task_type, image_id: item.image_id });
    upsertItem(item.item_id, { itemEventId: ie.id, taskType: item.task_type, imageId: item.image_id ?? null });
    return ie.id;
  }

  async function tryLocalAsr() {
    const au = pendingAudio[turnK];
    if (!au) return;
    try {
      const r = await api.asrTranscribe(au.rawAudioId);
      if (r.degraded || r.asr_text == null) {
        toast(`本地 ASR 引擎未接(${r.engine_version}),请人工转写`, "info");
        return;
      }
      // 引擎产文只预填,仍走人工确认→冻结的正常链路
      setWork((w) => (w.savedAsr ? w : { ...w, asrText: r.asr_text ?? w.asrText }));
      toast(`ASR 预填完成(${r.engine_version},置信度 ${r.asr_confidence ?? "—"})`, "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  async function saveTranscription() {
    if (!item || !planTurn) return;
    if (savingTurn) return;                                          // 在途锁:防双击重复建 item/turn
    if (work.savedAsr && work.turnId) { toast("该环节转写已冻结,不可重写", "warn"); return; }
    setSavingTurn(true);
    try {
      const ieid = await ensureItemEvent();
      if (ieid == null) return;
      const au = pendingAudio[turnK];
      const te = await api.createTurn(ieid, {
        turn_seq: planTurn.turn_seq, response_role: planTurn.response_role,
        asr_text: work.asrText, raw_audio_id: au?.rawAudioId ?? null, duration_seconds: au?.duration ?? null,
      });
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: te.id, responseRole: planTurn.response_role, asrSaved: true, asrText: work.asrText });
      setWork((w) => ({ ...w, turnId: te.id, savedAsr: true }));
      toast("转写已保存并冻结", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
    finally { setSavingTurn(false); }
  }

  async function saveConfirm() {
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return; }
    if (!work.confirmed.trim()) { toast("确认文本为空,未提交(避免把已有确认覆盖成空串)", "warn"); return; }
    try {
      await api.confirmTurn(work.turnId, work.confirmed);
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: work.turnId, responseRole: planTurn.response_role, confirmed: true, confirmedText: work.confirmed });
      setWork((w) => ({ ...w, savedConfirm: true }));
      toast("确认文本已保存(未覆盖原文)", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  async function runAiJudge() {
    if (work.turnId == null) { toast("请先保存转写", "warn"); return; }
    try {
      const te = await api.aiJudgeTurn(work.turnId);
      setWork((w) => ({ ...w, ai: { answerType: te.ai_answer_type ?? null, score: te.ai_score ?? null, needsReview: !!te.ai_needs_review } }));
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  async function doLock(elementValue: number, promptLevel?: number) {
    if (!item || !planTurn || work.turnId == null) { toast("请先保存转写", "warn"); return; }
    const payload = { reviewer_id: reviewerId, element_value: elementValue, prompt_level: promptLevel ?? null };
    try {
      assertPortraitFree(payload as unknown as Record<string, unknown>); // ★锁分载荷画像守卫(前端第4层)
      await api.lockTurn(work.turnId, payload);
      upsertTurn(item.item_id, planTurn.turn_seq, { turnId: work.turnId, responseRole: planTurn.response_role, locked: true, elementValue, promptLevel });
      setWork((w) => ({ ...w, locked: true }));
      toast("已锁定评分", "ok");
    } catch (e) { toast(e instanceof ApiError ? e.detail : String(e), "danger"); }
  }

  if (err) return <FailClosed msg={err} />;
  if (!plan) return <p>加载会话计划…</p>;
  if (plan.items.length === 0) return <p>本场次无评分题(第 1 周应走关系建立控制台)。</p>;

  return (
    <div className="row" style={{ alignItems: "flex-start", gap: "var(--sp-5)" }}>
      <ItemRail plan={plan} itemIdx={itemIdx} lockedCount={countLocked(journal, plan)}
        onPick={(i) => { setItemIdx(i); setTurnIdx(0); }} journalTurns={journal.turns} />

      <div className="col grow" style={{ maxWidth: 720 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2>训练判分 · {item?.item_id}</h2>
          <div className="row">
            <label className="row" style={{ gap: 6 }}>评分人
              <TextInput value={reviewerId} onChange={(e) => setReviewerId(e.target.value)} style={{ width: 90 }} />
            </label>
            <label className="row" style={{ gap: 6 }}>
              <input type="checkbox" checked={aiEnabled} onChange={(e) => setAiEnabled(e.target.checked)} /> 动态 AI 初评
            </label>
            <Button variant="ghost" onClick={onWrapup}>场次收尾 →</Button>
          </div>
        </div>

        {item && planTurn && (
          <div className="card col">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div><StatusPill tone="primary">{item.task_type}</StatusPill> 环节 {planTurn.turn_seq}/{item.turns.length} · <strong>{planTurn.response_role}</strong></div>
              {work.locked && <StatusPill tone="ok">已锁定</StatusPill>}
            </div>

            <ItemReference item={item} bundle={bundle} />

            {/* 分级线索(0→3 只升不降;内容取版本锁定题库,推老人端显示) */}
            {!work.locked && (
              <CueButtons bundle={bundle} itemId={item.item_id} taskType={item.task_type}
                role={planTurn.response_role} cueLevel={cueLevel} onSend={sendCue} />
            )}

            {/* 录音示意(老人端 VOX) */}
            <div className="row wrap" style={{ alignItems: "center" }}>
              {recordingAllowed === false ? (
                <StatusPill tone="danger">该受试者不允许录音 · 由研究者现场听记</StatusPill>
              ) : recState === "armed" ? (
                <Button variant="danger" onClick={stopRecording}>■ 停止录音</Button>
              ) : (
                <Button onClick={armRecording} disabled={work.locked}>● 示意老人录音</Button>
              )}
              {pendingAudio[turnK] && (
                <>
                  <StatusPill tone="ok">已收到录音 {pendingAudio[turnK].duration.toFixed(1)}s</StatusPill>
                  <audio controls preload="none" src={api.audioBlobUrl(pendingAudio[turnK].rawAudioId)} style={{ height: 36 }} />
                  {!work.savedAsr && <Button onClick={tryLocalAsr}>本地 ASR 转写</Button>}
                </>
              )}
            </div>

            {/* 阶段A 转写 */}
            <Field label="① ASR 转写(asr_text,保存即冻结,不覆盖)">
              <TextInput value={work.asrText} disabled={work.savedAsr}
                onChange={(e) => setWork((w) => ({ ...w, asrText: e.target.value }))}
                placeholder={pendingAudio[turnK] ? "老人端已录音,可回放后键入识别文本" : "键入听到/识别的回答"} />
            </Field>
            {!work.savedAsr && <Button onClick={saveTranscription} disabled={savingTurn}>{savingTurn ? "保存中…" : "保存转写"}</Button>}

            {/* 阶段B 确认 */}
            {work.savedAsr && (
              <>
                <div className="row">
                  <div className="grow"><div className="muted">asr_text(原文只读)</div><div className="card mono" style={{ background: "var(--c-diff)" }}>{work.asrText || "（空）"}</div></div>
                </div>
                <Field label="② 确认文本 confirmed(可改写,不动原文)">
                  <TextInput value={work.confirmed} disabled={work.locked}
                    onChange={(e) => setWork((w) => ({ ...w, confirmed: e.target.value }))} placeholder="人工校正后的规范文本" />
                </Field>
                {!work.locked && <Button onClick={saveConfirm}>保存确认</Button>}
              </>
            )}

            {/* 阶段C AI 初评 */}
            {work.savedAsr && aiEnabled && (
              <div className="card" style={{ background: "var(--c-bg)" }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>③ AI 初评(仅辅助,永不锁分)</strong>
                  <Button onClick={runAiJudge} disabled={work.locked}>运行 AI 初评</Button>
                </div>
                {work.ai && (
                  <div className="row wrap">
                    <StatusPill tone="muted">{work.ai.answerType ?? "纯人工(无确定式口径)"}</StatusPill>
                    <span>初评分:{work.ai.score ?? "—"}</span>
                    {work.ai.needsReview && <StatusPill tone="warn">需人工复核</StatusPill>}
                  </div>
                )}
              </div>
            )}
            {work.savedAsr && !aiEnabled && <p className="muted">③ 动态 AI 初评已关闭 → 纯人工从零打分。</p>}

            {/* 阶段D 人工锁分 */}
            {work.savedAsr && !work.locked && (
              <LockControl key={turnK} taskType={item.task_type} role={planTurn.response_role}
                suggestedPromptLevel={cueLevel} onLock={doLock} />
            )}
            {work.locked && <div className="row"><StatusPill tone="ok">✓ 本环节锁定分已写入</StatusPill><Button variant="primary" onClick={advance}>下一环节 →</Button></div>}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------- 工作态 ----------------
interface WorkState {
  turnId: number | null; asrText: string; confirmed: string;
  ai: { answerType: string | null; score: number | null; needsReview: boolean } | null;
  locked: boolean; savedAsr: boolean; savedConfirm: boolean;
}
const emptyWork = (): WorkState => ({ turnId: null, asrText: "", confirmed: "", ai: null, locked: false, savedAsr: false, savedConfirm: false });

function countLocked(journal: ReturnType<typeof useSessionJournal>["journal"], plan: SessionPlan): number {
  return plan.items.filter((it) => it.turns.every((t) => journal.turns[`${it.item_id}#${t.turn_seq}`]?.locked)).length;
}

// ---------------- 左：题目游标列 ----------------
function ItemRail({ plan, itemIdx, lockedCount, onPick, journalTurns }: {
  plan: SessionPlan; itemIdx: number; lockedCount: number;
  onPick: (i: number) => void; journalTurns: Record<string, { locked: boolean }>;
}) {
  return (
    <div className="col" style={{ width: 220, flexShrink: 0 }}>
      <div className="muted">进度 {lockedCount}/{plan.items.length} 题全锁</div>
      <div className="col" style={{ gap: 4, maxHeight: "70vh", overflowY: "auto" }}>
        {plan.items.map((it, i) => {
          const total = it.turns.length;
          const locked = it.turns.filter((t) => journalTurns[`${it.item_id}#${t.turn_seq}`]?.locked).length;
          const allLocked = locked === total;
          return (
            <button key={it.item_id} onClick={() => onPick(i)}
              style={{ textAlign: "left", padding: "6px 10px", borderRadius: 8,
                border: i === itemIdx ? "2px solid var(--c-primary)" : "1px solid var(--c-line)",
                background: i === itemIdx ? "var(--c-warn-bg)" : "var(--c-surface)" }}>
              <span style={{ marginRight: 6 }}>{allLocked ? "📌" : "📍"}</span>
              {it.item_id} {total > 1 && <span className="muted">({locked}/{total})</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ---------------- 题目参考(判分依据,零画像) ----------------
function ItemReference({ item, bundle }: { item: PlanItem; bundle: ReturnType<typeof useItemBankBundle>["bundle"] }) {
  if (!bundle) return null;
  if (item.task_type === "单要素") {
    const s = findSingle(bundle, item.item_id);
    if (!s) return null;
    return <div className="muted" style={{ fontSize: "0.9em" }}>目标词:<strong>{s.target_word}</strong> · 可接受:{s.acceptable_expressions.join("、") || "—"}</div>;
  }
  if (item.task_type === "双要素") {
    const d = findDouble(bundle, item.item_id);
    if (!d) return null;
    return (
      <div className="muted" style={{ fontSize: "0.9em" }}>
        左:<strong>{d.left_word}</strong> · 右:<strong>{d.right_word}</strong> · 关系线索:{d.relation_cue}
      </div>
    );
  }
  return null;
}

// ---------------- 分级线索控件(推老人端;文本只来自题库,缺文本禁发) ----------------
function CueButtons({ bundle, itemId, taskType, role, cueLevel, onSend }: {
  bundle: ReturnType<typeof useItemBankBundle>["bundle"];
  itemId: string; taskType: string; role: string;
  cueLevel: 0 | 1 | 2 | 3; onSend: (level: 1 | 2 | 3) => void;
}) {
  // 单要素命名:三级;双要素 作用/关系:一级(题库仅一条角色线索);其余无预置线索。
  const levels: { level: 1 | 2 | 3; label: string }[] =
    taskType === "单要素"
      ? [{ level: 1, label: "发线索1" }, { level: 2, label: "发线索2" }, { level: 3, label: "告知答案" }]
      : taskType === "双要素" && (role.includes("作用") || role === "关系识别")
        ? [{ level: 1, label: "发线索" }]
        : [];
  if (levels.length === 0) return null;
  const current = lookupCue(bundle, itemId, taskType, role, cueLevel);
  return (
    <div className="card col" style={{ background: "var(--c-bg)", gap: "var(--sp-2)" }}>
      <div className="row wrap">
        <strong>线索(当前 {cueLevel} 级)</strong>
        {levels.map(({ level, label }) => {
          const text = lookupCue(bundle, itemId, taskType, role, level);
          return (
            <Button key={level} disabled={level <= cueLevel || text == null}
              title={text == null ? "题库缺此级线索文本(待内容组补)" : undefined}
              onClick={() => onSend(level)}>
              {label}{text == null ? "(缺文本)" : ""}
            </Button>
          );
        })}
      </div>
      {current && <p className="muted" style={{ fontSize: "0.9em" }}>老人端正显示:{current}</p>}
    </div>
  );
}

// ---------------- 按角色切换的锁分控件 ----------------
function LockControl({ taskType, role, suggestedPromptLevel = 0, onLock }: {
  taskType: string; role: string; suggestedPromptLevel?: number;
  onLock: (v: number, promptLevel?: number) => void;
}) {
  const [promptLevel, setPromptLevel] = useState<string | null>(String(suggestedPromptLevel));
  const [confirmVal, setConfirmVal] = useState<{ v: number; label: string } | null>(null);

  // 线索升级发生在锁分之后填的场景:跟随建议(研究者仍可改)。
  useEffect(() => { setPromptLevel(String(suggestedPromptLevel)); }, [suggestedPromptLevel]);

  const relation = isRelationRole(role);
  const single = taskType === "单要素";

  const choices: { v: number; label: string; tone: "ok" | "warn" | "danger" }[] = relation
    ? [{ v: 1, label: "识别(1)", tone: "ok" }, { v: 0.5, label: "部分(0.5)", tone: "warn" }, { v: 0, label: "未识别(0)", tone: "danger" }]
    : [{ v: 1, label: single ? "命名正确(1)" : "正确(1)", tone: "ok" }, { v: 0, label: single ? "未正确(0)" : "错误(0)", tone: "danger" }];

  return (
    <div className="card col" style={{ background: "var(--c-bg)" }}>
      <strong>④ 人工锁分(研究数据真值 · 一旦锁定不可改)</strong>
      {single && (
        <Field label="提示等级 prompt_level(0 自发 / 1 轻提示 / 2 明确或语音 / 3 告知答案)">
          <EnumSelect options={["0", "1", "2", "3"]} value={promptLevel} onChange={setPromptLevel} allowEmpty={false} />
        </Field>
      )}
      <div className="row wrap">
        {choices.map((c) => (
          <Button key={c.v} variant={c.tone === "ok" ? "primary" : "ghost"} onClick={() => setConfirmVal({ v: c.v, label: c.label })}>
            {c.label}
          </Button>
        ))}
      </div>

      {confirmVal && (
        <LockConfirm label={confirmVal.label} role={role} promptLevel={single ? Number(promptLevel ?? 0) : undefined}
          onCancel={() => setConfirmVal(null)}
          onConfirm={() => { onLock(confirmVal.v, single ? Number(promptLevel ?? 0) : undefined); setConfirmVal(null); }} />
      )}
    </div>
  );
}

function LockConfirm({ label, role, promptLevel, onConfirm, onCancel }: {
  label: string; role: string; promptLevel?: number; onConfirm: () => void; onCancel: () => void;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { const t = setTimeout(() => setArmed(true), 400); return () => clearTimeout(t); }, []);
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.4)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 950 }}>
      <div className="card col" style={{ maxWidth: 460 }}>
        <h3>确认锁定</h3>
        <p>环节「{role}」→ 锁定为 <strong>{label}</strong>{promptLevel != null ? ` · 提示等级 ${promptLevel}` : ""}。</p>
        <p className="muted">锁定后本环节不可再改 confirmed / 不可重复锁。</p>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <Button onClick={onCancel}>取消</Button>
          <Button variant="primary" disabled={!armed} onClick={onConfirm}>{armed ? "确认锁定" : "请稍候…"}</Button>
        </div>
      </div>
    </div>
  );
}

function FailClosed({ msg }: { msg: string }) {
  return (
    <div className="card" style={{ background: "var(--c-danger-bg)", border: "2px solid var(--c-danger)", color: "var(--c-danger)", maxWidth: 720 }}>
      <h3>训练屏 fail-closed</h3>
      <p>{msg}</p>
      <p className="muted">题库版本不一致或计划加载失败时,拒开训练以保护数据完整性。</p>
    </div>
  );
}
