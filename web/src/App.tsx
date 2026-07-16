import { Component, useEffect, useState, type ReactNode } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ConsoleShell } from "./console/ConsoleShell";
import { PatientShell } from "./patient/PatientShell";

// 两路由:/console 操作端 · /patient 老人端。零外链(硬约束3)。SPA fallback 由 main.py 提供,clean URL 可刷新。
export default function App() {
  return (
    <BrowserRouter>
      <StaleBuildBanner />
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/console" element={<Boundary variant="console"><ConsoleShell /></Boundary>} />
        <Route path="/patient" element={<Boundary variant="patient"><PatientShell /></Boundary>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

// 渲染崩溃兜底:没有它,任何一处 render 抛错 = React 卸载整棵树 = 整页按钮全部失灵且无解释。
// 操作端给出错信息+重载入口;老人端保持平和(永不报错、永不闪红),由研究者处理。
class Boundary extends Component<{ variant: "console" | "patient"; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) { return { error: String(e) }; }
  render() {
    if (this.state.error === null) return this.props.children;
    if (this.props.variant === "patient") {
      return (
        <main className="patient-message-screen" aria-live="polite">
          <div className="target">请稍等一下</div>
          <p className="question muted">请研究者过来看一眼</p>
          <button className="patient-primary-action" type="button" onClick={() => location.reload()}>
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
          <button type="button" onClick={() => { try { localStorage.removeItem("nmu:console:state"); } catch { /* noop */ } location.reload(); }} style={btn}>
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
  if (!pathname.startsWith("/console") || !stale) return null;
  return (
    <button className="stale-build-banner" type="button" onClick={() => location.reload()}>
      界面已更新 · 点击刷新
    </button>
  );
}

function Landing() {
  return (
    <main className="landing-shell">
      <div className="landing-brand" aria-hidden>语</div>
      <div className="page-kicker">南京医科大学 · 本地研究工具</div>
      <h1>语言沟通训练系统</h1>
      <p className="landing-lead">为轻中度阿尔茨海默病语言沟通训练提供双端协同、过程记录与人工判分。</p>
      <div className="landing-grid">
        <Link to="/console" className="landing-card">
          <span className="landing-card-label">研究者操作端</span>
          <strong>建档、训练与判分</strong>
          <span>用于研究者控制场次、记录回答和完成正式锁分。</span>
        </Link>
        <Link to="/patient" className="landing-card patient">
          <span className="landing-card-label">受试者呈现端</span>
          <strong>题目呈现与回答</strong>
          <span>建议在外接触摸屏或另一窗口打开，由研究者协助开始。</span>
        </Link>
      </div>
      <p className="landing-note">
        同机双窗运行：操作端一窗，受试者端一窗；两端通过本地连接同步，全程离线。
      </p>
    </main>
  );
}

const btn: React.CSSProperties = {
  minHeight: 44, padding: "8px 18px", borderRadius: 10, border: "1px solid var(--c-line)",
  background: "var(--c-surface)", cursor: "pointer", fontWeight: 600,
};
