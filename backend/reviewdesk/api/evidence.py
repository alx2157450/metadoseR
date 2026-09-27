"""模块 D：证据与大纲（阶段四）。"""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from .. import audit, jobs
from ..gateway import gateway
from ..models import EvidenceCard, FullText, Outline, Record
from ..pipelines import latest_outline, outline_text
from ..security import ProjectCtx, get_project_ctx
from ..services.text import quote_in
from .common import row, rows

router = APIRouter(prefix="/api/projects/{project_id}")

MIN_CARDS = 8


# ------------------------------------------------------------------ D1 / D2 证据卡


class ExtractIn(BaseModel):
    record_ids: list[int] | None = None
    levels: list[str] = ["A", "B"]


@router.post("/cards/extract")
def extract(body: ExtractIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Record.id).where(Record.project_id == ctx.project.id, Record.decision == "include", Record.fulltext_status == "parsed")
    if body.record_ids:
        q = q.where(Record.id.in_(body.record_ids))
    else:
        q = q.where(Record.relevance_level.in_(body.levels))
    ids = list(ctx.db.scalars(q))
    if not ids:
        raise HTTPException(422, "没有可抽取的文献：需要已纳入、已解析全文的文献")
    return row(jobs.enqueue(ctx.db, "extract_cards", {"record_ids": ids}, project_id=ctx.project.id, user_id=ctx.user.id))


@router.get("/cards")
def list_cards(record_id: int | None = None, node: str | None = None, status: str | None = None, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id).order_by(EvidenceCard.id)
    if record_id:
        q = q.where(EvidenceCard.record_id == record_id)
    if status:
        q = q.where(EvidenceCard.verify_status == status)
    out = rows(ctx.db.scalars(q))
    if node:
        out = [c for c in out if any(n == node or n.startswith(node + ".") for n in c["outline_nodes"])]
    return out


class CardIn(BaseModel):
    record_id: int | None = None
    claim: str | None = None
    evidence_type: str | None = None
    model_or_population: str | None = None
    quantitative: str | None = None
    quote: str | None = None
    location: str | None = None
    limitations: str | None = None
    outline_nodes: list[str] | None = None
    secondary: bool | None = None
    verify_status: str | None = None


def _check_quote(ctx: ProjectCtx, card: EvidenceCard) -> None:
    ft = ctx.db.scalar(select(FullText).where(FullText.record_id == card.record_id))
    if ft:
        card.quote_found = quote_in(card.quote, " ".join(p["text"] for p in ft.paragraphs))


@router.post("/cards")
def create_card(body: CardIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    rec = ctx.db.get(Record, body.record_id or 0)
    if not rec or rec.project_id != ctx.project.id:
        raise HTTPException(404, "文献不存在")
    data = body.model_dump(exclude_none=True, exclude={"record_id", "verify_status"})
    card = EvidenceCard(project_id=ctx.project.id, record_id=rec.id, citekey=rec.citekey, verify_status="verified", verified_by=ctx.user.id, **data)
    _check_quote(ctx, card)
    ctx.db.add(card)
    ctx.db.flush()
    audit.log(ctx.db, "card.create", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="card", object_id=card.card_id)
    ctx.db.commit()
    return row(card)


@router.patch("/cards/{card_id}")
def update_card(card_id: str, body: CardIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """逐卡核对：学生确认（verified）或修改（modified）；只有“已核对”的卡才能进入写作证据包。"""
    card = ctx.db.scalar(select(EvidenceCard).where(EvidenceCard.card_id == card_id, EvidenceCard.project_id == ctx.project.id))
    if not card:
        raise HTTPException(404, "证据卡不存在")
    changes = body.model_dump(exclude_none=True, exclude={"record_id"})
    content_changed = any(k in changes and changes[k] != getattr(card, k) for k in ("claim", "quote", "location", "quantitative", "evidence_type", "model_or_population", "limitations"))
    for k, v in changes.items():
        setattr(card, k, v)
    if "quote" in changes:
        _check_quote(ctx, card)
    if body.verify_status in ("verified", "modified") or content_changed:
        card.verify_status = "modified" if content_changed and card.extracted_by_run else (body.verify_status or "verified")
        card.verified_by = ctx.user.id
    audit.log(ctx.db, "card.update", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="card", object_id=card.card_id, detail={"fields": sorted(changes), "status": card.verify_status})
    ctx.db.commit()
    return row(card)


@router.delete("/cards/{card_id}")
def delete_card(card_id: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    card = ctx.db.scalar(select(EvidenceCard).where(EvidenceCard.card_id == card_id, EvidenceCard.project_id == ctx.project.id))
    if card:
        ctx.db.delete(card)
        audit.log(ctx.db, "card.delete", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="card", object_id=card_id)
        ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ D3 大纲（学生先写）


class OutlineIn(BaseModel):
    nodes: list[dict]


def _validate_nodes(nodes: list[dict]) -> list[dict]:
    out, prev_level = [], 0
    for n in nodes:
        nid = str(n.get("id", "")).strip()
        if not nid:
            raise HTTPException(422, "每个大纲节点都需要编号（如 2.1）")
        level = int(n.get("level") or nid.count(".") + 1)
        if level > prev_level + 1:
            raise HTTPException(422, f"大纲层级跳级：{nid}（H1 → H2 → H3 不跳级）")
        prev_level = level
        out.append({"id": nid, "title": n.get("title", ""), "thesis": n.get("thesis", ""), "level": level})
    return out


@router.get("/outlines")
def list_outlines(ctx: ProjectCtx = Depends(get_project_ctx)):
    return rows(ctx.db.scalars(select(Outline).where(Outline.project_id == ctx.project.id).order_by(Outline.version.desc())))


@router.post("/outlines")
def save_outline(body: OutlineIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """第一版必须由学生独立撰写（student_draft），保存后用于与定稿对比，体现学生的独立贡献。"""
    ctx.require_writer()
    nodes = _validate_nodes(body.nodes)
    cur = latest_outline(ctx.db, ctx.project.id)
    kind = "student_draft" if cur is None else "revision"
    o = Outline(project_id=ctx.project.id, version=(cur.version + 1) if cur else 1, kind=kind, nodes=nodes, created_by=ctx.user.id)
    ctx.db.add(o)
    ctx.db.flush()
    audit.log(ctx.db, "outline.save", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="outline", object_id=f"outline@v{o.version}", detail={"kind": kind, "nodes": len(nodes)})
    ctx.db.commit()
    return row(o)


@router.get("/outlines/diff")
def outline_diff(ctx: ProjectCtx = Depends(get_project_ctx)):
    """学生初稿与最新版的差异。"""
    first = ctx.db.scalar(select(Outline).where(Outline.project_id == ctx.project.id, Outline.kind == "student_draft"))
    last = latest_outline(ctx.db, ctx.project.id)
    if not first or not last:
        return {"added": [], "removed": [], "changed": []}
    a = {n["id"]: n for n in first.nodes}
    b = {n["id"]: n for n in last.nodes}
    return {
        "from": first.version,
        "to": last.version,
        "added": [b[k] for k in b if k not in a],
        "removed": [a[k] for k in a if k not in b],
        "changed": [{"id": k, "before": a[k], "after": b[k]} for k in a if k in b and (a[k]["title"], a[k]["thesis"]) != (b[k]["title"], b[k]["thesis"])],
    }


def _require_student_draft(ctx: ProjectCtx) -> Outline:
    """“先想后看”：学生提交初版大纲之前不显示 AI 的聚类 / 证据地图（5.2）。"""
    first = ctx.db.scalar(select(Outline).where(Outline.project_id == ctx.project.id, Outline.kind == "student_draft"))
    if not first:
        raise HTTPException(409, "请先独立撰写并保存初版大纲，再查看证据地图")
    return latest_outline(ctx.db, ctx.project.id)


# ------------------------------------------------------------------ D4 证据地图


def evidence_map_data(ctx: ProjectCtx, outline: Outline) -> dict:
    cards = list(ctx.db.scalars(select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id)))
    levels = {r.id: r.relevance_level for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id))}
    ids = [n["id"] for n in outline.nodes]
    per = defaultdict(list)
    unassigned = []
    for c in cards:
        hits = [n for n in c.outline_nodes if n in ids]
        if not hits:
            unassigned.append(c.card_id)
        for n in hits:
            per[n].append(c)
    nodes = []
    for n in outline.nodes:
        # 父节点汇总子节点的卡
        own = [c for k, cs in per.items() if k == n["id"] or k.startswith(n["id"] + ".") for c in cs]
        verified = [c for c in own if c.verify_status in ("verified", "modified")]
        a_recs = {c.record_id for c in verified if levels.get(c.record_id) == "A"}
        types = defaultdict(int)
        for c in verified:
            types[c.evidence_type or "unknown"] += 1
        weak = n["level"] >= 2 and (len(verified) < MIN_CARDS or not a_recs)
        nodes.append({**n, "count": len(verified), "unverified": len(own) - len(verified), "a_count": len(a_recs), "types": dict(types), "weak": weak, "card_ids": [c.card_id for c in verified]})
    return {"outline_version": outline.version, "min_cards": MIN_CARDS, "nodes": nodes, "unassigned": unassigned}


@router.get("/evidence-map")
def evidence_map(ctx: ProjectCtx = Depends(get_project_ctx)):
    return evidence_map_data(ctx, _require_student_draft(ctx))


@router.post("/evidence-map/ai")
def evidence_map_ai(ctx: ProjectCtx = Depends(get_project_ctx)):
    """P12：证据地图与大纲对照（AI 不从零生成大纲，只做映射与诊断）。"""
    outline = _require_student_draft(ctx)
    data = evidence_map_data(ctx, outline)
    cards = ctx.db.scalars(select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id, EvidenceCard.verify_status.in_(["verified", "modified"])))
    levels = {r.id: r.relevance_level for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id))}
    card_text = "\n".join(f"{c.card_id} | {levels.get(c.record_id) or '?'} | {c.evidence_type} | {c.claim} | nodes={','.join(c.outline_nodes)}" for c in cards)
    res = gateway.call(
        ctx.db, task="P12", user=ctx.user, project=ctx.project,
        params={"outline": outline_text(outline), "cards": card_text or "（尚无已核对证据卡）", "min_cards": MIN_CARDS},
        ctx={"nodes": [n for n in data["nodes"] if n["level"] >= 2], "unassigned": len(data["unassigned"]), "min_cards": MIN_CARDS},
    )
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text}


# ------------------------------------------------------------------ D6 章节文献包


def section_pack(ctx: ProjectCtx, node: str) -> dict:
    cards = [
        c for c in ctx.db.scalars(select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id, EvidenceCard.verify_status.in_(["verified", "modified"])))
        if any(n == node or n.startswith(node + ".") for n in c.outline_nodes)
    ]
    rec_ids = {c.record_id for c in cards}
    related = list(ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.decision == "include")))
    direct = [r for r in related if r.id in rec_ids]
    indirect = [r for r in related if r.id not in rec_ids and node in (r.assigned_sections or [])]
    return {"node": node, "cards": rows(cards), "records_direct": rows(direct, exclude={"abstract"}), "records_indirect": rows(indirect, exclude={"abstract"})}


@router.get("/section-pack/{node}")
def get_section_pack(node: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    return section_pack(ctx, node)


@router.get("/section-pack/{node}/text", response_class=PlainTextResponse)
def section_pack_text(node: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    """可导出为文本供网页版 AI 使用（替代原工具 3 PaperProcessor、工具 4 SearchTxt）。"""
    p = section_pack(ctx, node)
    lines = [f"# 章节文献包 {node}", "", "## 证据卡（已核对）"]
    for c in p["cards"]:
        lines += [f"- [{c['card_id']}] [@{c['citekey']}] {c['claim']}", f"  - 类型：{c['evidence_type']}；模型 / 人群：{c['model_or_population']}；数值：{c['quantitative']}", f"  - 原文（{c['location']}）：“{c['quote']}”"]
    lines += ["", "## 题录（直接相关）"] + [f"- [@{r['citekey']}] {', '.join(r['authors'][:3])} ({r['year']}). {r['title']}. {r['journal']}. PMID {r['pmid'] or '-'} DOI {r['doi'] or '-'}" for r in p["records_direct"]]
    lines += ["", "## 题录（间接相关）"] + [f"- [@{r['citekey']}] {r['title']} ({r['year']})" for r in p["records_indirect"]]
    return PlainTextResponse("\n".join(lines))
