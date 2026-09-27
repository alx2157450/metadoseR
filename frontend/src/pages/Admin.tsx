import { useState } from "react";
import { api, type Json } from "../api";
import { Card, ErrorBox, Pill, useAction, useLoad } from "../components/ui";

const EMPTY = { name: "", kind: "openai_compatible", base_url: "", api_key: "", default_model: "", is_local: false, price_in: 0, price_out: 0, context_tokens: 32000 };
const PRESETS: [string, Json][] = [
  ["DeepSeek", { name: "deepseek", base_url: "https://api.deepseek.com/v1", default_model: "deepseek-chat", context_tokens: 64000 }],
  ["通义千问", { name: "qwen", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", default_model: "qwen-plus", context_tokens: 128000 }],
  ["Ollama（本机）", { name: "ollama-local", base_url: "http://localhost:11434/v1", default_model: "qwen2.5:14b", is_local: true, api_key: "ollama" }],
  ["vLLM（课题组服务器）", { name: "vllm", base_url: "http://vllm:8000/v1", default_model: "Qwen/Qwen2.5-32B-Instruct", is_local: true }],
  ["LiteLLM 代理", { name: "litellm", base_url: "http://litellm:4000/v1", default_model: "gpt-4o" }],
];

export default function Admin() {
  const users = useLoad<Json[]>("/api/users");
  const prov = useLoad<Json>("/api/admin/providers");
  const routes = useLoad<Json[]>("/api/admin/routes");
  const prompts = useLoad<Json[]>("/api/prompts");
  const usage = useLoad<Json[]>("/api/admin/usage");
  const [u, setU] = useState({ email: "", name: "", password: "", role: "student" });
  const [p, setP] = useState<Json>(EMPTY);
  const [r, setR] = useState({ task: "*", provider: "rules", model: "", local_fallback: "" });
  const [chain, setChain] = useState<Json>(null);
  const act = useAction();
  const names = [...(prov.data?.builtin || []), ...(prov.data?.configured || [])].map((x: Json) => x.name);

  return (
    <>
      <h1>平台管理</h1>
      <ErrorBox error={act.error} />
      <Card title="X1 用户与角色">
        <table><thead><tr><th>姓名</th><th>邮箱</th><th>角色</th><th>状态</th></tr></thead><tbody>
          {(users.data || []).map((x) => (
            <tr key={x.id}><td>{x.name}</td><td>{x.email}</td>
              <td><select value={x.role} onChange={(e) => act.run(async () => { await api.patch(`/api/users/${x.id}`, { role: e.target.value }); users.reload(); })}><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option></select></td>
              <td><button className="small" onClick={() => act.run(async () => { await api.patch(`/api/users/${x.id}`, { active: !x.active }); users.reload(); })}>{x.active ? "停用" : "启用"}</button></td></tr>
          ))}
        </tbody></table>
        <div className="row" style={{ marginTop: 8 }}>
          <input style={{ width: 200 }} placeholder="邮箱" value={u.email} onChange={(e) => setU({ ...u, email: e.target.value })} />
          <input style={{ width: 120 }} placeholder="姓名" value={u.name} onChange={(e) => setU({ ...u, name: e.target.value })} />
          <input style={{ width: 140 }} placeholder="初始密码" type="password" value={u.password} onChange={(e) => setU({ ...u, password: e.target.value })} />
          <select style={{ width: 100 }} value={u.role} onChange={(e) => setU({ ...u, role: e.target.value })}><option value="student">学生</option><option value="mentor">导师</option><option value="admin">管理员</option></select>
          <button className="primary" onClick={() => act.run(async () => { await api.post("/api/users", u); users.reload(); setU({ ...u, email: "", name: "", password: "" }); })}>创建</button>
        </div>
      </Card>

      <Card title="X2 模型网关：提供方（OpenAI 兼容接口，密钥加密存储）">
        <table><thead><tr><th>名称</th><th>地址</th><th>默认模型</th><th>本地</th><th>价格（/百万 token）</th><th></th></tr></thead><tbody>
          {(prov.data?.builtin || []).map((x: Json) => <tr key={x.name}><td>{x.name} <Pill>内置</Pill></td><td className="small">{x.base_url || "—"}</td><td>{x.default_model}</td><td>{x.is_local ? "是" : "否"}</td><td>—</td><td /></tr>)}
          {(prov.data?.configured || []).map((x: Json) => (
            <tr key={x.name}><td>{x.name}</td><td className="small">{x.base_url}</td><td>{x.default_model}</td><td>{x.is_local ? "是" : "否"}</td><td className="small">{x.price_in} / {x.price_out}</td>
              <td className="row">
                <button className="small" onClick={() => act.run(async () => { const t = await api.post(`/api/admin/providers/${x.name}/test`); alert(`连通：${t.reply}`); })}>测试</button>
                <button className="small" onClick={() => setP({ ...EMPTY, ...x, api_key: "" })}>编辑</button>
                <button className="small danger" onClick={() => act.run(async () => { await api.del(`/api/admin/providers/${x.name}`); prov.reload(); })}>删除</button>
              </td></tr>
          ))}
        </tbody></table>
        <div className="row" style={{ marginTop: 8 }}>{PRESETS.map(([l, v]) => <button key={l} className="small" onClick={() => setP({ ...EMPTY, ...v })}>{l}</button>)}</div>
        <div className="grid3">
          <div><label>名称</label><input value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} /></div>
          <div><label>Base URL</label><input value={p.base_url} onChange={(e) => setP({ ...p, base_url: e.target.value })} /></div>
          <div><label>默认模型</label><input value={p.default_model} onChange={(e) => setP({ ...p, default_model: e.target.value })} /></div>
          <div><label>API Key（留空不修改）</label><input type="password" value={p.api_key} onChange={(e) => setP({ ...p, api_key: e.target.value })} /></div>
          <div><label>上下文长度（token，用于自动分批）</label><input type="number" value={p.context_tokens} onChange={(e) => setP({ ...p, context_tokens: Number(e.target.value) })} /></div>
          <div><label>输入 / 输出价格</label><div className="row"><input type="number" style={{ width: 90 }} value={p.price_in} onChange={(e) => setP({ ...p, price_in: Number(e.target.value) })} /><input type="number" style={{ width: 90 }} value={p.price_out} onChange={(e) => setP({ ...p, price_out: Number(e.target.value) })} /></div></div>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={p.is_local} onChange={(e) => setP({ ...p, is_local: e.target.checked })} /> 本地模型（“数据不外发”项目只会路由到本地模型）
          <button className="primary" disabled={!p.name} onClick={() => act.run(async () => { await api.post("/api/admin/providers", { ...p, api_key: p.api_key || null }); prov.reload(); setP(EMPTY); })}>保存提供方</button>
        </div>
      </Card>

      <Card title="按任务路由（如：筛选用本地模型、审阅用云端强模型）">
        <table><thead><tr><th>任务</th><th>提供方</th><th>模型</th><th>本地回退</th><th></th></tr></thead><tbody>
          {(routes.data || []).map((x) => <tr key={x.task}><td>{x.task}</td><td>{x.provider}</td><td>{x.model || "（默认）"}</td><td>{x.local_fallback || "—"}</td><td><button className="small danger" onClick={() => act.run(async () => { await api.del(`/api/admin/routes/${encodeURIComponent(x.task)}`); routes.reload(); })}>删除</button></td></tr>)}
        </tbody></table>
        <div className="row" style={{ marginTop: 8 }}>
          <select style={{ width: 160 }} value={r.task} onChange={(e) => setR({ ...r, task: e.target.value })}><option value="*">* 全部任务</option>{(prompts.data || []).map((x) => <option key={x.id} value={x.id}>{x.id} {x.name}</option>)}</select>
          <select style={{ width: 160 }} value={r.provider} onChange={(e) => setR({ ...r, provider: e.target.value })}>{names.map((n) => <option key={n}>{n}</option>)}</select>
          <input style={{ width: 200 }} placeholder="模型（留空用默认）" value={r.model} onChange={(e) => setR({ ...r, model: e.target.value })} />
          <select style={{ width: 160 }} value={r.local_fallback} onChange={(e) => setR({ ...r, local_fallback: e.target.value })}><option value="">本地回退…</option>{names.map((n) => <option key={n}>{n}</option>)}</select>
          <button className="primary" onClick={() => act.run(async () => { await api.put("/api/admin/routes", r); routes.reload(); })}>保存路由</button>
        </div>
        <p className="muted small">默认提供方：{prov.data?.default}。“rules” 为离线规则引擎，可在没有任何大模型时跑通流程。</p>
      </Card>

      <Card title="X3 Prompt 注册表">
        <table><thead><tr><th>编号@版本</th><th>名称</th><th>阶段</th><th>最低级别</th><th>输出</th><th>温度</th></tr></thead><tbody>
          {(prompts.data || []).map((x) => <tr key={x.id}><td>{x.ref}</td><td>{x.name}</td><td>{x.stage}</td><td>{x.min_level}</td><td>{x.output}</td><td>{x.temperature}</td></tr>)}
        </tbody></table>
      </Card>

      <Card title="X6 用量与成本">
        <table><thead><tr><th>项目</th><th>用户</th><th>提供方 / 模型</th><th>调用</th><th>token（入 / 出）</th><th>费用</th></tr></thead><tbody>
          {(usage.data || []).map((x, i) => <tr key={i}><td className="small">{x.project_id}</td><td className="small">{x.user_id}</td><td>{x.provider} / {x.model}</td><td>{x.runs}</td><td>{x.tokens_in} / {x.tokens_out}</td><td>{x.cost}</td></tr>)}
        </tbody></table>
      </Card>

      <Card title="X4 审计日志完整性">
        <button onClick={() => act.run(async () => setChain(await api.get("/api/admin/audit/verify")))}>校验哈希链</button>
        {chain && (chain.ok ? <Pill kind="ok">完整：{chain.checked} 条</Pill> : <Pill kind="bad">在 #{chain.broken_at} 处断裂</Pill>)}
      </Card>
    </>
  );
}
