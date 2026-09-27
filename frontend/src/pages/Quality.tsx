import { useState } from "react";
import { api, pct, type Json } from "../api";
import { useProject } from "../App";
import { Card, ErrorBox, JobButton, Pill, VERDICT_ZH, useAction, useLoad } from "../components/ui";

const TABS = ["断言—引文核查", "引文真实性与撤稿", "规范自查", "AI 使用声明", "导出交付"];
const VK: Record<string, "ok" | "warn" | "bad" | "info"> = { strong: "ok", partial: "warn", none: "bad", undetermined: "info" };

export default function Quality() {
  const { project } = useProject();
  const [tab, setTab] = useState(0);
  return (
    <>
      <h1>阶段六 · 核查、披露与交付</h1>
      <div className="tabs">{TABS.map((t, i) => <button key={t} className={tab === i ? "on" : ""} onClick={() => setTab(i)}>{t}</button>)}</div>
      {tab === 0 && <Checks pid={project.id} isMentor={["mentor", "admin"].includes(project.my_role)} />}
      {tab === 1 && <Citations pid={project.id} />}
      {tab === 2 && <Checklist pid={project.id} />}
      {tab === 3 && <Disclosure pid={project.id} />}
      {tab === 4 && <Export pid={project.id} />}
    </>
  );
}

function Checks({ pid, isMentor }: { pid: string; isMentor: boolean }) {
  const P = `/api/projects/${pid}`;
  const checks = useLoad<Json>(`${P}/checks`);
  const act = useAction();
  const d = checks.data;
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title="F1 断言—引文核查（P19：AI 预核查 → 学生逐条确认 → 导师 G4 抽查）" right={
        <>
          <JobButton label="核查全部已完成段落" start={() => api.post(`${P}/checks/run`, {})} onDone={() => checks.reload()} />
          {isMentor && <button onClick={() => act.run(async () => { const r = await api.post(`${P}/checks/mentor-sample`); alert(`抽查 ${r.sampled} 条`); checks.reload(); })}>G4 每章随机抽 10 条</button>}
          <button onClick={() => api.download(`${P}/export/check-report.csv`, "check-report.csv")}>导出核查表</button>
        </>
      }>
        <p className="muted small">以“原子断言”为单位；只有摘要时不能判为强匹配。不匹配时必须修正或删除——不要让 AI “再找一篇文献来支撑”。段落修改后相关断言自动退回待核查。</p>
        {d && (
          <div className="row">
            {Object.entries(d.summary).map(([k, v]) => <Pill key={k} kind={VK[k]}>{VERDICT_ZH[k] || k}：{String(v)}</Pill>)}
            <span className="small">待确认 {d.pending} · AI 与人工一致率 {pct(d.ai_human_agreement)}</span>
          </div>
        )}
      </Card>
      <table>
        <thead><tr><th>段落 / 句</th><th>断言</th><th>引用</th><th>AI 判定与原文依据</th><th style={{ width: 170 }}>你的判定</th></tr></thead>
        <tbody>
          {(d?.items || []).map((c: Json) => (
            <tr key={c.check_id} style={c.mentor_sampled ? { outline: "2px solid var(--warn)" } : undefined}>
              <td className="small">{c.paragraph_id}<br />第 {c.sentence} 句{c.mentor_sampled && <><br /><Pill kind="warn">导师抽查</Pill></>}</td>
              <td>{c.claim}</td>
              <td className="small">{c.citekey || "—"}</td>
              <td className="small" style={{ maxWidth: 360 }}>
                <div className="ai">
                  <Pill kind={VK[c.ai_verdict]}>{VERDICT_ZH[c.ai_verdict] || c.ai_verdict}</Pill>
                  {c.quote && <div className="quote">“{c.quote}”（{c.location}）</div>}
                  <div>{c.note}</div>
                  {c.suggestion && <div>建议：{c.suggestion}</div>}
                </div>
              </td>
              <td>
                <select value={c.human_verdict || ""} onChange={(e) => act.run(async () => { await api.patch(`${P}/checks/${c.check_id}`, { human_verdict: e.target.value }); checks.reload(); })}>
                  <option value="">待确认</option>{Object.entries(VERDICT_ZH).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function Citations({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const rep = useLoad<Json>(`${P}/citations/report`);
  const r = rep.data;
  const List = ({ label, items, kind }: { label: string; items: string[]; kind?: "bad" | "warn" }) => (
    <div style={{ marginBottom: 6 }}><Pill kind={items.length ? kind : "ok"}>{label} {items.length}</Pill> <span className="small">{items.join("、")}</span></div>
  );
  return (
    <Card title="F2 引文真实性与撤稿检查" right={<JobButton label="解析全部引用（PubMed / Crossref）" start={() => api.post(`${P}/citations/verify`)} onDone={() => rep.reload()} />}>
      {r && (
        <>
          <p>正文共引用 <b>{r.cited_total}</b> 篇 {r.ok ? <Pill kind="ok">硬性检查通过</Pill> : <Pill kind="bad">存在必须处理的问题</Pill>}</p>
          <List label="文献库中不存在" items={r.not_in_library} kind="bad" />
          <List label="无 PMID / DOI" items={r.no_identifier} kind="bad" />
          <List label="无法解析或元数据不一致（视为可疑，人工核实）" items={r.unresolved_or_mismatch} kind="bad" />
          <List label="已撤稿" items={r.retracted} kind="bad" />
          <List label="关注声明 / 更正" items={r.concern_or_correction} kind="warn" />
          <List label="尚未解析" items={r.unchecked} kind="warn" />
          <List label="预印本（检查是否已正式发表）" items={r.preprints} kind="warn" />
          <List label="引用的综述（原创发现请改引原始研究）" items={r.reviews_cited} kind="warn" />
          <h3>集中度</h3>
          <div className="small">期刊：{r.top_journals.map((j: Json) => `${j.journal} ${pct(j.share)}`).join("；")}</div>
          <div className="small">第一作者：{r.top_first_authors.map((j: Json) => `${j.author} ${pct(j.share)}`).join("；")}</div>
        </>
      )}
    </Card>
  );
}

const LISTS: Record<string, string[]> = {
  body: ["每个论点都有引用支撑，没有“空洞断言”", "引用格式统一为 [@citekey]，导出时由 CSL 统一渲染", "数据引用与原文一致（已通过核查）", "没有超出原文的概括（动物 → 人、相关 → 因果、单项研究 → 共识）", "段落之间有合理过渡", "所有参考文献可解析，无撤稿文献（或已说明）"],
  intro: ["开头能迅速建立问题的重要性", "研究背景介绍充分但不冗长", "研究缺口与本文贡献明确（与同题综述的差异可见）", "语言流畅、符合学术规范", "看得出作者自己的判断，而不是文献的堆叠"],
};

function Checklist({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const cl = useLoad<Json>(`${P}/checklist`);
  const act = useAction();
  if (!cl.data) return null;
  const data = cl.data.data || {};
  const put = (d: Json) => act.run(async () => cl.setData(await api.put(`${P}/checklist`, { data: d })));
  return (
    <>
      <Card title={`SANRA 叙述性综述质量自评（${cl.data.sanra_total} / 12）`}>
        {cl.data.sanra_items.map((it: string, i: number) => (
          <div key={i} className="row" style={{ marginBottom: 4 }}>
            <span style={{ width: 240 }}>{i + 1}. {it}</span>
            {[0, 1, 2].map((v) => <button key={v} className={`small ${String(data.sanra?.[i]) === String(v) ? "primary" : ""}`} onClick={() => put({ ...data, sanra: { ...(data.sanra || {}), [i]: v } })}>{v}</button>)}
          </div>
        ))}
      </Card>
      {Object.entries(LISTS).map(([k, items]) => (
        <Card key={k} title={k === "body" ? "正文检查清单" : "引言与语言检查清单"}>
          {items.map((it, i) => (
            <div key={i} className="row"><input type="checkbox" style={{ width: "auto" }} checked={!!data[k]?.[i]} onChange={(e) => put({ ...data, [k]: { ...(data[k] || {}), [i]: e.target.checked } })} /> {it}</div>
          ))}
        </Card>
      ))}
      <ErrorBox error={act.error} />
    </>
  );
}

function Disclosure({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const usage = useLoad<Json>(`${P}/disclosure/usage`);
  const [venue, setVenue] = useState("目标期刊");
  const [draft, setDraft] = useState<string | null>(null);
  const act = useAction();
  const u = usage.data;
  return (
    <>
      <Card title="使用日志汇总（平台自动统计）">
        {u && (
          <table><tbody>
            <tr><td>AI 调用次数</td><td>{u.runs}</td></tr>
            <tr><td>模型</td><td>{u.models.join("；") || "—"}</td></tr>
            <tr><td>任务</td><td>{u.task_names.join("、") || "—"}</td></tr>
            <tr><td>筛选决定人工确认比例</td><td>{pct(u.screen_human_ratio)}</td></tr>
            <tr><td>AI 证据卡人工核对比例</td><td>{pct(u.card_verified_ratio)}（{u.cards_ai} 张）</td></tr>
            <tr><td>正文人工撰写字符占比</td><td>{pct(u.human_char_ratio)}</td></tr>
            <tr><td>接受的 AI 语言建议</td><td>{u.accepted_language} 条（{u.ai_accepted_chars} 字）</td></tr>
            <tr><td>断言核查人工确认比例</td><td>{pct(u.claims_human_resolved_ratio)}（{u.claims_checked} 条）</td></tr>
          </tbody></table>
        )}
      </Card>
      <Card title="F4 起草 AI 使用声明（P21；学生确认、导师终审 G5）">
        <div className="row"><input style={{ width: 300 }} value={venue} onChange={(e) => setVenue(e.target.value)} /><button className="primary" onClick={() => act.run(async () => setDraft((await api.post(`${P}/disclosure/draft`, { venue })).text))}>起草</button></div>
        <ErrorBox error={act.error} />
        {draft !== null && <textarea style={{ minHeight: 220, marginTop: 8 }} value={draft} onChange={(e) => setDraft(e.target.value)} />}
        <p className="muted small">只描述日志中出现的使用方式，不要夸大或遗漏。学位论文请遵守学校关于生成式 AI 与 AIGC 检测的规定；不要使用“改写器 / 降重工具”掩盖 AI 生成文本。</p>
      </Card>
    </>
  );
}

function Export({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const act = useAction();
  return (
    <Card title="F6 导出">
      <div className="row">
        <button className="primary" onClick={() => act.run(() => api.download(`${P}/export/manuscript.docx`, "manuscript.docx", "POST", new FormData()))}>Word 稿件（Pandoc）</button>
        <button onClick={() => api.download(`${P}/export/manuscript.md`, "manuscript.md")}>Markdown 稿件</button>
        <button onClick={() => api.download(`${P}/export/references.bib`, "references.bib")}>参考文献 BibTeX</button>
        <button onClick={() => api.download(`${P}/search/log.csv`, "prisma-s-search-log.csv")}>检索日志（PRISMA-S）</button>
        <button onClick={() => api.download(`${P}/export/check-report.csv`, "check-report.csv")}>核查报告</button>
        <button onClick={() => api.download(`${P}/export/package.zip`, `${pid}.zip`)}>完整项目包</button>
      </div>
      <ErrorBox error={act.error} />
      <p className="muted small">Word 通过 Pandoc 导出；服务器配置 Better BibTeX 的 zotero.lua 过滤器后，引用为可在 Zotero Word 插件中刷新的域代码，否则按 CSL 样式渲染为静态格式。项目包包含任务书、检索式、筛选决定、证据卡、各版本稿件与审计日志，不含受版权保护的全文。</p>
    </Card>
  );
}
