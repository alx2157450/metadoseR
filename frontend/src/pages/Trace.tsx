import { useState } from "react";
import { api, type Json } from "../api";
import { useProject } from "../App";
import { Card, Pill, useLoad } from "../components/ui";

export default function Trace() {
  const { project } = useProject();
  const P = `/api/projects/${project.id}`;
  const runs = useLoad<Json[]>(`${P}/runs?limit=200`);
  const audit = useLoad<Json[]>(`/api/admin/audit?project_id=${project.id}&limit=300`);
  const jobs = useLoad<Json[]>(`${P}/jobs`);
  const [detail, setDetail] = useState<Json>(null);
  return (
    <>
      <h1>留痕：AI 调用日志与审计日志</h1>
      <p className="muted">每次 AI 调用记录模型、版本、Prompt 版本、采样参数与输入输出哈希；每个人工决定记录决策人与时间。审计日志追加写入，哈希链防篡改。</p>
      <Card title="AI 调用（ModelRun）">
        <table>
          <thead><tr><th>时间</th><th>任务</th><th>Prompt 版本</th><th>提供方 / 模型</th><th>温度</th><th>token</th><th>费用</th><th>状态</th></tr></thead>
          <tbody>
            {(runs.data || []).map((r) => (
              <tr key={r.run_id} style={{ cursor: "pointer" }} onClick={() => api.get(`${P}/runs/${r.run_id}`).then(setDetail)}>
                <td className="small">{r.at?.slice(0, 19)}</td><td>{r.task}</td><td className="small">{r.prompt_version}</td>
                <td className="small">{r.provider} / {r.model} {r.is_local && <Pill kind="info">本地</Pill>}</td>
                <td>{r.temperature}</td><td className="small">{r.tokens_in} / {r.tokens_out}</td><td className="small">{r.cost.toFixed(4)}</td>
                <td><Pill kind={r.status === "ok" ? "ok" : "bad"}>{r.status}</Pill></td>
              </tr>
            ))}
          </tbody>
        </table>
        {detail && (
          <div className="card" style={{ marginTop: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}><b>{detail.run_id}</b><button className="small" onClick={() => setDetail(null)}>关闭</button></div>
            <div className="small muted">input {detail.input_hash}<br />output {detail.output_hash}</div>
            <h3>输入</h3><pre className="md small" style={{ maxHeight: 300, overflow: "auto" }}>{detail.input_text}</pre>
            <h3>输出</h3><pre className="md small" style={{ maxHeight: 300, overflow: "auto" }}>{detail.output_text}</pre>
            {detail.error && <div className="err">{detail.error}</div>}
          </div>
        )}
      </Card>
      <Card title="后台任务">
        <table><tbody>{(jobs.data || []).map((j) => <tr key={j.id}><td>#{j.id}</td><td>{j.kind}</td><td><Pill kind={j.status === "done" ? "ok" : j.status === "failed" ? "bad" : "warn"}>{j.status}</Pill></td><td className="small">{j.progress?.done ?? ""}/{j.progress?.total ?? ""}</td><td className="small">{j.error?.slice(0, 200)}</td></tr>)}</tbody></table>
      </Card>
      <Card title="审计日志">
        <table>
          <thead><tr><th>时间</th><th>操作</th><th>对象</th><th>详情</th></tr></thead>
          <tbody>{(audit.data || []).map((a) => <tr key={a.id}><td className="small">{a.at?.slice(0, 19)}</td><td>{a.action}</td><td className="small">{a.object_type} {a.object_id}</td><td className="small">{JSON.stringify(a.detail)}</td></tr>)}</tbody>
        </table>
      </Card>
    </>
  );
}
