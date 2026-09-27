import { useEffect, useRef, useState } from "react";
import { api, pct, type Json } from "../api";
import { useProject } from "../App";
import { AiTask, Card, ErrorBox, Pill, useAction, useLoad } from "../components/ui";
import { spliceSpans, type Span } from "../spans";

function SpanEditor({ spans, onChange }: { spans: Span[]; onChange: (s: Span[]) => void }) {
  const text = spans.map((s) => s.text).join("");
  const lastType = useRef<string>("");
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <>
      <textarea
        ref={ref}
        className="editor"
        value={text}
        onChange={(e) => {
          const nt = e.target.value;
          let p = 0;
          while (p < text.length && p < nt.length && text[p] === nt[p]) p++;
          let s = 0;
          while (s < text.length - p && s < nt.length - p && text[text.length - 1 - s] === nt[nt.length - 1 - s]) s++;
          const origin = /insertFromPaste|insertFromDrop/.test(lastType.current) ? "pasted" : "human";
          onChange(spliceSpans(spans, p, text.length - s, nt.slice(p, nt.length - s), origin));
        }}
        onBeforeInput={(e) => { lastType.current = (e.nativeEvent as InputEvent).inputType || ""; }}
        onPaste={() => { lastType.current = "insertFromPaste"; }}
        placeholder="在这里撰写本段正文。用 [@citekey] 插入引用；AI 不会替你写正文。"
      />
      <div className="small" style={{ marginTop: 6, lineHeight: 1.8 }}>
        {spans.map((s, i) => <span key={i} className={`span-${s.origin}`} title={s.origin}>{s.text}</span>)}
      </div>
      <div className="small muted">来源标记：普通 = 人工输入　<span className="span-ai_accepted">绿色 = 接受的 AI 语言建议</span>　<span className="span-pasted">橙色 = 外部粘贴</span></div>
    </>
  );
}

export default function Writing() {
  const { project } = useProject();
  const pid = project.id;
  const P = `/api/projects/${pid}`;
  const outlines = useLoad<Json[]>(`${P}/outlines`);
  const nodes: Json[] = outlines.data?.[0]?.nodes || [];
  const [node, setNode] = useState("");
  const paras = useLoad<Json[]>(node ? `${P}/paragraphs?node=${encodeURIComponent(node)}` : null, [node]);
  const [sel, setSel] = useState<string | null>(null);
  const prov = useLoad<Json>(`${P}/provenance`);
  const act = useAction();
  const [out, setOut] = useState<string | null>(null);
  const level = project.ai_level_max;

  return (
    <>
      <h1>阶段五 · 人机协同写作</h1>
      <p className="muted">逐段循环：① 段落证据包 → ② 论证骨架（L2，可选）→ ③ 学生撰写 → ④ AI 审阅（L2，只提问题不替写）→ ⑤ 修改（L3 语言建议逐条接受）→ ⑥ 完成并进入核查队列。</p>
      <ErrorBox error={act.error} />
      <Card title="章节" right={prov.data && <span className="small">全文人工撰写占比 <b>{pct(prov.data.human_ratio)}</b> · 接受 AI 建议 {prov.data.ai_accepted} 字 · 外部粘贴 {prov.data.pasted} 字</span>}>
        <div className="row">
          <select style={{ width: 360 }} value={node} onChange={(e) => { setNode(e.target.value); setSel(null); setOut(null); }}>
            <option value="">选择大纲节点…</option>
            {nodes.map((n) => <option key={n.id} value={n.id}>{"　".repeat(n.level - 1)}{n.id} {n.title}</option>)}
          </select>
          {node && <>
            <button onClick={() => act.run(async () => setOut((await api.post(`${P}/sections/${encodeURIComponent(node)}/plan`)).text))}>P14 章节证据分析与段落规划</button>
            <button disabled={level < "L2"} onClick={() => act.run(async () => setOut((await api.post(`${P}/sections/${encodeURIComponent(node)}/evaluate`)).text))}>P18 六维评价</button>
            <button className="primary" onClick={() => act.run(async () => { const p = await api.post(`${P}/paragraphs`, { outline_node: node }); await paras.reload(); setSel(p.id); })}>+ 新段落</button>
          </>}
        </div>
        {!nodes.length && <p className="muted">请先在“证据与大纲”页写出大纲。</p>}
        {out && <div className="ai" style={{ marginTop: 8 }}><pre className="md">{out}</pre></div>}
      </Card>

      {(paras.data || []).map((p) => (
        <div key={p.id}>
          {sel === p.id ? (
            <ParagraphWork pid={pid} para={p} level={level} onSaved={() => { paras.reload(); prov.reload(); }} onClose={() => setSel(null)} />
          ) : (
            <div className="card" onClick={() => setSel(p.id)} style={{ cursor: "pointer" }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small muted">{p.id} · v{p.revision} · 人工 {pct(p.provenance.human_ratio)}</span>
                <Pill kind={p.status === "done" ? "ok" : undefined}>{p.status === "done" ? "完成（核查队列）" : "撰写中"}</Pill>
              </div>
              <div>{p.text || <span className="muted">（空段落）</span>}</div>
            </div>
          )}
        </div>
      ))}

      <Card title="引言、结论与配图">
        <AiTask pid={pid} task="P17" label="范文结构分析" hint="在参数中粘贴目标期刊优秀综述的引言或结论" />
        <AiTask pid={pid} task="P20" label="配图规划" />
      </Card>
    </>
  );
}

function ParagraphWork({ pid, para, level, onSaved, onClose }: { pid: string; para: Json; level: string; onSaved: () => void; onClose: () => void }) {
  const P = `/api/projects/${pid}`;
  const [spans, setSpans] = useState<Span[]>(para.spans || []);
  const [thesis, setThesis] = useState(para.thesis || "");
  const [warnings, setWarnings] = useState<string[]>([]);
  const ev = useLoad<Json>(`${P}/paragraphs/${para.id}/evidence`);
  const ann = useLoad<Json[]>(`${P}/paragraphs/${para.id}/annotations`);
  const hist = useLoad<Json[]>(`${P}/paragraphs/${para.id}/history`);
  const act = useAction();
  useEffect(() => { setSpans(para.spans || []); }, [para.id, para.revision]);
  const dirty = spans.map((s) => s.text).join("") !== para.text || thesis !== para.thesis;
  const save = async () => { const r = await api.put(`${P}/paragraphs/${para.id}`, { spans, thesis }); setWarnings(r.warnings || []); onSaved(); hist.reload(); };

  return (
    <div className="grid2" style={{ gridTemplateColumns: "3fr 2fr" }}>
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <b>{para.id}</b>
          <div className="row">
            <button className="primary" disabled={!dirty || act.busy} onClick={() => act.run(save)}>保存</button>
            <button disabled={act.busy} onClick={() => act.run(async () => { if (dirty) await save(); await api.post(`${P}/paragraphs/${para.id}/done`); onSaved(); })}>标记完成</button>
            <button onClick={onClose}>收起</button>
          </div>
        </div>
        <label>本段核心论点（一句话）</label>
        <input value={thesis} onChange={(e) => setThesis(e.target.value)} />
        <label>正文</label>
        <SpanEditor spans={spans} onChange={setSpans} />
        {warnings.map((w, i) => <div key={i} className="note">{w}</div>)}
        <ErrorBox error={act.error} />
        <div className="row" style={{ marginTop: 10 }}>
          <button disabled={level < "L2"} onClick={() => act.run(async () => { await api.post(`${P}/paragraphs/${para.id}/skeleton`); ann.reload(); })}>② 论证骨架（L2）</button>
          <button disabled={level < "L2" || dirty} onClick={() => act.run(async () => { await api.post(`${P}/paragraphs/${para.id}/review`); ann.reload(); })}>④ 草稿审阅（L2）</button>
          <button disabled={level < "L3" || dirty} onClick={() => act.run(async () => { await api.post(`${P}/paragraphs/${para.id}/language`); ann.reload(); })}>⑤ 语言建议（L3）</button>
          <button className="danger small" onClick={() => act.run(async () => { if (confirm("删除该段落？")) { await api.del(`${P}/paragraphs/${para.id}`); onSaved(); onClose(); } })}>删除</button>
        </div>
        <h3>审阅意见与建议</h3>
        {(ann.data || []).filter((a) => a.status === "open").map((a) => (
          <div key={a.id} className={a.kind === "mentor" || a.kind === "note" ? "card" : "ai"} style={{ marginBottom: 6 }}>
            {a.kind === "skeleton" && (
              <>
                <b>论证骨架</b>（只给要点，不成句）
                <ol>{(a.body.points || []).map((p: Json, i: number) => <li key={i}>{p.text} <span className="muted small">{(p.card_ids || []).join(", ")}</span></li>)}</ol>
                <div className="small">推理顺序：{a.body.order}；值得加入作者判断之处：{a.body.author_judgment}</div>
              </>
            )}
            {a.kind === "ai_review" && <><Pill>{a.body.category}</Pill> 第 {a.sentence ?? "?"} 句：{a.body.issue}<div className="small">→ {a.body.direction}</div></>}
            {a.kind === "language" && <>第 {a.sentence} 句：<del>{a.body.original}</del> → <ins>{a.body.revised}</ins> <span className="small muted">{a.body.reason}</span></>}
            {(a.kind === "mentor" || a.kind === "note") && <><b>{a.kind === "mentor" ? "导师批注" : "备注"}</b>：{a.body.text}</>}
            <div className="row" style={{ marginTop: 4 }}>
              {a.kind === "language" && <button className="small primary" onClick={() => act.run(async () => { await api.post(`${P}/annotations/${a.id}`, { action: "accept" }); ann.reload(); onSaved(); })}>接受</button>}
              <button className="small" onClick={() => act.run(async () => { await api.post(`${P}/annotations/${a.id}`, { action: a.kind === "language" ? "reject" : "resolve" }); ann.reload(); })}>{a.kind === "language" ? "拒绝" : "已处理"}</button>
            </div>
          </div>
        ))}
        <div className="row"><input id={`c-${para.id}`} placeholder="添加批注（导师 / 自己）" /><button className="small" onClick={() => act.run(async () => { const el = document.getElementById(`c-${para.id}`) as HTMLInputElement; await api.post(`${P}/paragraphs/${para.id}/comments`, { text: el.value }); el.value = ""; ann.reload(); })}>添加</button></div>
        <details style={{ marginTop: 10 }}>
          <summary className="small">版本历史（{hist.data?.length ?? 0}）</summary>
          {(hist.data || []).map((h) => <div key={h.id} className="small"><b>v{h.revision}</b> <span className="muted">{h.created_at?.slice(0, 19)}</span> {h.text}</div>)}
        </details>
      </div>
      <div className="card">
        <h3 style={{ marginTop: 0 }}>① 段落证据包（已核对证据卡）</h3>
        {(ev.data?.cards || []).map((c: Json) => (
          <div key={c.card_id} style={{ marginBottom: 10 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <b className="small">{c.card_id} · {c.evidence_type}</b>
              <button className="small" onClick={() => setSpans(spliceSpans(spans, spans.map((s) => s.text).join("").length, spans.map((s) => s.text).join("").length, ` [@${c.citekey}]`, "human"))}>插入引用</button>
            </div>
            <div className="small">{c.claim}</div>
            <div className="quote">“{c.quote}”（{c.location}）</div>
            <div className="small muted">{c.model_or_population} {c.quantitative}</div>
          </div>
        ))}
        {!ev.data?.cards?.length && <div className="muted small">该节点暂无已核对证据卡。</div>}
        <p className="muted small">“插入引用”只插入 [@citekey]，不插入文字。</p>
      </div>
    </div>
  );
}
