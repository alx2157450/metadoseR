import { useState } from "react";
import { api, pct, type Json } from "../api";
import { useProject } from "../App";
import { Card, DECISION_ZH, ErrorBox, JobButton, LevelBadge, Pill, SOURCE_ZH, Stat, useAction, useLoad } from "../components/ui";

const TABS = ["导入与去重", "筛选校准", "辅助筛选", "全文与同步", "PRISMA"];

export default function Screening() {
  const { project } = useProject();
  const [tab, setTab] = useState(0);
  return (
    <>
      <h1>阶段三 · 文献获取、筛选与管理</h1>
      <p className="muted">先校准、后辅助、人决定：学生独立筛选校准样本 → 与 AI 预判比对（敏感度、特异度、κ）→ 达标后进入辅助模式 → 纳入与不确定逐条确认、排除至少抽查 20%。</p>
      <div className="tabs">{TABS.map((t, i) => <button key={t} className={tab === i ? "on" : ""} onClick={() => setTab(i)}>{t}</button>)}</div>
      {tab === 0 && <ImportTab pid={project.id} />}
      {tab === 1 && <CalibrationTab pid={project.id} />}
      {tab === 2 && <AssistTab pid={project.id} systematic={["systematic", "meta"].includes(project.review_type)} />}
      {tab === 3 && <FulltextTab pid={project.id} />}
      {tab === 4 && <PrismaTab pid={project.id} />}
    </>
  );
}

function ImportTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const [meta, setMeta] = useState({ database: "", query: "", hit_count: "" });
  const [res, setRes] = useState<Json>(null);
  const suspects = useLoad<Json>(`${P}/records?status=suspect&limit=100`);
  const act = useAction();
  return (
    <>
      <Card title="C1 题录导入（.nbib / RIS / BibTeX；知网、万方导出为 RIS / EndNote）">
        <div className="grid3">
          <div><label>来源数据库（留空视为补充来源）</label><input value={meta.database} onChange={(e) => setMeta({ ...meta, database: e.target.value })} placeholder="PubMed / Embase / CNKI" /></div>
          <div><label>检索式</label><input value={meta.query} onChange={(e) => setMeta({ ...meta, query: e.target.value })} /></div>
          <div><label>命中数</label><input value={meta.hit_count} onChange={(e) => setMeta({ ...meta, hit_count: e.target.value })} /></div>
        </div>
        <input type="file" style={{ marginTop: 10 }} accept=".nbib,.ris,.bib,.txt,.enw,.medline" onChange={(e) => {
          const f = e.target.files?.[0];
          if (!f) return;
          const fd = new FormData();
          fd.append("file", f);
          fd.append("database", meta.database);
          fd.append("query", meta.query);
          fd.append("hit_count", meta.hit_count || "0");
          act.run(async () => { setRes(await api.upload(`${P}/records/import`, fd)); suspects.reload(); });
        }} />
        <ErrorBox error={act.error} />
        {res && <div className="note">解析 {res.parsed} 条：新增 {res.added}，精确重复合并 {res.merged}（PMID / DOI），可疑重复 {res.suspect_duplicates}（标题模糊匹配，需人工确认）</div>}
      </Card>
      <Card title={`C2 可疑重复（${suspects.data?.total ?? 0}）`}>
        <table><tbody>
          {(suspects.data?.items || []).map((r: Json) => (
            <tr key={r.id}>
              <td>{r.title}<div className="small muted">{r.citekey} · 疑似重复 #{r.extra?.suspect_duplicate_of}</div></td>
              <td style={{ width: 200 }}>
                <button className="small" onClick={() => act.run(async () => { await api.post(`${P}/records/${r.id}/duplicate`, { is_duplicate: true }); suspects.reload(); })}>是重复</button>{" "}
                <button className="small" onClick={() => act.run(async () => { await api.post(`${P}/records/${r.id}/duplicate`, { is_duplicate: false }); suspects.reload(); })}>不是</button>
              </td>
            </tr>
          ))}
        </tbody></table>
      </Card>
      <Card title="C3 元数据补全与撤稿标记（Crossref / PubMed）">
        <JobButton label="补全元数据并检查撤稿" start={() => api.post(`${P}/records/enrich`)} />
      </Card>
    </>
  );
}

function CalibrationTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const list = useLoad<Json[]>(`${P}/screening/calibration`);
  const metrics = useLoad<Json>(`${P}/screening/calibration/metrics`);
  const act = useAction();
  const unlabeled = (list.data || []).filter((r) => !r.calibration_label).length;
  const reload = () => { list.reload(); metrics.reload(); };
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title="C5 校准轮" right={
        <>
          <button onClick={() => act.run(async () => { await api.post(`${P}/screening/calibration/sample`); reload(); })}>抽取校准样本（100 条或 10%）</button>
          <button className="primary" disabled={unlabeled > 0 || !list.data?.length} onClick={() => act.run(async () => { await api.post(`${P}/screening/calibration/run-ai`); reload(); })}>运行 AI 预判（P10）</button>
          <button onClick={() => act.run(async () => { await api.post(`${P}/screening/calibration/reset`); reload(); })}>修订标准后重新校准</button>
        </>
      }>
        <p className="muted small">盲评：你完成标注之前，不显示该条的 AI 预判。还剩 {unlabeled} 条未标注。</p>
        {metrics.data && metrics.data.n > 0 && (
          <div className="grid3" style={{ marginBottom: 10 }}>
            <div>敏感度 <b>{pct(metrics.data.sensitivity)}</b>（目标 ≥ {pct(metrics.data.target_sensitivity)}）</div>
            <div>特异度 <b>{pct(metrics.data.specificity)}</b></div>
            <div>κ <b>{metrics.data.kappa?.toFixed(2) ?? "—"}</b> · 分歧 {metrics.data.disagreements.length} 条</div>
          </div>
        )}
        {metrics.data && metrics.data.n > 0 && (
          <div className="row">
            {metrics.data.passed ? <Pill kind="ok">已进入辅助模式</Pill> : (
              <button className="primary" onClick={() => act.run(async () => { await api.post(`${P}/screening/calibration/accept`); reload(); })}>
                {metrics.data.sensitivity >= metrics.data.target_sensitivity ? "达标，进入辅助模式" : "未达标（需导师确认放行）"}
              </button>
            )}
          </div>
        )}
        {metrics.data?.disagreements?.length > 0 && (
          <>
            <h3>分歧逐条讨论</h3>
            <table><thead><tr><th>题录</th><th>人工</th><th>AI</th><th>AI 理由与依据句</th></tr></thead><tbody>
              {metrics.data.disagreements.map((d: Json) => <tr key={d.id}><td>{d.title}</td><td>{DECISION_ZH[d.human]}</td><td>{DECISION_ZH[d.ai]} <LevelBadge level={d.ai_level} /></td><td className="small">{d.ai_reason}<div className="quote">{d.ai_quote}</div></td></tr>)}
            </tbody></table>
          </>
        )}
      </Card>
      <table>
        <thead><tr><th>题录</th><th style={{ width: 160 }}>你的独立判断</th><th>AI 预判</th></tr></thead>
        <tbody>
          {(list.data || []).map((r) => (
            <tr key={r.id}>
              <td><b>{r.title}</b><div className="small muted">{r.journal} {r.year} · PMID {r.pmid || "—"}</div><details><summary className="small">摘要</summary><div className="small">{r.abstract || "（无摘要）"}</div></details></td>
              <td>
                {["include", "exclude"].map((l) => (
                  <button key={l} className={`small ${r.calibration_label === l ? "primary" : ""}`} onClick={() => act.run(async () => { await api.put(`${P}/records/${r.id}/calibration`, { label: l }); list.reload(); })}>{DECISION_ZH[l]}</button>
                ))}
              </td>
              <td>{r.calibration_label ? (r.ai_decision ? <>{DECISION_ZH[r.ai_decision]} <LevelBadge level={r.ai_level} /></> : "未运行") : <span className="muted small">标注后显示</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function AssistTab({ pid, systematic }: { pid: string; systematic: boolean }) {
  const P = `/api/projects/${pid}`;
  const [status, setStatus] = useState("ai_include");
  const [offset, setOffset] = useState(0);
  const recs = useLoad<Json>(`${P}/records?status=${status}&sort=rank&limit=30&offset=${offset}`, [status, offset]);
  const act = useAction();
  const decide = (id: number, decision: string) => act.run(async () => {
    if (systematic) await api.post(`${P}/records/${id}/vote`, { decision });
    else await api.post(`${P}/records/${id}/decision`, { decision });
    recs.reload();
  });
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title="C6 / C7 辅助筛选与分级" right={
        <>
          <JobButton label="AI 预筛全部未判题录" start={() => api.post(`${P}/screening/prescreen`)} onDone={() => recs.reload()} />
          <button onClick={() => act.run(async () => { await api.post(`${P}/screening/rerank`); recs.reload(); })}>主动学习重排序</button>
          <button onClick={() => act.run(async () => { const r = await api.post(`${P}/screening/audit-sample`); alert(`抽查 ${r.sampled} 条 AI 预判排除的题录`); setStatus("audit"); })}>抽查排除（≥20%）</button>
          {!systematic && <button onClick={() => act.run(async () => { const r = await api.post(`${P}/screening/finalize-excludes`); alert(`按“AI 预判未复核”排除 ${r.excluded} 条`); recs.reload(); })}>其余 AI 排除项定稿</button>}
          <button onClick={() => api.download(`${P}/screening/export-batches`, "P10-batches.txt")}>导出分批文本</button>
        </>
      }>
        {systematic && <div className="note">系统综述：必须双人独立筛选，AI 只能作为第三意见。两票一致自动定稿，不一致交导师仲裁。</div>}
        <div className="row">
          {[["ai_include", "AI 建议纳入 / 不确定（逐条确认）"], ["audit", "排除抽查"], ["undecided", "全部未决"], ["include", "已纳入"], ["exclude", "已排除"], ["uncertain", "不确定"]].map(([k, l]) => (
            <button key={k} className={`small ${status === k ? "primary" : ""}`} onClick={() => { setStatus(k); setOffset(0); }}>{l}</button>
          ))}
          <span className="muted small">共 {recs.data?.total ?? 0} 条</span>
        </div>
      </Card>
      <table>
        <thead><tr><th>题录（按相关概率排序）</th><th>AI 预判</th><th style={{ width: 210 }}>你的决定</th></tr></thead>
        <tbody>
          {(recs.data?.items || []).map((r: Json) => (
            <tr key={r.id}>
              <td>
                <b>{r.title}</b> {r.is_seed && <Pill kind="info">种子</Pill>} {r.retraction_status === "retracted" && <Pill kind="bad">已撤稿</Pill>}
                <div className="small muted">{r.citekey} · {r.journal} {r.year} · {(r.pub_types || []).join(", ")}</div>
                <details><summary className="small">摘要</summary><div className="small">{r.abstract || "（无摘要）"}</div></details>
              </td>
              <td className="small" style={{ maxWidth: 320 }}>
                {r.ai_decision ? (
                  <div className="ai">
                    <LevelBadge level={r.ai_level} /> {DECISION_ZH[r.ai_decision]}
                    <div>{r.ai_reason}</div>
                    {r.ai_quote && <div className="quote">{r.ai_quote}</div>}
                  </div>
                ) : "—"}
              </td>
              <td>
                {["include", "uncertain", "exclude"].map((d) => (
                  <button key={d} className={`small ${r.decision === d ? "primary" : ""}`} onClick={() => decide(r.id, d)}>{DECISION_ZH[d]}</button>
                ))}
                {r.decision_source && <div className="small muted">{SOURCE_ZH[r.decision_source]}</div>}
                <select className="small" style={{ marginTop: 4 }} value={r.relevance_level || ""} onChange={(e) => act.run(async () => { await api.patch(`${P}/records/${r.id}`, { relevance_level: e.target.value }); recs.reload(); })}>
                  <option value="">分级</option>{["A", "B", "C", "D", "U"].map((l) => <option key={l}>{l}</option>)}
                </select>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button>
        <button disabled={(recs.data?.total ?? 0) <= offset + 30} onClick={() => setOffset(offset + 30)}>下一页</button>
      </div>
    </>
  );
}

function FulltextTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const recs = useLoad<Json>(`${P}/records?status=include&limit=500`);
  const [z, setZ] = useState({ library_type: "users", library_id: "", api_key: "" });
  const act = useAction();
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title="C8 全文获取与解析" right={<JobButton label="获取开放获取全文（PMC / Europe PMC）" start={() => api.post(`${P}/records/fulltext/fetch`)} onDone={() => recs.reload()} />}>
        <p className="muted small">A 级必须获取全文，B 级尽量获取，C 级可只用摘要。订阅全文请通过学校图书馆权限下载后上传（PDF 需服务器启用 GROBID；也可上传 JATS XML / 纯文本）。受版权保护的全文仅供上传者本人使用。</p>
        <table>
          <thead><tr><th>文献</th><th>等级</th><th>全文</th><th>上传</th><th>引文追溯（B8）</th></tr></thead>
          <tbody>
            {(recs.data?.items || []).map((r: Json) => (
              <tr key={r.id}>
                <td>{r.title}<div className="small muted">{r.citekey} · {r.pmcid || "无 PMCID"}</div></td>
                <td><LevelBadge level={r.relevance_level} /></td>
                <td>{r.fulltext_status === "parsed" ? <Pill kind="ok">已解析</Pill> : <Pill kind={r.relevance_level === "A" ? "bad" : undefined}>无</Pill>}</td>
                <td><input type="file" accept=".pdf,.xml,.nxml,.txt,.md" onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  const fd = new FormData();
                  fd.append("file", f);
                  act.run(async () => { await api.upload(`${P}/records/${r.id}/fulltext`, fd); recs.reload(); });
                }} /></td>
                <td>{r.doi && r.relevance_level === "A" && (
                  <span className="row">
                    <button className="small" onClick={() => act.run(async () => { const x = await api.post(`${P}/search/citations`, { record_id: r.id, direction: "backward" }); alert(`导入 ${x.added} 条`); })}>后向</button>
                    <button className="small" onClick={() => act.run(async () => { const x = await api.post(`${P}/search/citations`, { record_id: r.id, direction: "forward" }); alert(`导入 ${x.added} 条`); })}>前向</button>
                  </span>
                )}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <Card title="C4 Zotero 双向同步（在原条目写入标签与子笔记，不重新导入）">
        <div className="grid3">
          <div><label>库类型</label><select value={z.library_type} onChange={(e) => setZ({ ...z, library_type: e.target.value })}><option value="users">个人库</option><option value="groups">群组库</option></select></div>
          <div><label>Library ID</label><input value={z.library_id} onChange={(e) => setZ({ ...z, library_id: e.target.value })} /></div>
          <div><label>API Key（不保存）</label><input type="password" value={z.api_key} onChange={(e) => setZ({ ...z, api_key: e.target.value })} /></div>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" disabled={!z.library_id || !z.api_key} onClick={() => act.run(async () => { const r = await api.post(`${P}/zotero/sync`, z); alert(`已同步 ${r.synced.length} 条；Zotero 中未找到 ${r.not_found_in_zotero.length} 条`); })}>同步纳入文献</button>
          <button onClick={() => api.download(`${P}/records.bib`, "references.bib")}>导出 BibTeX</button>
        </div>
        <p className="muted small">以 Better BibTeX citekey / DOI / PMID 为主键匹配。期刊指标请导入机构已授权的指标表，只作参考，不作为纳入依据。</p>
      </Card>
    </>
  );
}

function PrismaTab({ pid }: { pid: string }) {
  const { data } = useLoad<Json>(`/api/projects/${pid}/prisma`);
  if (!data) return null;
  return (
    <Card title="C9 PRISMA 2020 流程图数据">
      <div className="grid3">
        <Stat label="数据库检索识别" value={data.identified_database_total} />
        <Stat label="其他来源识别" value={data.identified_other} />
        <Stat label="去除重复" value={data.duplicates_removed} />
        <Stat label="题录筛选" value={data.records_screened} />
        <Stat label="排除" value={data.excluded} />
        <Stat label="纳入" value={data.included} />
      </div>
      <h3>排除来源</h3>
      <div>{Object.entries(data.excluded_by_source).map(([k, v]) => <span key={k} style={{ marginRight: 12 }}>{SOURCE_ZH[k] || k}：<b>{String(v)}</b></span>)}</div>
      <h3>纳入来源</h3>
      <div>{Object.entries(data.included_by_source).map(([k, v]) => <span key={k} style={{ marginRight: 12 }}>{SOURCE_ZH[k] || k}：<b>{String(v)}</b></span>)}</div>
      <p className="muted small">各数据库：{Object.entries(data.identified_databases).map(([k, v]) => `${k} ${v}`).join("；")} · 待决 {data.awaiting_decision} · 已解析全文 {data.fulltext_parsed}{data.retracted_included ? ` · 纳入中含撤稿 ${data.retracted_included}` : ""}</p>
    </Card>
  );
}
