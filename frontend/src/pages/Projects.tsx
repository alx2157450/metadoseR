import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Json } from "../api";
import { useSession } from "../App";
import { Card, ErrorBox, Pill, useAction, useLoad } from "../components/ui";

export default function Projects() {
  const { user } = useSession();
  const { data, error, reload } = useLoad<Json[]>("/api/projects");
  const [form, setForm] = useState({ name: "", mentor_topic: "", mentor_email: "", student_email: "" });
  const act = useAction();
  const nav = useNavigate();
  return (
    <>
      <h1>我的综述项目</h1>
      <ErrorBox error={error} />
      <Card title="新建项目（A1 命题录入）">
        <div className="grid2">
          <div><label>项目名称</label><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
          <div><label>导师题目原话（逐字记录，不要改写）</label><input value={form.mentor_topic} onChange={(e) => setForm({ ...form, mentor_topic: e.target.value })} /></div>
          {user.role === "mentor" ? (
            <div><label>学生邮箱（项目作者）</label><input value={form.student_email} onChange={(e) => setForm({ ...form, student_email: e.target.value })} /></div>
          ) : (
            <div><label>导师邮箱（可选，单机版可留空）</label><input value={form.mentor_email} onChange={(e) => setForm({ ...form, mentor_email: e.target.value })} /></div>
          )}
        </div>
        <ErrorBox error={act.error} />
        <div className="row" style={{ marginTop: 10 }}>
          <button className="primary" disabled={!form.name || act.busy} onClick={() => act.run(async () => {
            const p = await api.post("/api/projects", { ...form, mentor_email: form.mentor_email || null, student_email: form.student_email || null });
            nav(`/p/${p.id}/charter`);
          })}>创建</button>
          <label className="btn" style={{ margin: 0, color: "var(--ink)", fontSize: 14 }}>
            导入项目包
            <input type="file" accept=".zip" style={{ display: "none" }} onChange={(e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              const fd = new FormData();
              fd.append("file", f);
              act.run(async () => { await api.upload("/api/projects/import-package", fd); reload(); });
            }} />
          </label>
        </div>
      </Card>
      <table>
        <thead><tr><th>项目</th><th>导师原题</th><th>我的角色</th><th>任务书</th><th>AI 上限</th><th>创建</th></tr></thead>
        <tbody>
          {(data || []).map((p) => (
            <tr key={p.id}>
              <td><Link to={`/p/${p.id}`}>{p.name}</Link><div className="muted small">{p.id}</div></td>
              <td>{p.mentor_topic}</td>
              <td>{p.my_role}</td>
              <td>v{p.charter_version} <Pill kind={p.charter_status === "approved" ? "ok" : p.charter_status === "submitted" ? "warn" : undefined}>{p.charter_status}</Pill></td>
              <td>{p.ai_level_max}{p.data_local_only && <> <Pill kind="warn">数据不外发</Pill></>}</td>
              <td className="small muted">{p.created_at?.slice(0, 10)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
