import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { clearConsoleWorkspaceState } from "./security/localSensitiveState";
import { CONSOLE_NOTE_EVENT, PATIENT_VIEW_EVENT, PATIENT_VIEW_EXIT_EVENT, PATIENT_VIEW_REC_EVENT } from "./sync/messages";

const ConsoleShell = lazy(async () => ({
  default: (await import("./console/ConsoleShell")).ConsoleShell,
}));
const PatientShell = lazy(async () => ({
  default: (await import("./patient/PatientShell")).PatientShell,
}));
const DeviceCheckScreen = lazy(async () => ({
  default: (await import("./device/DeviceCheckScreen")).DeviceCheckScreen,
}));

// 两路由:/console 操作端 · /patient 老人端。零外链(硬约束3)。SPA fallback 由 main.py 提供,clean URL 可刷新。
export default function App() {
  return (
    <BrowserRouter>
      <StaleBuildBanner />
      <Routes>
        <Route path="/" element={<Landing />} />
        {/* 操作端与叠层宿主各自独立 Boundary:操作端崩溃时叠层不塌——受试者继续看平和画面,
            绝不被突然切到红色报错屏;研究者经宿主的按住返回离开后才见错误页。 */}
        <Route path="/console" element={<>
          <Boundary variant="console">
            <Suspense fallback={<ConsoleLoading />}><ConsoleShell /></Suspense>
          </Boundary>
          <Boundary variant="patient"><PatientViewHost /></Boundary>
        </>} />
        <Route path="/patient" element={
          <Boundary variant="patient">
            <Suspense fallback={<PatientLoading />}><PatientShell /></Suspense>
          </Boundary>
        } />
        {/* 现场准备用的设备自检:不登录、不碰任何受试者数据,只测这台机器本身。 */}
        <Route path="/device-check" element={
          <Boundary variant="console">
            <Suspense fallback={<ConsoleLoading />}><DeviceCheckScreen /></Suspense>
          </Boundary>
        } />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

function ConsoleLoading() {
  return <main className="login-shell"><div className="login-card"><p className="muted">正在打开研究者工作台…</p></div></main>;
}

function PatientLoading() {
  return (
    <main className="patient-message-screen" aria-live="polite">
      <div className="target">请稍等一下</div>
      <p className="question muted">训练画面正在准备</p>
    </main>
  );
}

// 渲染崩溃兜底:没有它,任何一处 render 抛错 = React 卸载整棵树 = 整页按钮全部失灵且无解释。
// 操作端给出错信息+重载入口;老人端保持平和(永不报错、永不闪红),由研究者处理。
class Boundary extends Component<{ variant: "console" | "patient"; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) { return { error: String(e) }; }
  componentDidCatch() {
    // 老人端崩溃时其卸载清理会删掉适老字号尺度,平和屏会缩成 16px 小字——补回来。
    if (this.props.variant === "patient") document.documentElement.dataset.scale = "patient";
  }
  render() {
    if (this.state.error === null) return this.props.children;
    if (this.props.variant === "patient") {
      return (
        <main className="patient-message-screen" aria-live="polite">
          <div className="target">请稍等一下</div>
          <p className="question muted">请研究者过来看一眼</p>
          {/* 原地重试而非整页 reload:叠层场景下 reload 会把受试者载进研究者操作端。 */}
          <button className="patient-primary-action" type="button" onClick={() => this.setState({ error: null })}>
            重新载入
          </button>
        </main>
      );
    }
    return (
      <main className="page-shell narrow error-page">
        <div className="page-header-block">
          <div className="page-kicker">安全暂停</div>
          <h2 className="page-title">界面遇到问题，操作已停止</h2>
          <p className="page-description">已保存到服务器的研究数据不受影响。</p>
        </div>
        <p className="muted" style={{ wordBreak: "break-all" }}>{this.state.error}</p>
        <div className="form-actions">
          <button type="button" onClick={() => location.reload()} style={btn}>重新载入</button>
          {/* 只清屏幕位置状态;判分作业日志(journal)是研究记录,绝不在这里清 */}
          <button type="button" onClick={() => { clearConsoleWorkspaceState(); location.reload(); }} style={btn}>
            重置屏幕状态并重载
          </button>
        </div>
      </main>
    );
  }
}

// 旧标签页检测:本页编译时的 __BUILD_ID__ ≠ 服务器 dist/build-id.txt ⇒ 这窗口在跑旧代码。
// 只在操作端渲染:老人端不进任何可交互元素(录音中误触"刷新"会销毁整段作答),
// 研究者在操作端看到横幅后顺手刷新老人端窗口即可(README 已注明)。
function StaleBuildBanner() {
  const { pathname } = useLocation();
  const [stale, setStale] = useState(false);
  // 叠层打开时藏起来:横幅在 inert 包装之外,Tab+Enter 会整页 reload,销毁进行中的录音。
  const [overlayOpen, setOverlayOpen] = useState(false);
  useEffect(() => {
    const onToggle = (e: Event) => setOverlayOpen((e as CustomEvent<{ open: boolean }>).detail?.open === true);
    window.addEventListener(PATIENT_VIEW_EVENT, onToggle);
    return () => window.removeEventListener(PATIENT_VIEW_EVENT, onToggle);
  }, []);
  useEffect(() => {
    if (!import.meta.env.PROD) return;
    let stop = false;
    const check = () => {
      fetch("/build-id.txt", { cache: "no-store" })
        .then((r) => (r.ok ? r.text() : null))
        // 文件缺失时 SPA 兜底会回 200+index.html——只认纯数字指纹,防永久假阳性横幅
        .then((t) => { if (!stop && t && /^\d{10,}$/.test(t.trim()) && t.trim() !== __BUILD_ID__) setStale(true); })
        .catch(() => {});
    };
    check();
    const timer = setInterval(check, 30000);
    return () => { stop = true; clearInterval(timer); };
  }, []);
  if (!pathname.startsWith("/console") || !stale || overlayOpen) return null;
  return (
    <button className="stale-build-banner" type="button" onClick={() => location.reload()}>
      界面已更新 · 点击刷新
    </button>
  );
}

// 单机一条流:受试者画面叠层宿主。挂在 /console 路由(与 ConsoleShell 并列),
// ConsoleShell 保持挂载在下层继续写游标/跑自动驾驶;开关状态源在 ConsoleShell,
// 经窗内事件到这——红线守卫禁止 console/** import 老人端源码,叠层只能在这层长出来。
function PatientViewHost() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onToggle = (e: Event) => setOpen((e as CustomEvent<{ open: boolean }>).detail?.open === true);
    window.addEventListener(PATIENT_VIEW_EVENT, onToggle);
    return () => window.removeEventListener(PATIENT_VIEW_EVENT, onToggle);
  }, []);
  const exit = () => {
    // 宿主本地也收拢:操作端若已崩溃(独立 Boundary 接管),退出事件没了接收者,
    // 研究者必须仍能离开叠层去看错误页。
    setOpen(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => { /* 已不在全屏 */ });
    window.dispatchEvent(new Event(PATIENT_VIEW_EXIT_EVENT));
  };
  if (!open) return null;
  return (
    <div className="patient-embed-overlay">
      <Boundary variant="patient">
        <Suspense fallback={<PatientLoading />}><PatientShell /></Suspense>
      </Boundary>
      <HoldToExit onExit={exit} />
    </div>
  );
}

// 研究者退出叠层:按住 2.5 秒 → 屏幕上方另一位置出现确认钮,4 秒内点它才真正返回。
// 两步且异位:受试者"点了没反应就按住"够不成整套动作;多点触控/滑出均有护栏。
function HoldToExit({ onExit }: { onExit: () => void }) {
  const [holding, setHolding] = useState(false);
  const [armed, setArmed] = useState(false);
  const [recActive, setRecActive] = useState(false);
  const [noteCount, setNoteCount] = useState(0);
  const timer = useRef<number | null>(null);
  const disarmTimer = useRef<number | null>(null);
  const pointerId = useRef<number | null>(null);

  useEffect(() => {
    const onRec = (e: Event) => setRecActive((e as CustomEvent<{ active: boolean }>).detail?.active === true);
    const onNote = (e: Event) => setNoteCount((e as CustomEvent<{ count: number }>).detail?.count ?? 0);
    window.addEventListener(PATIENT_VIEW_REC_EVENT, onRec);
    window.addEventListener(CONSOLE_NOTE_EVENT, onNote);
    return () => {
      window.removeEventListener(PATIENT_VIEW_REC_EVENT, onRec);
      window.removeEventListener(CONSOLE_NOTE_EVENT, onNote);
    };
  }, []);
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
    if (disarmTimer.current !== null) clearTimeout(disarmTimer.current);
  }, []);

  const start = (e: React.PointerEvent<HTMLButtonElement>) => {
    // 只认主指针且不可重入:手掌/第二指的 pointerdown 若覆盖计时器,会留下孤儿计时器
    // 在手抬起后照样触发退出——恰好击穿防误触门槛。
    if (!e.isPrimary || timer.current !== null || armed) return;
    pointerId.current = e.pointerId;
    e.currentTarget.setPointerCapture(e.pointerId);
    setHolding(true);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      pointerId.current = null;
      setHolding(false);
      setArmed(true);
      disarmTimer.current = window.setTimeout(() => { disarmTimer.current = null; setArmed(false); }, 4000);
    }, 2500);
  };
  const cancel = (e: React.PointerEvent) => {
    if (pointerId.current !== null && e.pointerId !== pointerId.current) return;
    pointerId.current = null;
    setHolding(false);
    if (timer.current !== null) { clearTimeout(timer.current); timer.current = null; }
  };
  const move = (e: React.PointerEvent<HTMLButtonElement>) => {
    // 触屏隐式 pointer capture 会吃掉 pointerleave:滑出按钮范围就地取消按住。
    if (pointerId.current === null || e.pointerId !== pointerId.current) return;
    const r = e.currentTarget.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) cancel(e);
  };

  if (armed) {
    return (
      <button type="button" className="patient-exit-hold is-armed" aria-label="确认返回操作端"
        onClick={() => {
          setArmed(false);
          if (disarmTimer.current !== null) { clearTimeout(disarmTimer.current); disarmTimer.current = null; }
          onExit();
        }}>
        点此返回操作端
      </button>
    );
  }
  return (
    <button type="button" className={`patient-exit-hold${holding ? " is-holding" : ""}`}
      aria-label={`研究者按住 2.5 秒后确认返回操作端${noteCount > 0 ? ";有待处理提示" : ""}`}
      onPointerDown={start} onPointerUp={cancel} onPointerCancel={cancel} onPointerMove={move}
      onContextMenu={(e) => e.preventDefault()}>
      <span aria-hidden="true">{holding && recActive ? "录音中 · " : ""}研究者 · 按住返回</span>
      {noteCount > 0 && <span className="patient-exit-hold__note" aria-hidden="true" />}
      <span className="patient-exit-hold__bar" aria-hidden="true" />
    </button>
  );
}

function Landing() {
  return (
    <main className="landing-shell">
      <div className="landing-brand" aria-hidden>语</div>
      <div className="page-kicker">南京医科大学 · 研究开发版</div>
      <h1>语言沟通训练系统</h1>
      <p className="landing-lead">AI 可辅助收音、转写、判类、提示和推进；研究者随时接管，并对正式研究分异步复核锁定。</p>
      <div className="landing-grid">
        <Link to="/console" className="landing-card">
          <span className="landing-card-label">研究者操作端</span>
          <strong>准备、AI 训练与研究复核</strong>
          <span>用于测试前建档、监看或接管训练、回看录音，并完成人工研究真值锁定。</span>
        </Link>
        <Link to="/patient" className="landing-card patient">
          <span className="landing-card-label">受试者呈现端</span>
          <strong>题目呈现与回答</strong>
          <span>建议在外接触摸屏或另一窗口打开，由研究者协助开始。</span>
        </Link>
      </div>
      <p className="landing-note">
        一台设备即可完成全程：进入操作端建好场次，点「受试者画面」全屏切给受试者，研究者按住角落按钮返回。
        也支持双窗/双设备同步运行。
        首次在新设备或新房间使用，先跑一遍<Link to="/device-check">设备自检</Link>（约 20 秒，测麦克风、噪声、网络与播放）。
      </p>
      <p className="landing-note">
        当前仅限模拟预演和受控技术验证，不得用于正式受试者采集。启用云服务后，固定话术、回答音频或回答文本会按所用能力发送至第三方云端；AI 实时判断不是正式研究评分。
      </p>
    </main>
  );
}

const btn: React.CSSProperties = {
  minHeight: 44, padding: "8px 18px", borderRadius: 10, border: "1px solid var(--c-line)",
  background: "var(--c-surface)", cursor: "pointer", fontWeight: 600,
};
