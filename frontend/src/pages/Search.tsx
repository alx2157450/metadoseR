import { useState } from "react";
import { api, pct, type Json } from "../api";
import { useProject } from "../App";
import { AiTask, Card, ErrorBox, JobButton, Pill, useAction, useLoad } from "../components/ui";

export default function Search() {
  const { project } = useProject();
  const pid = project.id;
  const P = `/api/projects/${pid}/search`;
  const [query, setQuery] = useState("");
  const [validation, setValidation] = useState<Json>(null);
  const [gen, setGen] = useState<Json>(null);
  const [conv, setConv] = useState<Json>(null);
  const [runRes, setRunRes] = useState<Json>(null);
  const [sample, setSample] = useState<{ runId: number; items: Json[] } | null>(null);
  const [precision, setPrecision] = useState<Json>(null);
  const [diag, setDiag] = useState<string | null>(null);
  const [manual, setManual] = useState({ database: "Embase", platform: "Embase.com", query: "", hit_count: 0 });
  const queries = useLoad<Json[]>(`${P}/queries`);
  const runs = useLoad<Json[]>(`${P}/runs`);
  const act = useAction();

  const check = (q = query) => act.run(async () => setValidation(await api.post(`${P}/validate`, { query: q })));

  return (
    <>
      <h1>阶段二 · 检索策略构建与验证</h1>
      <p className="muted">PubMed 原生语法（[Mesh]、[tiab]、[ti]、"…"[tiab:~N]）；语法校验器自动拦截 Scopus / WoS / Embase 语法与 ? 通配符。检索式须通过种子召回、查准率抽查和规模评估后提交 G1。</p>
      <ErrorBox error={act.error} />

      <Card title="B1 / B2 检索式生成与语法校验" right={
        <button disabled={act.busy} onClick={() => act.run(async () => { const r = await api.post(`${P}/generate`, {}); setGen(r); if (r.data) { setQuery(r.data.query); setValidation(r.validation); } })}>P06 由任务书概念生成</button>
      }>
        <textarea className="editor" style={{ fontFamily: "ui-monospace, monospace", fontSize: 13 }} value={query} onChange={(e) => { setQuery(e.target.value); setValidation(null); }} placeholder='("Diabetic Cardiomyopathies"[Mesh] OR "diabetic cardiomyopath*"[tiab]) AND (mitophag*[tiab] OR PINK1[tiab])' />
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => check()}>语法校验</button>
          <button className="primary" disabled={!query || act.busy} onClick={() => act.run(async () => { await api.post(`${P}/queries`, { query, origin: gen?.data?.query === query ? "ai_accepted" : "human" }); queries.reload(); })}>保存为新版本</button>
          <button disabled={!query || act.busy} onClick={() => act.run(async () => setRunRes(await api.post(`${P}/run`, { query })))}>在 PubMed 执行</button>
        </div>
        {validation && (
          <div style={{ marginTop: 8 }}>
            {validation.ok ? <Pill kind="ok">语法通过</Pill> : <Pill kind="bad">存在错误</Pill>}
            <ul>{validation.issues.map((i: Json, k: number) => <li key={k}><Pill kind={i.level === "error" ? "bad" : "warn"}>{i.level}</Pill> {i.message} <code>{i.fragment}</code></li>)}</ul>
          </div>
        )}
        {gen?.data && (
          <div className="ai">
            <table>
              <thead><tr><th>概念</th><th>MeSH</th><th>自由词</th></tr></thead>
              <tbody>{gen.data.groups.map((g: Json, i: number) => <tr key={i}><td>{g.concept}</td><td>{(g.mesh || []).join("; ")}</td><td>{(g.free_terms || []).join("; ")}</td></tr>)}</tbody>
            </table>
            <div className="small">风险：{(gen.data.risks || []).join("；")}</div>
          </div>
        )}
      </Card>

      {runRes && (
        <Card title="执行结果（B4 召回检验 · 规模评估）" right={
          <JobButton label="导入全部题录到文献库" start={() => api.post(`${P}/runs/${runRes.id}/import`)} onDone={() => runs.reload()} />
        }>
          <div className="grid3">
            <div>总命中 <b>{runRes.size.total}</b>（本次取回 {runRes.retrieved_ids}）</div>
            <div>综述 <b>{runRes.size.reviews}</b></div>
            <div>近 5 年占比 <b>{pct(runRes.size.recent_ratio)}</b></div>
          </div>
          <div style={{ marginTop: 8 }}>
            种子召回：{runRes.recall.seeds ? <>{pct(runRes.recall.recall)}（目标 ≥ {pct(runRes.recall.target)}） {runRes.recall.passed ? <Pill kind="ok">通过</Pill> : <Pill kind="bad">未通过</Pill>}</> : <span className="muted">任务书未填写种子文献</span>}
            {runRes.recall.missing?.length > 0 && <div className="note">未召回：{runRes.recall.missing.join("、")} —— 逐篇分析原因：缺同义词？尚未标引 MeSH？字段过窄？</div>}
          </div>
          <div className="small muted">PubMed 实际解释：{runRes.translation}</div>
        </Card>
      )}

      <Card title="B3 多数据库转换（Embase / WoS / Scopus / 知网）">
        <div className="row">
          {["scopus", "wos", "embase", "cnki"].map((t) => (
            <button key={t} disabled={!query} onClick={() => act.run(async () => setConv(await api.post(`${P}/convert`, { query, target: t })))}>{t}</button>
          ))}
          {project.ai_level_max && <button disabled={!query} onClick={() => act.run(async () => setConv(await api.post(`${P}/convert`, { query, target: "cnki", use_ai: true })))}>P07 AI 转换（知网）</button>}
        </div>
        {conv && (
          <div className="ai" style={{ marginTop: 8 }}>
            <code style={{ whiteSpace: "pre-wrap" }}>{conv.query}</code>
            {conv.unmapped?.length > 0 && <div className="small">无法一一对应：{conv.unmapped.join("；")}</div>}
            <div className="small">{conv.notes}</div>
          </div>
        )}
        <h3>在其他数据库试检后记录（PRISMA-S）</h3>
        <div className="row">
          <input style={{ width: 120 }} value={manual.database} onChange={(e) => setManual({ ...manual, database: e.target.value })} />
          <input style={{ width: 140 }} value={manual.platform} onChange={(e) => setManual({ ...manual, platform: e.target.value })} />
          <input style={{ flex: 1 }} placeholder="实际使用的检索式" value={manual.query} onChange={(e) => setManual({ ...manual, query: e.target.value })} />
          <input style={{ width: 100 }} type="number" placeholder="命中数" value={manual.hit_count || ""} onChange={(e) => setManual({ ...manual, hit_count: Number(e.target.value) })} />
          <button onClick={() => act.run(async () => { await api.post(`${P}/runs/manual`, manual); runs.reload(); })}>记录</button>
        </div>
      </Card>

      <Card title="B7 检索日志（PRISMA-S）与 B5 查准率抽查" right={<button onClick={() => api.download(`${P}/log.csv`, "prisma-s-search-log.csv")}>导出 CSV</button>}>
        <table>
          <thead><tr><th>#</th><th>数据库</th><th>日期</th><th>检索式</th><th>命中</th><th>导入</th><th>召回</th><th></th></tr></thead>
          <tbody>
            {(runs.data || []).map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{r.database}<div className="small muted">{r.kind}</div></td><td className="small">{r.run_at?.slice(0, 10)}</td>
                <td className="small" style={{ maxWidth: 380, wordBreak: "break-all" }}>{r.query}</td><td>{r.hit_count}</td><td>{r.imported_count}</td>
                <td>{r.recall?.recall !== undefined && r.recall?.recall !== null ? pct(r.recall.recall) : "—"}</td>
                <td>{r.database === "pubmed" && <button className="small" onClick={() => act.run(async () => { setSample({ runId: r.id, items: await api.post(`${P}/runs/${r.id}/precision-sample?n=30`) }); setPrecision(null); })}>随机抽 30 条</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {sample && (
          <div style={{ marginTop: 10 }}>
            <p className="muted small">逐条标注相关（Y）/ 不相关（N）。不要只看最新 10 条，会高估相关度。</p>
            {sample.items.map((x, i) => (
              <div key={x.pmid} className="row" style={{ borderBottom: "1px solid var(--line)", padding: "4px 0" }}>
                <select style={{ width: 70 }} value={x.label || ""} onChange={(e) => setSample({ ...sample, items: sample.items.map((y, j) => (j === i ? { ...y, label: e.target.value } : y)) })}><option value="">?</option><option>Y</option><option>N</option></select>
                <span style={{ flex: 1 }}>{x.title} <span className="muted small">PMID {x.pmid}</span></span>
              </div>
            ))}
            <div className="row" style={{ marginTop: 8 }}>
              <button className="primary" onClick={() => act.run(async () => setPrecision(await api.put(`${P}/runs/${sample.runId}/precision-sample`, { labels: Object.fromEntries(sample.items.filter((x) => x.label).map((x) => [x.pmid, x.label])) })))}>计算查准率</button>
              {precision && <span>查准率 <b>{pct(precision.precision)}</b>（{precision.labeled} 条）· {precision.advice}</span>}
              {precision && <button onClick={() => act.run(async () => setDiag((await api.post(`${P}/runs/${sample.runId}/diagnose`)).text))}>P09 误检诊断</button>}
            </div>
            {diag && <div className="ai"><pre className="md">{diag}</pre></div>}
          </div>
        )}
      </Card>

      <Card title="检索式版本">
        <table>
          <thead><tr><th>版本</th><th>数据库</th><th>检索式</th><th>来源</th><th>校验</th><th></th></tr></thead>
          <tbody>
            {(queries.data || []).map((q) => (
              <tr key={q.id}>
                <td>v{q.version} {q.is_final && <Pill kind="ok">定稿</Pill>}</td><td>{q.database}</td>
                <td className="small" style={{ maxWidth: 480, wordBreak: "break-all" }}><a href="#" onClick={(e) => { e.preventDefault(); setQuery(q.query); check(q.query); }}>{q.query}</a></td>
                <td>{q.origin === "ai_accepted" ? "采纳 AI 候选" : "人工"}</td>
                <td>{q.validation?.ok ? <Pill kind="ok">通过</Pill> : <Pill kind="bad">错误</Pill>}</td>
                <td>{!q.is_final && <button className="small" onClick={() => act.run(async () => { await api.post(`${P}/queries/${q.id}/final`); queries.reload(); })}>定稿</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="进阶与补充检索">
        <AiTask pid={pid} task="P08" label="矩阵式检索计划" />
        <div className="row">
          <input style={{ flex: 1 }} id="pp" placeholder="预印本检索词（Europe PMC：bioRxiv / medRxiv）" />
          <button onClick={() => act.run(async () => { const q = (document.getElementById("pp") as HTMLInputElement).value; const r = await api.post(`${P}/preprints`, { query: q }); alert(`导入 ${r.added} 条，合并 ${r.merged} 条`); runs.reload(); })}>检索预印本</button>
        </div>
        <p className="muted small">引文追溯（B8）在“文献与筛选”页对 A 级文献执行；知网 / 万方请导出 RIS 或 EndNote 后导入。</p>
      </Card>
    </>
  );
}
