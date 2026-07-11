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
        <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 24, textAlign: "center", padding: 24 }}>
          <div style={{ fontSize: "2em", fontWeight: 700 }}>请稍等一下</div>
          <div style={{ opacity: 0.7 }}>请研究者过来看一眼</div>
          <button type="button" onClick={() => location.reload()}
            style={{ minHeight: 72, minWidth: 220, fontSize: "1.1em", borderRadius: 12, border: "1px solid var(--c-border)", cursor: "pointer" }}>
            重新载入
          </button>
        </div>
      );
    }
    return (
      <div style={{ maxWidth: 640, margin: "10vh auto", padding: 24 }}>
        <h2>界面遇到问题,已停住</h2>
        <p className="muted" style={{ wordBreak: "break-all" }}>{this.state.error}</p>
        <div className="row" style={{ marginTop: 16, gap: 12 }}>
          <button type="button" onClick={() => location.reload()} style={btn}>重新载入</button>
          {/* 只清屏幕位置状态;判分作业日志(journal)是研究记录,绝不在这里清 */}
          <button type="button" onClick={() => { try { localStorage.removeItem("nmu:console:state"); } catch { /* noop */ } location.reload(); }} style={btn}>
            重置屏幕状态并重载
          </button>
        </div>
      </div>
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
    <button type="button" onClick={() => location.reload()}
      style={{ position: "fixed", top: 10, left: "50%", transform: "translateX(-50%)", zIndex: 1200,
        padding: "10px 20px", borderRadius: 999, border: "none", cursor: "pointer", fontWeight: 700,
        background: "var(--c-primary)", color: "#fff", boxShadow: "0 4px 12px rgba(0,0,0,.25)" }}>
      界面已更新 · 点击刷新
    </button>
  );
}

function Landing() {
  return (
    <div style={{ maxWidth: 640, margin: "0 auto", padding: "var(--sp-8) var(--sp-5)", textAlign: "center" }}>
      <h1>语言沟通训练系统</h1>
      <p className="muted">南京医科大学 · 轻中度阿尔茨海默病 · 本地仪器 M0</p>
      <div className="row" style={{ justifyContent: "center", marginTop: "var(--sp-6)" }}>
        <Link to="/console" style={link}>操作端(研究者)</Link>
        <Link to="/patient" style={link}>老人端(受试者)</Link>
      </div>
      <p className="muted" style={{ marginTop: "var(--sp-6)", fontSize: "0.9em" }}>
        建议同机双窗:操作端一窗、老人端一窗(外接触摸屏),两窗经本地广播同步,全程离线。
      </p>
    </div>
  );
}

const btn: React.CSSProperties = {
  minHeight: 44, padding: "8px 18px", borderRadius: 10, border: "1px solid var(--c-border)",
  background: "var(--c-surface)", cursor: "pointer", fontWeight: 600,
};

const link: React.CSSProperties = {
  display: "inline-block", padding: "var(--sp-4) var(--sp-6)", borderRadius: "var(--radius)",
  background: "var(--c-primary)", color: "var(--c-on-primary)", textDecoration: "none", fontWeight: 700, fontSize: "1.2em",
};
