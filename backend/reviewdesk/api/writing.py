"""模块 E：写作工作台（阶段五）。

每一段正文都由学生撰写；AI 在写作前提供证据和结构建议，在写作后提供审阅意见。
平台有意不提供“AI 直接生成段落并插入稿件”的功能（1.4）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from .. import audit
from ..gateway import gateway
from ..models import Annotation, ClaimCheck, EvidenceCard, Paragraph, ParagraphRevision, utcnow
from ..pipelines import charter_data, latest_outline, numbered_sentences, outline_text
from ..security import ProjectCtx, get_project_ctx
from ..services.text import split_sentences
from .common import row, rows

router = APIRouter(prefix="/api/projects/{project_id}")

ORIGINS = ("human", "ai_accepted", "pasted")
PASTE_WARN_CHARS = 200


def normalize_spans(spans: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in spans:
        text = s.get("text", "")
        origin = s.get("origin", "human")
        if origin not in ORIGINS:
            raise HTTPException(422, f"未知的文本来源：{origin}")
        if not text:
            continue
        run = s.get("ai_run")
        if out and out[-1]["origin"] == origin and out[-1].get("ai_run") == run:
            out[-1]["text"] += text
        else:
            out.append({"text": text, "origin": origin, "ai_run": run})
    return out


def apply_replacement(spans: list[dict], original: str, revised: str, run_id: str | None) -> list[dict] | None:
    """在段落中把 original 替换为 revised，替换部分标记为“接受的 AI 建议”。"""
    full = "".join(s["text"] for s in spans)
    start = full.find(original)
    if start < 0:
        return None
    end = start + len(original)
    out, pos, inserted = [], 0, False
    for s in spans:
        a, b = pos, pos + len(s["text"])
        pos = b
        if b <= start or a >= end:
            if a >= end and not inserted:
                out.append({"text": revised, "origin": "ai_accepted", "ai_run": run_id})
                inserted = True
            out.append(dict(s))
            continue
        if a < start:
            out.append({**s, "text": s["text"][: start - a]})
        if not inserted:
            out.append({"text": revised, "origin": "ai_accepted", "ai_run": run_id})
            inserted = True
        if b > end:
            out.append({**s, "text": s["text"][end - a :]})
    if not inserted:
        out.append({"text": revised, "origin": "ai_accepted", "ai_run": run_id})
    return normalize_spans(out)


def provenance(spans: list[dict]) -> dict:
    counts = {o: 0 for o in ORIGINS}
    for s in spans:
        counts[s["origin"]] += len(s["text"])
    total = sum(counts.values())
    return {**counts, "total": total, "human_ratio": counts["human"] / total if total else None}


def _para(ctx: ProjectCtx, pid: str) -> Paragraph:
    p = ctx.db.get(Paragraph, pid)
    if not p or p.project_id != ctx.project.id:
        raise HTTPException(404, "段落不存在")
    return p


def _para_out(p: Paragraph) -> dict:
    return row(p, extra={"text": p.text, "provenance": provenance(p.spans or [])})


@router.get("/paragraphs")
def list_paragraphs(node: str | None = None, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Paragraph).where(Paragraph.project_id == ctx.project.id).order_by(Paragraph.outline_node, Paragraph.order)
    if node:
        q = q.where(Paragraph.outline_node == node)
    return [_para_out(p) for p in ctx.db.scalars(q)]


class ParagraphIn(BaseModel):
    outline_node: str
    thesis: str = ""
    card_ids: list[str] = []
    order: int | None = None


@router.post("/paragraphs")
def create_paragraph(body: ParagraphIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    ctx.require_writer()
    n = len(list(ctx.db.scalars(select(Paragraph.id).where(Paragraph.project_id == ctx.project.id, Paragraph.outline_node == body.outline_node))))
    p = Paragraph(
        id=f"P-{body.outline_node}-{n + 1:02d}-{utcnow().strftime('%H%M%S%f')[-6:]}",
        project_id=ctx.project.id, outline_node=body.outline_node, thesis=body.thesis, card_ids=body.card_ids,
        order=body.order if body.order is not None else n, spans=[],
    )
    ctx.db.add(p)
    audit.log(ctx.db, "paragraph.create", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="paragraph", object_id=p.id)
    ctx.db.commit()
    return _para_out(p)


class ParagraphUpdate(BaseModel):
    spans: list[dict] | None = None
    thesis: str | None = None
    card_ids: list[str] | None = None
    order: int | None = None


@router.put("/paragraphs/{pid}")
def update_paragraph(pid: str, body: ParagraphUpdate, ctx: ProjectCtx = Depends(get_project_ctx)):
    """保存段落。文字变化时生成新版本；相关断言核查自动退回“待核查”（7.1）。"""
    ctx.require_writer()
    p = _para(ctx, pid)
    warnings = []
    for k in ("thesis", "card_ids", "order"):
        v = getattr(body, k)
        if v is not None:
            setattr(p, k, v)
    if body.spans is not None:
        spans = normalize_spans(body.spans)
        old_text = p.text
        if "".join(s["text"] for s in spans) != old_text:
            old_pasted = provenance(p.spans or [])["pasted"]
            p.spans = spans
            p.revision += 1
            p.updated_at = utcnow()
            ctx.db.add(ParagraphRevision(paragraph_id=p.id, revision=p.revision, spans=spans, created_by=ctx.user.id))
            stale = 0
            for c in ctx.db.scalars(select(ClaimCheck).where(ClaimCheck.paragraph_id == p.id, ClaimCheck.status != "stale")):
                c.status = "stale"
                stale += 1
            if p.status == "done":
                p.status = "drafting"
            new_pasted = provenance(spans)["pasted"] - old_pasted
            if new_pasted >= PASTE_WARN_CHARS:
                warnings.append(f"检测到大段外部粘贴（{new_pasted} 字符）。若来自 AI 生成内容，请改为自己撰写；引用他人文字请标注出处。")
            if stale:
                warnings.append(f"段落已修改，{stale} 条断言核查已退回待核查。")
            audit.log(ctx.db, "paragraph.revise", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="paragraph", object_id=p.id, detail={"revision": p.revision, "pasted_added": max(0, new_pasted)})
    ctx.db.commit()
    return {**_para_out(p), "warnings": warnings}


@router.post("/paragraphs/{pid}/done")
def mark_done(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    """段落标记为完成，自动进入核查队列（6.1 步骤⑥）。"""
    ctx.require_writer()
    p = _para(ctx, pid)
    if not p.text.strip():
        raise HTTPException(422, "空段落不能标记完成")
    p.status = "done"
    audit.log(ctx.db, "paragraph.done", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="paragraph", object_id=p.id, detail={"revision": p.revision})
    ctx.db.commit()
    return _para_out(p)


@router.delete("/paragraphs/{pid}")
def delete_paragraph(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    ctx.require_writer()
    p = _para(ctx, pid)
    ctx.db.delete(p)
    audit.log(ctx.db, "paragraph.delete", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="paragraph", object_id=pid)
    ctx.db.commit()
    return {"ok": True}


@router.get("/paragraphs/{pid}/history")
def history(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    _para(ctx, pid)
    revs = ctx.db.scalars(select(ParagraphRevision).where(ParagraphRevision.paragraph_id == pid).order_by(ParagraphRevision.revision))
    return [row(r, extra={"text": "".join(s["text"] for s in r.spans)}) for r in revs]


# ------------------------------------------------------------------ E1 段落证据包


def _verified_cards(ctx: ProjectCtx, p: Paragraph) -> list[EvidenceCard]:
    q = select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id, EvidenceCard.verify_status.in_(["verified", "modified"]))
    cards = list(ctx.db.scalars(q))
    chosen = [c for c in cards if c.card_id in (p.card_ids or [])]
    if not chosen:
        chosen = [c for c in cards if any(n == p.outline_node or n.startswith(p.outline_node + ".") for n in c.outline_nodes)]
    return chosen


@router.get("/paragraphs/{pid}/evidence")
def evidence_pack(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    p = _para(ctx, pid)
    return {"paragraph": p.id, "cards": rows(_verified_cards(ctx, p))}


def _cards_text(cards: list[EvidenceCard]) -> str:
    return "\n".join(f"{c.card_id} [@{c.citekey}] ({c.evidence_type}; {c.model_or_population}) {c.claim}\n  原文（{c.location}）：“{c.quote}”" for c in cards) or "（无）"


# ------------------------------------------------------------------ E2 论证骨架（L2）


@router.post("/paragraphs/{pid}/skeleton")
def skeleton(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    p = _para(ctx, pid)
    cards = _verified_cards(ctx, p)
    res = gateway.call(
        ctx.db, task="P16", user=ctx.user, project=ctx.project,
        params={"n": p.order + 1, "thesis": p.thesis or "（未填写本段核心论点）", "cards": _cards_text(cards)},
        ctx={"cards": [row(c) for c in cards]},
    )
    if res.data:
        # 骨架只给要点，不成句；超过 15 个词的要点截断，防止变相代写
        for pt in res.data.get("points", []):
            words = pt.get("text", "").split()
            if len(words) > 15:
                pt["text"] = " ".join(words[:15]) + " …"
    a = Annotation(project_id=ctx.project.id, paragraph_id=p.id, kind="skeleton", body=res.data or {"error": res.errors}, ai_run_id=res.run_id)
    ctx.db.add(a)
    ctx.db.commit()
    return row(a)


# ------------------------------------------------------------------ E4 草稿审阅（L2）


@router.post("/paragraphs/{pid}/review")
def review(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    p = _para(ctx, pid)
    if not p.text.strip():
        raise HTTPException(422, "段落为空")
    sents = numbered_sentences(p.text)
    cards = _verified_cards(ctx, p)
    ch = charter_data(ctx.db, ctx.project.id)
    res = gateway.call(
        ctx.db, task="P15", user=ctx.user, project=ctx.project,
        params={"journal": ", ".join(j.get("name", "") for j in ch.get("target_journals", []) or []) or "目标期刊", "sentences": "\n".join(f"({s['n']}) {s['text']}" for s in sents), "cards": _cards_text(cards)},
        ctx={"sentences": sents, "cards": [row(c) for c in cards]},
    )
    out = []
    for c in (res.data or {}).get("comments", []):
        a = Annotation(project_id=ctx.project.id, paragraph_id=p.id, kind="ai_review", sentence=c.get("sentence"), body={**c, "revision": p.revision}, ai_run_id=res.run_id)
        ctx.db.add(a)
        out.append(a)
    ctx.db.commit()
    return {"run_id": res.run_id, "valid": res.valid, "top3": (res.data or {}).get("top3", []), "annotations": rows(out)}


# ------------------------------------------------------------------ E5 语言建议（L3，修订模式）


@router.post("/paragraphs/{pid}/language")
def language(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    p = _para(ctx, pid)
    sents = numbered_sentences(p.text)
    lang = "英文" if sum(ch.isascii() for ch in p.text) > len(p.text) * 0.7 else "中文"
    res = gateway.call(ctx.db, task="LANG", user=ctx.user, project=ctx.project, params={"language": lang, "sentences": "\n".join(f"({s['n']}) {s['text']}" for s in sents)}, ctx={"sentences": sents})
    out = []
    for s in (res.data or {}).get("suggestions", []):
        if s.get("original") and s["original"] in p.text and s["original"] != s.get("revised"):
            a = Annotation(project_id=ctx.project.id, paragraph_id=p.id, kind="language", sentence=s.get("sentence"), body=s, ai_run_id=res.run_id)
            ctx.db.add(a)
            out.append(a)
    ctx.db.commit()
    return {"run_id": res.run_id, "annotations": rows(out)}


@router.get("/paragraphs/{pid}/annotations")
def list_annotations(pid: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    _para(ctx, pid)
    return rows(ctx.db.scalars(select(Annotation).where(Annotation.paragraph_id == pid).order_by(Annotation.id)))


class CommentIn(BaseModel):
    sentence: int | None = None
    text: str


@router.post("/paragraphs/{pid}/comments")
def mentor_comment(pid: str, body: CommentIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """导师批注（阶段五“每章批注”）。"""
    p = _para(ctx, pid)
    a = Annotation(project_id=ctx.project.id, paragraph_id=p.id, kind="mentor" if ctx.is_mentor else "note", sentence=body.sentence, body={"text": body.text}, author_id=ctx.user.id)
    ctx.db.add(a)
    ctx.db.commit()
    return row(a)


class AnnotationAction(BaseModel):
    action: str  # accept | reject | resolve


@router.post("/annotations/{aid}")
def act_annotation(aid: int, body: AnnotationAction, ctx: ProjectCtx = Depends(get_project_ctx)):
    a = ctx.db.get(Annotation, aid)
    if not a or a.project_id != ctx.project.id:
        raise HTTPException(404, "批注不存在")
    if body.action == "accept":
        if a.kind != "language":
            raise HTTPException(400, "只有语言建议可以“接受”；审阅意见请自行修改后标记已处理")
        ctx.require_writer()
        p = _para(ctx, a.paragraph_id)
        spans = apply_replacement(p.spans or [], a.body["original"], a.body["revised"], a.ai_run_id)
        if spans is None:
            raise HTTPException(409, "原文已变化，该建议无法应用")
        update_paragraph(p.id, ParagraphUpdate(spans=spans), ctx)
        a.status = "accepted"
    elif body.action in ("reject", "resolve"):
        a.status = "rejected" if body.action == "reject" else "resolved"
    else:
        raise HTTPException(400, "action 必须为 accept / reject / resolve")
    audit.log(ctx.db, f"annotation.{body.action}", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="annotation", object_id=a.id, detail={"kind": a.kind})
    ctx.db.commit()
    return row(a)


# ------------------------------------------------------------------ P14 章节规划、E7 写作评价


@router.post("/sections/{node}/plan")
def section_plan(node: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    outline = latest_outline(ctx.db, ctx.project.id)
    title = next((n["title"] for n in (outline.nodes if outline else []) if n["id"] == node), node)
    cards = [c for c in ctx.db.scalars(select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id, EvidenceCard.verify_status.in_(["verified", "modified"]))) if any(n == node or n.startswith(node + ".") for n in c.outline_nodes)]
    res = gateway.call(ctx.db, task="P14", user=ctx.user, project=ctx.project, params={"outline": outline_text(outline), "section": f"{node} {title}", "cards": _cards_text(cards)})
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text}


@router.post("/sections/{node}/evaluate")
def section_evaluate(node: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    """P18：写作六维评价（L2）。"""
    paras = ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id, Paragraph.outline_node == node).order_by(Paragraph.order))
    text = "\n\n".join(p.text for p in paras)
    if not text.strip():
        raise HTTPException(422, "该章节尚无正文")
    res = gateway.call(ctx.db, task="P18", user=ctx.user, project=ctx.project, params={"section": node, "text": text})
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text}


@router.get("/provenance")
def project_provenance(ctx: ProjectCtx = Depends(get_project_ctx)):
    """E6：来源标记统计——人工输入 / 接受的 AI 建议 / 外部粘贴。"""
    paras = list(ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id)))
    total = {o: 0 for o in ORIGINS}
    per = []
    for p in paras:
        pv = provenance(p.spans or [])
        for o in ORIGINS:
            total[o] += pv[o]
        per.append({"id": p.id, "node": p.outline_node, **pv, "sentences": len(split_sentences(p.text))})
    s = sum(total.values())
    return {**total, "total": s, "human_ratio": total["human"] / s if s else None, "paragraphs": per}
