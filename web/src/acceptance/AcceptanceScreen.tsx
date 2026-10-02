/**
 * 真机验收向导页(/acceptance)。它是记录仪不是判定器——判定全在
 * acceptanceProtocol.ts，这里只负责让人在现场一项一项地勾，并把机器说得出的
 * 事实（UA、构建编号、屏幕、时间）自动记进回执。
 *
 * 刻意不往服务器写：验收当天可能正好在验"断网会怎样"，一个需要联网才能保存的
 * 记录页在那一刻恰好用不了。所以只下载 JSON + 复制纯文本。
 *
 * 「完成并冻结」只生成一次回执字节，之后下载、复制拿到的都是那一份；冻结后表单只读，
 * 要改就解冻换新编号。
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Alert } from "../components/Alert";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  ACCEPTANCE_HEADER_FIELDS,
  ACCEPTANCE_ITEMS,
  VERDICT_LABEL,
  emptyDraft,
  failedSteps,
  freezeAcceptanceReceipt,
  missingHeaderFields,
  readFrozenAcceptanceReceipt,
  reviseFrozenDraft,
  shortBuildId,
  shortReceiptId,
  summarizeAcceptance,
  unsetSteps,
  type AcceptanceDraft,
  type ItemOutcome,
} from "./acceptanceProtocol.ts";

const VERDICT_TONE = {
  "incomplete-header": "var(--c-warn)",
  "incomplete-steps": "var(--c-warn)",
  "recorded-with-failures": "var(--c-danger)",
  "recorded-all-pass": "var(--c-ok)",
} as const;

type CopyKind = "text" | "json";

interface AcceptanceWork {
  draft: AcceptanceDraft;
  /** 已冻结回执的原始字节；没冻结为 null。 */
  frozenJson: string | null;
}

function machineFacts(): { userAgent: string; buildId: string; screen: string } {
  return {
    userAgent: navigator.userAgent,
    buildId: __BUILD_ID__,
    screen: `${window.screen.width}×${window.screen.height} @${window.devicePixelRatio}x`,
  };
}

export function AcceptanceScreen() {
  const [work, setWork] = useState<AcceptanceWork>(() => ({ draft: emptyDraft(), frozenJson: null }));
  // 旧 WebView 上剪贴板会静默失败；失败时把要复制的内容直接摆出来让人长按复制。
  const [copyResult, setCopyResult] = useState<{ kind: CopyKind; ok: boolean } | null>(null);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const facts = useMemo(machineFacts, []);

  const { draft } = work;
  const frozen = useMemo(
    () => (work.frozenJson === null ? null : readFrozenAcceptanceReceipt(work.frozenJson)),
    [work.frozenJson],
  );
  const locked = work.frozenJson !== null;
  const verdict = summarizeAcceptance(draft);
  const missing = missingHeaderFields(draft);
  const unset = unsetSteps(draft);
  const failed = failedSteps(draft);
  const copyFallbackText = frozen && copyResult && !copyResult.ok
    ? (copyResult.kind === "text" ? frozen.text : frozen.json)
    : null;

  const updateDraft = (change: (prev: AcceptanceDraft) => AcceptanceDraft) => {
    setCopyResult(null);
    // 冻结后表单只读；这里再拦一道，晚到的输入事件也改不动已冻结那份的草稿。
    setWork((prev) => (prev.frozenJson !== null ? prev : { ...prev, draft: change(prev.draft) }));
  };
  const setHeader = (key: string, value: string) =>
    updateDraft((prev) => ({ ...prev, header: { ...prev.header, [key]: value } }));
  const setOutcome = (key: string, value: ItemOutcome) =>
    updateDraft((prev) => ({ ...prev, outcomes: { ...prev.outcomes, [key]: value } }));
  const setNote = (key: string, value: string) =>
    updateDraft((prev) => ({ ...prev, notes: { ...prev.notes, [key]: value } }));

  const freeze = () => {
    setCopyResult(null);
    const frozenNow = freezeAcceptanceReceipt(draft, { ...facts, finishedAtIso: new Date().toISOString() });
    setWork({ draft, frozenJson: frozenNow.json });
  };

  const revise = () => {
    setCopyResult(null);
    setWork({ draft: reviseFrozenDraft(draft), frozenJson: null });
  };

  const startNew = () => {
    setCopyResult(null);
    setConfirmingReset(false);
    setWork({ draft: emptyDraft(), frozenJson: null });
  };

  const download = () => {
    if (frozen === null) return;
    const blob = new Blob([frozen.json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = frozen.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Safari 在点击后立刻回收 blob 地址会让下载落空；留足时间再回收。
    window.setTimeout(() => URL.revokeObjectURL(url), 40_000);
  };

  const copy = async (kind: CopyKind) => {
    if (frozen === null) return;
    try {
      await navigator.clipboard.writeText(kind === "text" ? frozen.text : frozen.json);
      setCopyResult({ kind, ok: true });
    } catch {
      setCopyResult({ kind, ok: false });
    }
  };

  const copyLabel = (kind: CopyKind, idle: string) => {
    if (copyResult?.kind !== kind) return idle;
    return copyResult.ok ? "已复制" : "复制失败，长按下方文本";
  };

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">真机验收 · 十三项</p>
          <h1 className="page-title">真机验收记录</h1>
          <p className="page-description">
            在<strong>正式使用的设备、浏览器和房间</strong>里填写，
            只用虚构受试者。不确定的项一律勾「未通过」。
          </p>
        </div>
      </header>

      <section className="form-section">
        <p className="muted">
          请先完成<Link to="/device-check">设备基础检查</Link>。
        </p>
        <p className="muted">屏幕 {facts.screen}</p>
        <p className="muted">
          页面版本号（短号；完整编号已自动写进提交记录）：
        </p>
        <p className="mono acceptance-build-id">{shortBuildId(facts.buildId)}</p>
        <p className="muted">本份记录编号（短号；完整编号写在记录文件里）：</p>
        <p className="mono acceptance-build-id">{shortReceiptId(draft.receiptId)}</p>
        {draft.supersedesReceiptId !== null && (
          <p className="muted">
            这是一份改正记录，冻结后会写明它取代记录 {shortReceiptId(draft.supersedesReceiptId)}。
          </p>
        )}
      </section>

      <Alert tone="warn" role="note" title="不写老人姓名或编号">
        表头和每一项备注都不写老人的姓名、受试者编号或床号；验收只用虚构受试者。
        「验收人」一栏填工作人员真名。
      </Alert>

      <section className="form-section" aria-label="设备与场地">
        <div className="form-section-header">
          <div>
            <h2>先填这台设备的基本情况</h2>
            <p className="muted">
              每一格都必须填，缺一格记录不能归档。
            </p>
          </div>
        </div>
        {ACCEPTANCE_HEADER_FIELDS.map((field) => (
          <label key={field.key} className="field">
            <span>{field.label}</span>
            <input
              value={draft.header[field.key] ?? ""}
              placeholder={field.placeholder}
              disabled={locked}
              onChange={(event) => setHeader(field.key, event.target.value)}
            />
          </label>
        ))}
      </section>

      {ACCEPTANCE_ITEMS.map((item) => (
        <section className="form-section" key={item.no}
                 aria-label={`第 ${item.no} 项 ${item.title}`}>
          <div className="form-section-header">
            <div>
              <h2>{item.no}. {item.title}</h2>
            </div>
          </div>
          {item.steps.map((step) => {
            const outcome = draft.outcomes[step.key] ?? "unset";
            return (
              <div className="acceptance-step" key={step.key}>
                <h3>[{step.key}] {step.title}</h3>
                <p className="muted">怎么做：{step.how}</p>
                <p className="muted">通过判据：{step.criterion}</p>
                <div className="form-actions" role="group"
                     aria-label={`第 ${step.key} 项结果`}>
                  {/* 没做过的项两个都不选中——像纸表上那一格本来就是空的。
                      给"未做"配一个默认选中的单选框，等于替验收人主动勾了它。 */}
                  {(["pass", "fail"] as const).map((value) => (
                    <label key={value} className="check-row">
                      <input
                        type="radio"
                        name={`outcome-${step.key}`}
                        checked={outcome === value}
                        disabled={locked}
                        onChange={() => setOutcome(step.key, value)}
                      />
                      <span>{value === "pass" ? "通过" : "未通过"}</span>
                    </label>
                  ))}
                  {outcome === "unset"
                    ? <span className="muted">（还没勾）</span>
                    : !locked && (
                      <button
                        type="button"
                        className="button button--quiet button--sm"
                        onClick={() => setOutcome(step.key, "unset")}
                      >
                        点错了，清除这一项
                      </button>
                    )}
                </div>
                <label className="field">
                  <span>备注（看到什么就写什么，不确定也写；不写老人姓名或编号）</span>
                  <input
                    value={draft.notes[step.key] ?? ""}
                    disabled={locked}
                    onChange={(event) => setNote(step.key, event.target.value)}
                  />
                </label>
              </div>
            );
          })}
        </section>
      ))}

      <section className="form-section" aria-label="本次结论">
        <h2 style={{ color: VERDICT_TONE[verdict] }}>{VERDICT_LABEL[verdict]}</h2>
        {missing.length > 0 && (
          <p className="muted">表头还差：{missing.join("、")}</p>
        )}
        {unset.length > 0 && (
          <p className="muted">还没勾的项：{unset.join("、")}</p>
        )}
        {failed.length > 0 && (
          <p className="muted">
            未通过的项：{failed.join("、")}。
            修复后<strong>必须重新验收这些项</strong>，不得沿用本次结果。
          </p>
        )}
        {frozen === null ? (
          <>
            <div className="form-actions">
              <button type="button" className="button button--primary button--md" onClick={freeze}>
                完成并冻结
              </button>
              <button type="button" className="button button--quiet button--md"
                      onClick={() => setConfirmingReset(true)}>
                开始新记录
              </button>
            </div>
            <p className="muted">
              冻结后这份记录就定稿了，下载和复制拿到的都是同一份内容。
              没填完也可以冻结，记录里会注明未完成。
            </p>
          </>
        ) : (
          <>
            <p>
              已冻结：记录编号 <span className="mono">{shortReceiptId(frozen.receipt.receiptId)}</span>，
              冻结于 {frozen.receipt.machine.finishedAtIso}。
            </p>
            <div className="form-actions">
              <button type="button" className="button button--primary button--md" onClick={download}>
                保存记录文件
              </button>
              <button type="button" className="button button--ghost button--md"
                      onClick={() => void copy("text")}>
                {copyLabel("text", "复制纯文本")}
              </button>
              <button type="button" className="button button--ghost button--md"
                      onClick={() => void copy("json")}>
                {copyLabel("json", "复制 JSON（下载不了时用）")}
              </button>
            </div>
            <p className="muted">
              文件名 {frozen.filename}。原样发给负责人，不要改名、不要打开改内容。
            </p>
            <div className="form-actions">
              <button type="button" className="button button--quiet button--md" onClick={revise}>
                发现填错：解冻修改（会换新的记录编号）
              </button>
              <button type="button" className="button button--quiet button--md"
                      onClick={() => setConfirmingReset(true)}>
                开始新记录
              </button>
            </div>
          </>
        )}
        {copyFallbackText !== null && (
          <pre style={{ whiteSpace: "pre-wrap", background: "var(--c-surface)",
                        padding: "var(--sp-3)", borderRadius: "var(--radius)",
                        border: "1px solid var(--c-line)", fontSize: 12 }}>
            {copyFallbackText}
          </pre>
        )}
      </section>

      <ConfirmDialog
        open={confirmingReset}
        title="清空这份记录，开始新的一份？"
        body={locked
          ? "已冻结的这份如果还没保存文件或复制出去，清空后在这台设备上就找不回来了。"
          : "现在填的内容会全部清空，这份记录还没冻结。"}
        confirmLabel="清空并开始新记录"
        onConfirm={startNew}
        onCancel={() => setConfirmingReset(false)}
      />

      <p style={{ marginTop: "var(--sp-5)" }}>
        <Link to="/" className="muted">← 返回首页</Link>
      </p>
    </div>
  );
}
