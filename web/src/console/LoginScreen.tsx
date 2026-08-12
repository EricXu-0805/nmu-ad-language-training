import { useState } from "react";
import { api, ApiError } from "../api";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Field, TextInput } from "../components/Field";

// console 账号登录门(公网部署)。工作人员操作端用;老人端从不走账号,只用 PIN 保底。
// 登录成功 → onLoggedIn() 触发上层重新评估身份并进入工作台。
export function LoginScreen({ onLoggedIn }: { onLoggedIn: () => void | Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) { setErr("请输入用户名和密码"); return; }
    setBusy(true); setErr(null);
    try {
      await api.authLogin(username.trim(), password);
      setPassword("");
      await onLoggedIn();
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) setErr("尝试过多，请稍后再试");
      else if (e instanceof ApiError && e.status === 401) setErr("用户名或密码错误");
      else setErr(e instanceof ApiError ? e.detail : "登录失败，请检查服务连接");
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand" aria-hidden>语</div>
        <div className="page-kicker">南京医科大学 · 工作人员操作端</div>
        <h1 className="login-title">登录</h1>
        <p className="muted">请用管理员为您开通的工作人员账号登录。老人画面无需登录。</p>
        {err && <Alert tone="danger" title="无法登录">{err}</Alert>}
        <Field label="用户名">
          <TextInput value={username} onChange={(e) => setUsername(e.target.value)}
            autoComplete="username" autoFocus disabled={busy} />
        </Field>
        <Field label="密码">
          <TextInput type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" disabled={busy} />
        </Field>
        <Button type="submit" variant="primary" disabled={busy}>{busy ? "正在登录…" : "登录"}</Button>
      </form>
    </main>
  );
}
