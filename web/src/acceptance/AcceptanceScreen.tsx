/**
 * 真机验收向导页(/acceptance)。它是记录仪不是判定器——判定全在
 * acceptanceProtocol.ts，这里只负责让人在现场一项一项地勾，并把机器说得出的
 * 事实（UA、构建编号、屏幕、时间）自动记进回执。
 *
 * 刻意不往服务器写：验收当天可能正好在验"断网会怎样"，一个需要联网才能保存的
 * 记录页在那一刻恰好用不了。所以只下载 JSON + 复制纯文本。
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ACCEPTANCE_HEADER_FIELDS,
  ACCEPTANCE_ITEMS,
  VERDICT_LABEL,
  buildAcceptanceReceipt,
  emptyDraft,
  failedSteps,
  formatAcceptanceText,
  missingHeaderFields,
  receiptFilename,
  summarizeAcceptance,
  unsetSteps,
  type AcceptanceDraft,
  type ItemOutcome,
} from "./acceptanceProtocol.ts";

const VERDICT_TONE = {
  "incomplete-header": "#b26a00",
  "incomplete-steps": "#b26a00",
  "recorded-with-failures": "#c62828",
  "recorded-all-pass": "#2e7d32",
} as const;

function machineFacts(): { userAgent: string; buildId: string; screen: string } {
  return {
    userAgent: navigator.userAgent,
    buildId: __BUILD_ID__,
    screen: `${window.screen.width}×${window.screen.height} @${window.devicePixelRatio}x`,
  };
}

export function AcceptanceScreen() {
  const [draft, setDraft] = useState<AcceptanceDraft>(emptyDraft);
  const [copied, setCopied] = useState(false);
  const facts = useMemo(machineFacts, []);

  const verdict = summarizeAcceptance(draft);
  const missing = missingHeaderFields(draft);
  const unset = unsetSteps(draft);
  const failed = failedSteps(draft);

  const setHeader = (key: string, value: string) => {
    setCopied(false);
    setDraft((prev) => ({ ...prev, header: { ...prev.header, [key]: value } }));
  };
  const setOutcome = (key: string, value: ItemOutcome) => {
    setCopied(false);
    setDraft((prev) => ({ ...prev, outcomes: { ...prev.outcomes, [key]: value } }));
  };
  const setNote = (key: string, value: string) => {
    setCopied(false);
    setDraft((prev) => ({ ...prev, notes: { ...prev.notes, [key]: value } }));
  };

  const receipt = () => buildAcceptanceReceipt(draft, {
    ...facts, finishedAtIso: new Date().toISOString(),
  });

  const download = () => {
    const value = receipt();
    const blob = new Blob([JSON.stringify(value, null, 2)],
                         { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = receiptFilename(value);
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(formatAcceptanceText(receipt()));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="page-shell page-shell--wide">
      <header className="page-header-block">
        <div>
          <p className="page-kicker">真机验收 · 八项</p>
          <h1 className="page-title">目标设备验收记录</h1>
          <p className="page-description">
            在<strong>目标设备、目标浏览器、真实房间、真实网络</strong>上填，
            全程只用虚构受试者和合成语音。这一页只记录，不判定；
            任何一项不清楚都按未通过处理。
          </p>
        </div>
      </header>

      <section className="form-section">
        <p className="muted">
          先跑一遍<Link to="/device-check">设备基础检查</Link>。
          它全绿只代表基础检查通过，<strong>不代表这台设备已可用于养老院</strong>。
        </p>
        <p className="muted">屏幕 {facts.screen}</p>
        <p className="muted">
          本页构建编号（回执里记的就是这一串，抄的时候整串抄）：
        </p>
        <p className="mono acceptance-build-id">{facts.buildId}</p>
      </section>

      <section className="form-section" aria-label="设备与场地">
        <div className="form-section-header">
          <div>
            <h2>先填这台设备的基本情况</h2>
            <p className="muted">
              这些机器猜不出来，少填一格整份记录就不能归档——设备矩阵靠的就是这几格。
            </p>
          </div>
        </div>
        {ACCEPTANCE_HEADER_FIELDS.map((field) => (
          <label key={field.key} className="field">
            <span>{field.label}</span>
            <input
              value={draft.header[field.key] ?? ""}
              placeholder={field.placeholder}
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
                        onChange={() => setOutcome(step.key, value)}
                      />
                      <span>{value === "pass" ? "通过" : "未通过"}</span>
                    </label>
                  ))}
                  {outcome === "unset"
                    ? <span className="muted">（还没勾）</span>
                    : (
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
                  <span>备注（看到什么就写什么，不确定也写）</span>
                  <input
                    value={draft.notes[step.key] ?? ""}
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
        <div className="form-actions">
          <button className="button button--primary button--md" onClick={download}>
            下载 JSON 回执
          </button>
          <button className="button button--ghost button--md" onClick={() => void copy()}>
            {copied ? "已复制纯文本" : "复制纯文本"}
          </button>
        </div>
        <p className="muted">
          未完成的记录也可以下载——半份真实记录比一份补齐的漂亮记录有用。
          回执里会写明它未完成。
        </p>
      </section>
    </div>
  );
}
