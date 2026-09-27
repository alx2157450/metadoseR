import { useEffect, useState } from "react";
import { api, type Json } from "../api";
import { useProject } from "../App";
import { AiTask, Card, ErrorBox, JobButton, LevelBadge, Pill, useAction, useLoad } from "../components/ui";

const TABS = ["证据卡核对", "大纲（学生先写）", "证据地图", "章节文献包"];
const TYPES: Record<string, string> = { human: "人群研究", clinical_trial: "临床试验", animal: "动物实验", cell: "细胞实验", computational: "计算研究", review: "综述观点", "": "—" };

export default function Evidence() {
  const { project } = useProject();
  const [tab, setTab] = useState(0);
  return (
    <>
      <h1>阶段四 · 证据提取与大纲构建</h1>
      <p className="muted">A/B 级文献全文拆成“证据卡”，每张卡只陈述一个发现并带原文定位；只有“已核对”的卡才能进入写作证据包。学生先独立起草大纲，再查看 AI 证据地图（先想后看）。</p>
      <div className="tabs">{TABS.map((t, i) => <button key={t} className={tab === i ? "on" : ""} onClick={() => setTab(i)}>{t}</button>)}</div>
      {tab === 0 && <CardsTab pid={project.id} />}
      {tab === 1 && <OutlineTab pid={project.id} />}
      {tab === 2 && <MapTab pid={project.id} />}
      {tab === 3 && <PackTab pid={project.id} />}
    </>
  );
}

function CardsTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const recs = useLoad<Json>(`${P}/records?status=include&limit=500`);
  const [rid, setRid] = useState<number | null>(null);
  const cards = useLoad<Json[]>(rid ? `${P}/cards?record_id=${rid}` : null, [rid]);
  const [ft, setFt] = useState<Json>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const act = useAction();
  useEffect(() => {
    setFt(null);
    if (rid) api.get(`${P}/records/${rid}/fulltext`).then(setFt).catch(() => setFt(null));
  }, [rid]);
  const withText = (recs.data?.items || []).filter((r: Json) => r.fulltext_status === "parsed");
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title="D1 证据卡抽取（P11）" right={<JobButton label="为 A/B 级全文批量抽取" start={() => api.post(`${P}/cards/extract`, {})} onDone={() => cards.reload()} />}>
        <select value={rid ?? ""} onChange={(e) => setRid(Number(e.target.value) || null)}>
          <option value="">选择一篇已解析全文的文献…</option>
          {withText.map((r: Json) => <option key={r.id} value={r.id}>[{r.relevance_level || "?"}] {r.citekey} — {r.title.slice(0, 90)}</option>)}
        </select>
        {rid && <div className="row" style={{ marginTop: 8 }}><JobButton label="抽取本篇" start={() => api.post(`${P}/cards/extract`, { record_ids: [rid] })} onDone={() => cards.reload()} /></div>}
      </Card>
      {rid && (
        <div className="grid2">
          <Card title="原文（按段落编号）">
            {!ft && <div className="muted">无全文或无权查看</div>}
            <div style={{ maxHeight: "70vh", overflow: "auto" }}>
              {ft?.paragraphs.map((p: Json) => (
                <div key={p.id} id={`para-${p.id}`} style={{ padding: 6, borderRadius: 6, background: focus === p.id ? "var(--warn-soft)" : undefined }}>
                  <span className="pill">{p.id}</span> <span className="muted small">{p.section}</span>
                  <div className="small">{p.text}</div>
                </div>
              ))}
            </div>
          </Card>
          <div>
            {(cards.data || []).map((c) => <CardEditor key={c.card_id} pid={pid} card={c} onFocus={() => { setFocus(c.location); document.getElementById(`para-${c.location}`)?.scrollIntoView({ block: "center" }); }} onChange={cards.reload} />)}
            <button onClick={() => act.run(async () => { await api.post(`${P}/cards`, { record_id: rid, claim: "（新证据卡）", quote: "", location: "" }); cards.reload(); })}>+ 手工添加证据卡</button>
          </div>
        </div>
      )}
    </>
  );
}

function CardEditor({ pid, card, onFocus, onChange }: { pid: string; card: Json; onFocus: () => void; onChange: () => void }) {
  const [c, setC] = useState(card);
  const act = useAction();
  const save = (verify: string) => act.run(async () => { await api.patch(`/api/projects/${pid}/cards/${card.card_id}`, { ...c, verify_status: verify }); onChange(); });
  const f = (k: string, rows = 1) => (
    <div><label>{k}</label>{rows > 1 ? <textarea rows={rows} value={c[k] || ""} onChange={(e) => setC({ ...c, [k]: e.target.value })} /> : <input value={c[k] || ""} onChange={(e) => setC({ ...c, [k]: e.target.value })} />}</div>
  );
  return (
    <div className={`card ${card.verify_status === "unverified" ? "ai" : ""}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span><b>{card.card_id}</b> <Pill kind={card.verify_status === "unverified" ? "warn" : "ok"}>{{ unverified: "未核对", verified: "已核对", modified: "已修改" }[card.verify_status as string]}</Pill>{" "}
          {card.quote_found === false && <Pill kind="bad">引句未在原文中找到</Pill>}</span>
        <button className="small" onClick={onFocus}>定位原文 {card.location}</button>
      </div>
      {f("claim", 2)}
      <div className="grid2">
        <div><label>evidence_type</label><select value={c.evidence_type || ""} onChange={(e) => setC({ ...c, evidence_type: e.target.value })}>{Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></div>
        {f("model_or_population")}
        {f("quantitative")}
        {f("location")}
      </div>
      {f("quote", 2)}
      {f("limitations")}
      <label>outline_nodes（逗号分隔，如 2.1, 2.2）</label>
      <input value={(c.outline_nodes || []).join(", ")} onChange={(e) => setC({ ...c, outline_nodes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })} />
      <ErrorBox error={act.error} />
      <div className="row" style={{ marginTop: 8 }}>
        <button className="primary small" onClick={() => save("verified")}>核对无误 / 保存修改</button>
        <button className="small danger" onClick={() => act.run(async () => { await api.del(`/api/projects/${pid}/cards/${card.card_id}`); onChange(); })}>删除</button>
      </div>
    </div>
  );
}

function OutlineTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const outlines = useLoad<Json[]>(`${P}/outlines`);
  const diff = useLoad<Json>(`${P}/outlines/diff`);
  const latest = outlines.data?.[0];
  const [nodes, setNodes] = useState<Json[]>([]);
  const act = useAction();
  useEffect(() => { if (latest) setNodes(latest.nodes); else setNodes([{ id: "1", title: "Introduction", thesis: "", level: 1 }]); }, [latest?.id]);
  const set = (i: number, k: string, v: Json) => setNodes(nodes.map((n, j) => (j === i ? { ...n, [k]: v } : n)));
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title={`D3 大纲编辑器 ${latest ? `（当前 v${latest.version}，${latest.kind === "student_draft" ? "学生初版" : "修订版"}，${latest.gate_status}）` : "（尚无大纲：请先独立写出初版）"}`} right={
        <>
          <button className="primary" onClick={() => act.run(async () => { await api.post(`${P}/outlines`, { nodes }); outlines.reload(); diff.reload(); })}>{latest ? "保存为新版本" : "保存学生初版"}</button>
          {latest && <button onClick={() => act.run(async () => { await api.post(`${P}/gates/G3/submit`, { object_version: `outline@v${latest.version}` }); outlines.reload(); })}>提交导师审批（G3）</button>}
        </>
      }>
        <p className="muted small">一级标题 3–5 个，对应任务书核心问题；每个二级标题写一句“本节要证明什么”，而不是只写名词短语。H1 → H2 → H3 不跳级。</p>
        <table>
          <thead><tr><th style={{ width: 80 }}>编号</th><th style={{ width: 70 }}>层级</th><th>标题</th><th>核心论点（本节要证明什么）</th><th></th></tr></thead>
          <tbody>
            {nodes.map((n, i) => (
              <tr key={i}>
                <td><input value={n.id} onChange={(e) => set(i, "id", e.target.value)} /></td>
                <td><select value={n.level} onChange={(e) => set(i, "level", Number(e.target.value))}><option value={1}>H1</option><option value={2}>H2</option><option value={3}>H3</option></select></td>
                <td><input style={{ paddingLeft: 8 + (n.level - 1) * 16 }} value={n.title} onChange={(e) => set(i, "title", e.target.value)} /></td>
                <td><input value={n.thesis} onChange={(e) => set(i, "thesis", e.target.value)} /></td>
                <td><button className="small danger" onClick={() => setNodes(nodes.filter((_, j) => j !== i))}>×</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="small" style={{ marginTop: 6 }} onClick={() => setNodes([...nodes, { id: "", title: "", thesis: "", level: 2 }])}>+ 节点</button>
      </Card>
      {diff.data?.from && (
        <Card title={`学生初版 v${diff.data.from} → 当前 v${diff.data.to} 的差异（体现学生独立贡献）`}>
          <div className="small">新增：{diff.data.added.map((n: Json) => `${n.id} ${n.title}`).join("；") || "—"}</div>
          <div className="small">删除：{diff.data.removed.map((n: Json) => `${n.id} ${n.title}`).join("；") || "—"}</div>
          <div className="small">修改：{diff.data.changed.map((n: Json) => n.id).join("、") || "—"}</div>
        </Card>
      )}
    </>
  );
}

function MapTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const map = useLoad<Json>(`${P}/evidence-map`);
  const act = useAction();
  const [ai, setAi] = useState<string | null>(null);
  if (map.error) return <div className="note">{map.error}</div>;
  if (!map.data) return null;
  return (
    <>
      <ErrorBox error={act.error} />
      <Card title={`D4 证据地图（大纲 v${map.data.outline_version}；薄弱节点：已核对证据卡 < ${map.data.min_cards} 张或缺少 A 级文献）`} right={
        <button className="primary" onClick={() => act.run(async () => setAi((await api.post(`${P}/evidence-map/ai`)).text))}>P12 AI 对照与诊断</button>
      }>
        <table>
          <thead><tr><th>节点</th><th>已核对卡</th><th>A 级文献</th><th>未核对</th><th>证据类型</th><th></th></tr></thead>
          <tbody>
            {map.data.nodes.map((n: Json) => (
              <tr key={n.id}>
                <td style={{ paddingLeft: 8 + (n.level - 1) * 16 }}>{n.id} {n.title}<div className="small muted">{n.thesis}</div></td>
                <td>{n.count}</td><td>{n.a_count}</td><td>{n.unverified}</td>
                <td className="small">{Object.entries(n.types).map(([k, v]) => `${TYPES[k] || k} ${v}`).join("；")}</td>
                <td>{n.weak && <Pill kind="warn">证据不足</Pill>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {map.data.unassigned.length > 0 && <div className="note">{map.data.unassigned.length} 张证据卡未映射到任何节点：可能对应遗漏主题。</div>}
      </Card>
      {ai && <div className="ai"><pre className="md">{ai}</pre></div>}
      <Card title="D5 多视角提问（借鉴 STORM）"><AiTask pid={pid} task="P13" label="多视角提问" /></Card>
    </>
  );
}

function PackTab({ pid }: { pid: string }) {
  const P = `/api/projects/${pid}`;
  const outlines = useLoad<Json[]>(`${P}/outlines`);
  const [node, setNode] = useState("");
  const pack = useLoad<Json>(node ? `${P}/section-pack/${encodeURIComponent(node)}` : null, [node]);
  return (
    <Card title="D6 章节文献包（替代原工具 3 PaperProcessor、工具 4 SearchTxt）" right={node && <button onClick={() => api.download(`${P}/section-pack/${encodeURIComponent(node)}/text`, `section-${node}.txt`)}>导出文本</button>}>
      <select value={node} onChange={(e) => setNode(e.target.value)}>
        <option value="">选择大纲节点…</option>
        {(outlines.data?.[0]?.nodes || []).map((n: Json) => <option key={n.id} value={n.id}>{n.id} {n.title}</option>)}
      </select>
      {pack.data && (
        <>
          <h3>已核对证据卡（{pack.data.cards.length}）</h3>
          {pack.data.cards.map((c: Json) => <div key={c.card_id} style={{ marginBottom: 8 }}><b>{c.card_id}</b> [@{c.citekey}] {c.claim}<div className="quote">“{c.quote}”（{c.location}）</div></div>)}
          <h3>题录</h3>
          {pack.data.records_direct.map((r: Json) => <div key={r.id} className="small"><LevelBadge level={r.relevance_level} /> [@{r.citekey}] {r.title} ({r.year})</div>)}
          {pack.data.records_indirect.map((r: Json) => <div key={r.id} className="small muted">间接：[@{r.citekey}] {r.title}</div>)}
        </>
      )}
    </Card>
  );
}
