/**
 * 设备基础检查页(/device-check)。它只记录基础技术状况，
 * 不能判定设备已可用于训练或养老院。
 * 判定全在 deviceCheck.ts;这里只是状态机:自动检查 → 安静采样 →
 * 请说话 → 提示音人工确认 → 汇总 + 可复制的记录文本。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../components/Button";
import {
  classifyNoiseFloor,
  classifySpeakerConfirmation,
  classifySpeechLevel,
  describeSampleFailure,
  formatReport,
  runAutoChecks,
  summarize,
  type CheckResult,
  type Verdict,
} from "./deviceCheck.ts";
import { createBrowserDeviceCheckDependencies } from "./browserDeviceCheckDependencies.ts";

type Stage = "idle" | "auto" | "quiet" | "speech" | "beep" | "done";

const STATUS_MARK = { pass: "✓", warn: "△", fail: "✗" } as const;
const STATUS_COLOR = { pass: "var(--c-ok)", warn: "var(--c-warn)", fail: "var(--c-danger)" } as const;
const VERDICT_LABEL: Record<Verdict, string> = {
  "basic-pass": "✓ 基础检查通过，下一步做真机验收",
  "basic-pass-with-warnings": "△ 基础检查有警告，不得直接投入使用",
  "basic-fail": "✗ 基础检查不通过，不得使用这台设备",
  incomplete: "✗ 检查未完成，不得使用这台设备",
};

export function DeviceCheckScreen() {
  const deps = useMemo(createBrowserDeviceCheckDependencies, []);
  const [stage, setStage] = useState<Stage>("idle");
  const [results, setResults] = useState<CheckResult[]>([]);
  const [online, setOnline] = useState(navigator.onLine);
  const [copied, setCopied] = useState(false);
  const [beepPlayed, setBeepPlayed] = useState(false);
  const [finishedAt, setFinishedAt] = useState<string | null>(null);
  const running = useRef(false);

  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
      deps.dispose();
    };
  }, [deps]);

  const push = (result: CheckResult) =>
    setResults((prev) => [...prev.filter((r) => r.id !== result.id), result]);

  const start = async () => {
    if (running.current) return;
    running.current = true;
    deps.armAudioSync();                       // 必须同步在手势栈内,见依赖实现的注释
    setResults([]);
    setCopied(false);
    setBeepPlayed(false);
    setFinishedAt(null);
    setStage("auto");
    try {
      const auto = await runAutoChecks(deps, push);
      const micOk = auto.results.find((r) => r.id === "microphone")?.status !== "fail";
      if (micOk) {
        setStage("quiet");
        try {
          push(classifyNoiseFloor(await deps.sampleNoise(3)));
        } catch (error) {
          push({ id: "noise-floor", title: "环境噪声", status: "fail", detail: describeSampleFailure(error) });
        }
        setStage("speech");
        try {
          push(classifySpeechLevel(await deps.sampleNoise(4)));
        } catch (error) {
          push({ id: "speech-level", title: "人声拾取", status: "fail", detail: describeSampleFailure(error) });
        }
      } else {
        push({ id: "noise-floor", title: "环境噪声", status: "fail", detail: "麦克风不可用，未采样。" });
        push({ id: "speech-level", title: "人声拾取", status: "fail", detail: "麦克风不可用，未采样。" });
      }
      setStage("beep");
      setBeepPlayed(deps.playBeep());
    } finally {
      try { deps.releaseMicrophone(); }
      finally { running.current = false; }
    }
  };

  const confirmBeep = (heard: boolean) => {
    push(classifySpeakerConfirmation(beepPlayed, heard));
    setFinishedAt(new Date().toISOString());
    setStage("done");
  };

  const verdict = summarize(results);
  const report = formatReport(navigator.userAgent, results, verdict, finishedAt ?? "未完成");

  const copy = () => {
    navigator.clipboard?.writeText(report).then(() => setCopied(true)).catch(() => {
      // 剪贴板不可用(旧 WebView):文本就在下面,人工长按复制。
    });
  };

  return (
    <main className="page-shell narrow">
      <div className="page-header-block">
        <div className="page-kicker">现场准备 · 由工作人员测试</div>
        <h1 className="page-title">设备基础检查</h1>
        <p className="page-description">
          在训练用的设备和房间里做，约 20 秒。只测音量，不保存、不上传录音。
          请由工作人员操作。
        </p>
      </div>

      <p style={{ margin: "0 0 var(--sp-3)" }}>
        <span style={{ fontWeight: 600 }}>联网状态：</span>{" "}
        <span style={{ color: online ? STATUS_COLOR.pass : STATUS_COLOR.fail, fontWeight: 600 }}>
          {online ? "在线" : "离线"}
        </span>
      </p>

      {stage === "idle" && (
        <Button variant="primary" size="lg" onClick={() => { void start(); }}>
          开始检查
        </Button>
      )}
      {stage === "auto" && <p className="muted">正在检查运行环境、麦克风、网络…</p>}
      {stage === "quiet" && (
        <p style={stagePrompt}>请保持安静 3 秒，正在测环境噪声…</p>
      )}
      {stage === "speech" && (
        <p style={stagePrompt}>请对着设备正常说一句话（比如「今天天气很好」）…</p>
      )}
      {stage === "beep" && (
        <div>
          <p style={stagePrompt}>
            {beepPlayed ? "刚才有一声提示音，你听到了吗？" : "提示音播放失败，视为没听到。"}
          </p>
          <div className="form-actions">
            <Button variant="primary" size="lg" disabled={!beepPlayed}
              onClick={() => confirmBeep(true)}>听到了</Button>
            <Button variant="secondary" size="lg" onClick={() => confirmBeep(false)}>没听到</Button>
          </div>
        </div>
      )}

      {results.length > 0 && (
        <ul style={{ listStyle: "none", padding: 0, margin: "16px 0" }}>
          {results.map((r) => (
            <li key={r.id} style={{ padding: "6px 0", borderBottom: "1px solid var(--c-line)" }}>
              <span style={{ color: STATUS_COLOR[r.status], fontWeight: 700, marginRight: 8 }}>
                {STATUS_MARK[r.status]}
              </span>
              <span style={{ fontWeight: 600, marginRight: 8 }}>{r.title}</span>
              <span className="muted">{r.detail}</span>
            </li>
          ))}
        </ul>
      )}

      {stage === "done" && (
        <div>
          <p style={{ fontWeight: 700, fontSize: 18, color: STATUS_COLOR[
            verdict === "basic-pass" ? "pass" : verdict === "basic-pass-with-warnings" ? "warn" : "fail"] }}>
            {VERDICT_LABEL[verdict]}
          </p>
          <p className="muted">
            这只是基础检查，正式启用前还要做真机验收。设备或系统更新后请重测。
          </p>
          <p className="muted">
            断网测试：关掉 Wi-Fi，顶部指示应变红；重新联网应变绿。
          </p>
          {(verdict === "basic-pass" || verdict === "basic-pass-with-warnings") && (
            <p>
              <Link to="/acceptance">去做真机验收 →</Link>
            </p>
          )}
          <div className="form-actions">
            <Button variant="primary" size="lg" onClick={() => { void start(); }}>重新检查</Button>
            <Button variant="secondary" size="lg" onClick={copy}>
              {copied ? "已复制" : "复制检查记录"}
            </Button>
          </div>
          <pre style={{ whiteSpace: "pre-wrap", background: "var(--c-surface)", padding: 12,
                        borderRadius: 10, border: "1px solid var(--c-line)", fontSize: 12 }}>
            {report}
          </pre>
        </div>
      )}

      <p style={{ marginTop: "var(--sp-5)" }}>
        <Link to="/" className="muted">← 返回首页</Link>
      </p>
    </main>
  );
}

const stagePrompt: React.CSSProperties = { fontSize: 18, fontWeight: 600 };
