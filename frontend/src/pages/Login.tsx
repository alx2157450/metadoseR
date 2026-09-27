import { useState } from "react";
import { api, setToken, type Json } from "../api";
import { ErrorBox, useAction } from "../components/ui";

export default function Login({ meta, onLogin }: { meta: Json; onLogin: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useAction();
  return (
    <div className="center card">
      <h1>综述协作平台</h1>
      <p className="muted small">以导师命题为起点 · 研究者主导写作 · AI 负责检索、证据与审阅</p>
      <form onSubmit={(e) => { e.preventDefault(); run(async () => { const r = await api.post("/api/auth/login", { email, password }); setToken(r.token); onLogin(); }); }}>
        <label>邮箱</label>
        <input value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
        <label>密码</label>
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <ErrorBox error={error} />
        <div style={{ marginTop: 12 }}><button className="primary" disabled={busy}>登录</button></div>
      </form>
      <p className="muted small">部署形态：{meta.profile} · v{meta.version}</p>
    </div>
  );
}
