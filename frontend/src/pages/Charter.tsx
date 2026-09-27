import { useEffect, useState } from "react";
import { api, type Json } from "../api";
import { useProject } from "../App";
import { AiTask, Card, ErrorBox, Pill, useAction, useLoad } from "../components/ui";

const lines = (v: Json) => (Array.isArray(v) ? v.join("\n") : v || "");
const toLines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

export default function Charter() {
  const { project, reload: reloadProject } = useProject();
  const pid = project.id;
  const versions = useLoad<Json[]>(`/api/projects/${pid}/charter`);
  const cur = versions.data?.[0];
  const [d, setD] = useState<Json>({});
  const [scopingQ, setScopingQ] = useState("");
  const [scoping, setScoping] = useState<Json>(null);
  const act = useAction();
  const isMentor = ["mentor", "admin"].includes(project.my_role);

  useEffect(() => { if (cur) setD(cur.data); }, [cur?.id, cur?.version]);
  const set = (k: string, v: Json) => setD({ ...d, [k]: v });
  const locked = cur && cur.status !== "draft";

  const save = () => act.run(async () => { await api.post(`/api/projects/${pid}/charter`, { data: d }); await versions.reload(); reloadProject(); });
  const concepts: Json[] = d.concepts || [];
  const journals: Json[] = d.target_journals || [];

  return (
    <>
      <h1>阶段一 · 导师命题解析与《综述任务书》</h1>
      <p className="muted">导师给题 → 题目解构（P01）→ 问题清单面谈（P02）→ 快速摸底 → 重叠分析（P03/P04）→ 切入角度（P05）→ 任务书 → G0 导师批准。任务书是后续所有阶段的“合同”。</p>
      <ErrorBox error={act.error} />
      {cur && (
        <div className="note">
          当前 v{cur.version} <Pill kind={cur.status === "approved" ? "ok" : cur.status === "submitted" ? "warn" : undefined}>{cur.status}</Pill>
          {locked && " · 已锁定，保存修改将生成新版本并需重新走 G0 审批。"}
        </div>
      )}

      <Card title="任务书（结构化表单）" right={
        <>
          <button className="primary" disabled={act.busy} onClick={save}>{locked ? "另存为新版本" : "保存草稿"}</button>
          {cur?.status === "draft" && <button disabled={act.busy} onClick={() => act.run(async () => { await save(); await api.post(`/api/projects/${pid}/charter/${cur.version}/submit`); versions.reload(); reloadProject(); })}>提交导师审批（G0）</button>}
          {isMentor && ["submitted", "draft"].includes(cur?.status) && (
            <>
              <button className="primary" onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/charter/${cur.version}/decide`, { decision: "approved" }); versions.reload(); reloadProject(); })}>导师批准</button>
              <button className="danger" onClick={() => act.run(async () => { await api.post(`/api/projects/${pid}/charter/${cur.version}/decide`, { decision: "rejected", comment: prompt("退回意见") || "" }); versions.reload(); })}>退回</button>
            </>
          )}
        </>
      }>
        <div className="grid2">
          <div><label>导师原题（逐字，不要改写）</label><input value={d.mentor_topic || ""} onChange={(e) => set("mentor_topic", e.target.value)} /></div>
          <div><label>学科</label><input value={d.discipline || ""} placeholder="如：心血管病学" onChange={(e) => set("discipline", e.target.value)} /></div>
          <div><label>命题背景与用途</label><textarea value={d.background || ""} onChange={(e) => set("background", e.target.value)} /></div>
          <div><label>工作标题</label><textarea value={d.working_title || ""} onChange={(e) => set("working_title", e.target.value)} /></div>
          <div>
            <label>综述类型</label>
            <select value={d.review_type || "narrative"} onChange={(e) => set("review_type", e.target.value)}>
              <option value="narrative">叙述性综述（SANRA）</option>
              <option value="scoping">范围综述（PRISMA-ScR）</option>
              <option value="systematic">系统综述（PRISMA 2020，双人独立筛选）</option>
              <option value="meta">Meta 分析</option>
              <option value="umbrella">伞状综述（PRIOR）</option>
              <option value="mini">迷你综述 / 观点</option>
            </select>
          </div>
          <div><label>规模（字数、参考文献数）</label><input value={d.scale || ""} placeholder="6000–8000 词，120–150 篇" onChange={(e) => set("scale", e.target.value)} /></div>
          <div><label>核心问题（每行一个，3–5 个）</label><textarea value={lines(d.core_questions)} onChange={(e) => set("core_questions", toLines(e.target.value))} /></div>
          <div><label>种子文献 PMID / DOI（每行一个，用于召回检验）</label><textarea value={lines(d.seed_ids)} onChange={(e) => set("seed_ids", toLines(e.target.value))} /></div>
          <div><label>纳入边界（每行一条，将直接转为筛选标准）</label><textarea value={lines(d.include)} onChange={(e) => set("include", toLines(e.target.value))} /></div>
          <div><label>排除边界（每行一条）</label><textarea value={lines(d.exclude)} onChange={(e) => set("exclude", toLines(e.target.value))} /></div>
          <div><label>时间窗与语言</label><input value={d.time_language || ""} onChange={(e) => set("time_language", e.target.value)} /></div>
          <div><label>里程碑</label><input value={d.milestones || ""} placeholder="G1 第 2 周末；G2 第 4 周末…" onChange={(e) => set("milestones", e.target.value)} /></div>
          <div><label>课题组项目 / 实验方向</label><input value={d.lab_direction || ""} onChange={(e) => set("lab_direction", e.target.value)} /></div>
          <div><label>截止时间</label><input value={d.deadline || ""} onChange={(e) => set("deadline", e.target.value)} /></div>
        </div>

        <h3>核心概念（检索语义群，按 B 核心对象 / A 情境 / C 现象 或 PICO 拆解）</h3>
        <table>
          <thead><tr><th>概念</th><th>角色</th><th>自由词（逗号分隔）</th><th>MeSH（逗号分隔）</th><th></th></tr></thead>
          <tbody>
            {concepts.map((c, i) => (
              <tr key={i}>
                <td><input value={c.name} onChange={(e) => set("concepts", concepts.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} /></td>
                <td style={{ width: 90 }}><select value={c.role || "B"} onChange={(e) => set("concepts", concepts.map((x, j) => (j === i ? { ...x, role: e.target.value } : x)))}><option>B</option><option>A</option><option>C</option><option>P</option><option>I</option><option>O</option></select></td>
                <td><input value={(c.terms || []).join(", ")} onChange={(e) => set("concepts", concepts.map((x, j) => (j === i ? { ...x, terms: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } : x)))} /></td>
                <td><input value={(c.mesh || []).join(", ")} onChange={(e) => set("concepts", concepts.map((x, j) => (j === i ? { ...x, mesh: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } : x)))} /></td>
                <td><button className="small danger" onClick={() => set("concepts", concepts.filter((_, j) => j !== i))}>删除</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="small" style={{ marginTop: 6 }} onClick={() => set("concepts", [...concepts, { name: "", role: "B", terms: [], mesh: [] }])}>+ 概念</button>

        <h3>目标期刊</h3>
        {journals.map((j, i) => (
          <div className="row" key={i} style={{ marginBottom: 4 }}>
            <input style={{ width: 280 }} placeholder="期刊" value={j.name} onChange={(e) => set("target_journals", journals.map((x, k) => (k === i ? { ...x, name: e.target.value } : x)))} />
            <input style={{ width: 120 }} placeholder="字数上限" value={j.word_limit || ""} onChange={(e) => set("target_journals", journals.map((x, k) => (k === i ? { ...x, word_limit: Number(e.target.value) || null } : x)))} />
            <input style={{ width: 140 }} placeholder="参考文献上限" value={j.ref_limit || ""} onChange={(e) => set("target_journals", journals.map((x, k) => (k === i ? { ...x, ref_limit: Number(e.target.value) || null } : x)))} />
            <button className="small danger" onClick={() => set("target_journals", journals.filter((_, k) => k !== i))}>删除</button>
          </div>
        ))}
        <button className="small" onClick={() => set("target_journals", [...journals, { name: "" }])}>+ 期刊</button>
        <p className="muted small">AI 介入上限由导师在“概览”页设定，批准时写入任务书快照：当前 {project.ai_level_max}。</p>
      </Card>

      <Card title="AI 辅助：读懂题目、摸清家底、找准角度（不替学生换题）">
        <AiTask pid={pid} task="P01" label="题目解构" />
        <AiTask pid={pid} task="P02" label="向导师确认的问题清单" params={{ decomposition: "（见 P01 输出，可粘贴到此参数）" }} />
        <div className="row" style={{ margin: "10px 0" }}>
          <input style={{ flex: 1 }} placeholder="粗颗粒度摸底检索式（PubMed 语法）" value={scopingQ} onChange={(e) => setScopingQ(e.target.value)} />
          <button disabled={!scopingQ || act.busy} onClick={() => act.run(async () => setScoping(await api.post(`/api/projects/${pid}/search/scoping`, { query: scopingQ, years: 10 })))}>快速摸底统计</button>
        </div>
        {scoping && (
          <div className="card">
            近 5 年综述 {scoping.reviews_5y} 篇 · 近 10 年综述 {scoping.reviews_10y} 篇 · 近 10 年原始研究 {scoping.original_10y} 篇
            <div className="note">{scoping.maturity}</div>
            <div className="small muted">年度发文：{Object.entries(scoping.trend).map(([y, n]) => `${y}: ${n}`).join("　")}</div>
          </div>
        )}
        <AiTask pid={pid} task="P03" label="领域摸底分析" hint="需在参数中提供综述题录；也可导出 Prompt 粘贴题录后在网页版 AI 使用" />
        <AiTask pid={pid} task="P04" label="同题综述重叠分析" />
        <AiTask pid={pid} task="P05" label="切入角度建议（题目内约束）" />
      </Card>

      <Card title="版本历史">
        <table>
          <thead><tr><th>版本</th><th>状态</th><th>创建</th><th>批准</th></tr></thead>
          <tbody>{(versions.data || []).map((v) => <tr key={v.id}><td>v{v.version}</td><td>{v.status}</td><td className="small">{v.created_at?.slice(0, 16)}</td><td className="small">{v.approved_at?.slice(0, 16) || "—"}</td></tr>)}</tbody>
        </table>
      </Card>
    </>
  );
}
