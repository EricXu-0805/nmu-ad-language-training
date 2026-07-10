import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { ConsoleShell } from "./console/ConsoleShell";
import { PatientShell } from "./patient/PatientShell";

// 两路由:/console 操作端 · /patient 老人端。零外链(硬约束3)。SPA fallback 由 main.py 提供,clean URL 可刷新。
export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/console" element={<ConsoleShell />} />
        <Route path="/patient" element={<PatientShell />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
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

const link: React.CSSProperties = {
  display: "inline-block", padding: "var(--sp-4) var(--sp-6)", borderRadius: "var(--radius)",
  background: "var(--c-primary)", color: "var(--c-on-primary)", textDecoration: "none", fontWeight: 700, fontSize: "1.2em",
};
