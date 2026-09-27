import { useState } from "react";
import { api, type Json } from "../api";
import { useProject } from "../App";
import { Card, ErrorBox, Pill, Stat, useAction, useLoad } from "../components/ui";

const LEVELS: [string, string][] = [
  ["L0", "仅工具：检索、去重、格式整理；不参与内容判断"],
  ["L1", "证据辅助（默认）：题录预筛、证据卡、证据地图、问题清单"],
  ["L2", "结构与审阅：+ 段落论证骨架（不成句）、草稿批判性审阅"],
  ["L3", "语言建议：+ 对已写段落提出修订模式语言建议，逐条接受"],
];

export default function Overview() {
  const { project, reload } = useProject();
  const pid = project.id;
  const gates = useLoad<Json>(`/api/projects/${pid}/gates`);
  const prisma = useLoad<Json>(`/api/projects/${pid}/prisma`);
  const isMentor = ["mentor", "admin"].includes(project.my_role);
  const act = useAction();
  const [member, setMember] = useState({ email: "", role: "mentor" });
  const [comment, setComment] = useState("");
  const save = (b: Json) => act.run(async () => { await api.put(`/api/projects/${pid}/settings`, b); reload(); });

  return (
    <>
      <h1>{project.name}</h1>
      <p className="muted">导师原题：{project.mentor_topic || "—"} · 任务书 v{project.charter_version}（{project.charter_status}）</p>
      <ErrorBox error={act.error} />
      <Card title="人工闸门 G0–G5">
        <table>
          <thead><tr><th>闸门</th><th>内容</th><th>状态</th><th>对象版本</th><th></th></tr></thead>
          <tbody>
            {gates.data && Object.entries(gates.data.status).map(([g, s]: [string, Json]) => (
              <tr key={g}>
                <td><b>{g}</b></td>
                <td>{s.name}</td>
                <td><Pill kind={s.status === "approved" ? "ok" : s.status === "submitted" ? "warn" : s.status === "rejected" ? "bad" : undefined}>{s.status}</Pill></td>
                <td className="small">{s.object_version || "—"}</td>
                <td>
                  {g !== "G0" && g !== "G3" && s.status !== "submitted" && !isMentor && (
                    <button className="small" onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/gates/${g}/submit`, { object_version: `${g.toLowerCase()}@${new Date().toISOString().slice(0, 10)}`, comment }); gates.reload(); })}>提交审批</button>
                  )}
                  {isMentor && s.status === "submitted" && g !== "G0" && (
                    <span className="row">
                      <button className="small primary" onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/gates/${g}/decide`, { decision: "approved", comment }); gates.reload(); })}>批准</button>
                      <button className="small danger" onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/gates/${g}/decide`, { decision: "rejected", comment }); gates.reload(); })}>退回</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <label>审批意见 / 提交说明</label>
        <input value={comment} onChange={(e) => setComment(e.target.value)} />
        <p className="muted small">G0 在“任务书”页审批；G3 在“证据与大纲”页提交。每道闸门记录决策人、时间和意见。</p>
      </Card>

      {prisma.data && (
        <Card title="进度">
          <div className="grid3">
            <Stat label="检索命中（数据库）" value={prisma.data.identified_database_total} />
            <Stat label="去重后待筛" value={prisma.data.records_screened} />
            <Stat label="已纳入" value={prisma.data.included} />
          </div>
        </Card>
      )}

      <Card title="AI 介入级别与隐私（由导师设定）">
        {LEVELS.map(([l, d]) => (
          <div key={l} className="row" style={{ marginBottom: 4 }}>
            <input type="radio" style={{ width: "auto" }} checked={project.ai_level_max === l} disabled={!isMentor} onChange={() => save({ ai_level_max: l })} />
            <b>{l}</b> <span className="muted">{d}</span>
          </div>
        ))}
        <div className="row muted small">不提供：由 AI 直接生成段落或章节正文并插入稿件——平台有意不支持。</div>
        <div className="row" style={{ marginTop: 10 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={project.data_local_only} disabled={!isMentor} onChange={(e) => save({ data_local_only: e.target.checked })} />
          数据不外发（强制只调用本地模型；适用于未发表数据、患者信息、基金申请内容）
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <span>AI 费用预算上限</span>
          <input style={{ width: 120 }} type="number" defaultValue={project.budget_limit ?? ""} disabled={!isMentor} onBlur={(e) => save({ budget_limit: e.target.value === "" ? null : Number(e.target.value) })} />
        </div>
        {!isMentor && <p className="muted small">学生不能自行调高介入级别。</p>}
      </Card>

      <Card title="成员">
        <table><tbody>
          <tr><td>{project.owner?.name}</td><td>{project.owner?.email}</td><td>作者（学生）</td></tr>
          {project.members.map((m: Json) => <tr key={m.user_id}><td>{m.name}</td><td>{m.email}</td><td>{m.role === "mentor" ? "导师" : "学生"}</td></tr>)}
        </tbody></table>
        <div className="row" style={{ marginTop: 8 }}>
          <input style={{ width: 240 }} placeholder="成员邮箱" value={member.email} onChange={(e) => setMember({ ...member, email: e.target.value })} />
          <select style={{ width: 100 }} value={member.role} onChange={(e) => setMember({ ...member, role: e.target.value })}><option value="mentor">导师</option><option value="student">学生</option></select>
          <button onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/members`, member); reload(); })}>添加</button>
          <button onClick={() => api.download(`/api/projects/${pid}/export/package.zip`, `${pid}.zip`)}>导出项目包</button>
        </div>
      </Card>
    </>
  );
}
