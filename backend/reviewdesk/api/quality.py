"""模块 F：质量与交付（阶段六）——断言核查、引文真实性、规范自查、AI 使用声明、导出。"""
from __future__ import annotations

import csv
import io
import random
from collections import Counter

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import audit, jobs
from ..db import get_db
from ..gateway import gateway
from ..gateway.prompts import get_prompt
from ..models import ChecklistState, ClaimCheck, EvidenceCard, ModelRun, Paragraph, Record, User, utcnow
from ..pipelines import charter_data, check_paragraph
from ..security import ProjectCtx, get_current_user, get_project_ctx
from ..services import export as exporter
from ..services.text import citekeys_in
from .common import row, rows
from .writing import provenance

router = APIRouter(prefix="/api/projects")

VERDICTS = ("strong", "partial", "none", "undetermined", "na")


# ------------------------------------------------------------------ F1 断言—引文核查


class CheckRunIn(BaseModel):
    paragraph_ids: list[str] | None = None
    sync: bool = False


@router.post("/{project_id}/checks/run")
def run_checks(body: CheckRunIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Paragraph).where(Paragraph.project_id == ctx.project.id)
    if body.paragraph_ids:
        q = q.where(Paragraph.id.in_(body.paragraph_ids))
    else:
        q = q.where(Paragraph.status == "done")
    paras = list(ctx.db.scalars(q))
    if not paras:
        raise HTTPException(422, "没有待核查的段落（段落标记为“完成”后进入核查队列）")
    if body.sync or len(paras) == 1:
        return {"results": [check_paragraph(ctx.db, ctx.project, ctx.user, p) for p in paras]}
    return row(jobs.enqueue(ctx.db, "claim_check", {"paragraph_ids": [p.id for p in paras]}, project_id=ctx.project.id, user_id=ctx.user.id))


@router.get("/{project_id}/checks")
def list_checks(paragraph_id: str | None = None, include_stale: bool = False, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(ClaimCheck).where(ClaimCheck.project_id == ctx.project.id).order_by(ClaimCheck.paragraph_id, ClaimCheck.sentence, ClaimCheck.id)
    if paragraph_id:
        q = q.where(ClaimCheck.paragraph_id == paragraph_id)
    if not include_stale:
        q = q.where(ClaimCheck.status != "stale")
    items = rows(ctx.db.scalars(q))
    summary = Counter((c["human_verdict"] or c["ai_verdict"] or "pending") for c in items)
    agree = [c for c in items if c["human_verdict"] and c["ai_verdict"]]
    return {
        "items": items,
        "summary": dict(summary),
        "pending": sum(1 for c in items if c["status"] == "pending"),
        "ai_human_agreement": (sum(c["human_verdict"] == c["ai_verdict"] for c in agree) / len(agree)) if agree else None,
    }


class CheckUpdate(BaseModel):
    human_verdict: str
    note: str | None = None


@router.patch("/{project_id}/checks/{check_id}")
def resolve_check(check_id: str, body: CheckUpdate, ctx: ProjectCtx = Depends(get_project_ctx)):
    """学生逐条确认或推翻 AI 判定。"""
    c = ctx.db.scalar(select(ClaimCheck).where(ClaimCheck.check_id == check_id, ClaimCheck.project_id == ctx.project.id))
    if not c:
        raise HTTPException(404, "核查记录不存在")
    if c.status == "stale":
        raise HTTPException(409, "段落已修改，该核查已过期；请重新运行核查")
    if body.human_verdict not in VERDICTS:
        raise HTTPException(400, f"判定必须为 {VERDICTS}")
    c.human_verdict, c.status, c.resolved_by = body.human_verdict, "resolved", ctx.user.id
    if body.note is not None:
        c.note = body.note
    audit.log(ctx.db, "check.resolve", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="check", object_id=c.check_id, detail={"ai": c.ai_verdict, "human": c.human_verdict})
    ctx.db.commit()
    return row(c)


@router.post("/{project_id}/checks/mentor-sample")
def mentor_sample(per_chapter: int = 10, ctx: ProjectCtx = Depends(get_project_ctx)):
    """G4：导师抽查（建议每章随机 10 条）。"""
    items = list(ctx.db.scalars(select(ClaimCheck).where(ClaimCheck.project_id == ctx.project.id, ClaimCheck.status == "resolved")))
    paras = {p.id: p.outline_node.split(".")[0] for p in ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id))}
    by_ch: dict[str, list[ClaimCheck]] = {}
    for c in items:
        by_ch.setdefault(paras.get(c.paragraph_id, "?"), []).append(c)
    picked = []
    for chapter, cs in by_ch.items():
        for c in random.sample(cs, min(per_chapter, len(cs))):
            c.mentor_sampled = True
            picked.append(c.check_id)
    ctx.db.commit()
    return {"sampled": len(picked), "check_ids": picked}


# ------------------------------------------------------------------ F2 引文真实性与撤稿检查


@router.get("/{project_id}/citations/report")
def citation_report(ctx: ProjectCtx = Depends(get_project_ctx)):
    paras = list(ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id)))
    cited: Counter = Counter()
    for p in paras:
        cited.update(citekeys_in(p.text))
    recs = {r.citekey: r for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id))}
    missing = [k for k in cited if k not in recs]
    used = [recs[k] for k in cited if k in recs]
    no_id = [r.citekey for r in used if not (r.pmid or r.doi)]
    unresolved = [r.citekey for r in used if r.resolve_status in ("unresolved", "mismatch")]
    unchecked = [r.citekey for r in used if r.resolve_status == "unchecked"]
    retracted = [r.citekey for r in used if r.retraction_status == "retracted"]
    concern = [r.citekey for r in used if r.retraction_status in ("concern", "correction")]
    preprints = [r.citekey for r in used if (r.extra or {}).get("preprint") or any((t or "").lower() == "preprint" for t in r.pub_types or [])]
    reviews_cited = [r.citekey for r in used if any("review" in (t or "").lower() for t in r.pub_types or [])]
    journals = Counter(r.journal for r in used if r.journal)
    first_authors = Counter((r.authors or ["?"])[0] for r in used)
    n = len(used) or 1
    return {
        "cited_total": len(cited),
        "not_in_library": missing,
        "no_identifier": no_id,
        "unresolved_or_mismatch": unresolved,
        "unchecked": unchecked,
        "retracted": retracted,
        "concern_or_correction": concern,
        "preprints": preprints,
        "reviews_cited": reviews_cited,
        "top_journals": [{"journal": j, "count": c, "share": c / n} for j, c in journals.most_common(5)],
        "top_first_authors": [{"author": a, "count": c, "share": c / n} for a, c in first_authors.most_common(5)],
        "ok": not (missing or no_id or unresolved or retracted),
    }


@router.post("/{project_id}/citations/verify")
def verify_citations(ctx: ProjectCtx = Depends(get_project_ctx)):
    """对正文中引用的全部文献做 PubMed / Crossref 解析与撤稿检查（后台任务）。"""
    keys = set()
    for p in ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id)):
        keys.update(citekeys_in(p.text))
    ids = list(ctx.db.scalars(select(Record.id).where(Record.project_id == ctx.project.id, Record.citekey.in_(keys)))) if keys else []
    return row(jobs.enqueue(ctx.db, "enrich", {"record_ids": ids}, project_id=ctx.project.id, user_id=ctx.user.id))


# ------------------------------------------------------------------ F3 规范自查


SANRA = [
    "论证综述对读者的重要性", "明确的目的或问题", "描述文献检索", "引用", "科学推理", "数据的恰当呈现",
]


@router.get("/{project_id}/checklist")
def get_checklist(ctx: ProjectCtx = Depends(get_project_ctx)):
    st = ctx.db.scalar(select(ChecklistState).where(ChecklistState.project_id == ctx.project.id))
    data = st.data if st else {}
    sanra = data.get("sanra", {})
    return {"sanra_items": SANRA, "data": data, "sanra_total": sum(int(v) for v in sanra.values() if str(v).isdigit()), "sanra_max": 12}


class ChecklistIn(BaseModel):
    data: dict


@router.put("/{project_id}/checklist")
def put_checklist(body: ChecklistIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    st = ctx.db.scalar(select(ChecklistState).where(ChecklistState.project_id == ctx.project.id))
    if not st:
        st = ChecklistState(project_id=ctx.project.id)
        ctx.db.add(st)
    st.data, st.updated_at = body.data, utcnow()
    ctx.db.commit()
    return get_checklist(ctx)


# ------------------------------------------------------------------ F4 AI 使用声明


TASK_EN = {
    "P01": "topic decomposition", "P02": "question list", "P03": "field scoping", "P04": "overlap analysis", "P05": "angle suggestions",
    "P06": "search-strategy development", "P07": "search translation", "P08": "matrix search planning", "P09": "search diagnostics",
    "P10": "title/abstract pre-screening", "P11": "evidence extraction", "P12": "evidence mapping", "P13": "multi-perspective questions",
    "P14": "section planning", "P15": "draft critique", "P16": "argument skeletons", "P17": "structure analysis of exemplar reviews",
    "P18": "writing evaluation", "P19": "claim–citation pre-checking", "P20": "figure planning", "P21": "disclosure drafting", "LANG": "language suggestions",
}


def usage_summary(ctx: ProjectCtx) -> dict:
    runs = list(ctx.db.scalars(select(ModelRun).where(ModelRun.project_id == ctx.project.id, ModelRun.status == "ok")))
    tasks = Counter(r.task for r in runs if r.task != "P21")
    models = sorted({f"{r.model} ({r.provider})" for r in runs if r.task != "P21"})
    recs = list(ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.decision.is_not(None))))
    src = Counter(r.decision_source for r in recs)
    cards = list(ctx.db.scalars(select(EvidenceCard).where(EvidenceCard.project_id == ctx.project.id)))
    ai_cards = [c for c in cards if c.extracted_by_run]
    paras = list(ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id)))
    pv = [provenance(p.spans or []) for p in paras]
    total = sum(x["total"] for x in pv)
    checks = list(ctx.db.scalars(select(ClaimCheck).where(ClaimCheck.project_id == ctx.project.id, ClaimCheck.status != "stale")))
    from ..models import Annotation

    accepted_lang = ctx.db.scalar(select(func.count()).select_from(Annotation).where(Annotation.project_id == ctx.project.id, Annotation.kind == "language", Annotation.status == "accepted")) or 0
    names = {t: get_prompt(t).name for t in tasks}
    return {
        "runs": sum(tasks.values()),
        "models": models,
        "tasks": dict(tasks),
        "task_names": [names[t] for t in sorted(tasks)],
        "task_names_en": sorted({TASK_EN.get(t, t) for t in tasks}),
        "screen_decisions": dict(src),
        "screen_human_ratio": (src.get("human", 0) + src.get("ai_then_human", 0)) / len(recs) if recs else 0,
        "cards_ai": len(ai_cards),
        "card_verified_ratio": sum(c.verify_status != "unverified" for c in ai_cards) / len(ai_cards) if ai_cards else 0,
        "human_char_ratio": sum(x["human"] for x in pv) / total if total else 0,
        "ai_accepted_chars": sum(x["ai_accepted"] for x in pv),
        "pasted_chars": sum(x["pasted"] for x in pv),
        "accepted_language": accepted_lang,
        "claims_checked": len(checks),
        "claims_human_resolved_ratio": sum(c.status == "resolved" for c in checks) / len(checks) if checks else 0,
    }


@router.get("/{project_id}/disclosure/usage")
def disclosure_usage(ctx: ProjectCtx = Depends(get_project_ctx)):
    return usage_summary(ctx)


class DisclosureIn(BaseModel):
    venue: str = "目标期刊"
    language: str = "中文与英文"


@router.post("/{project_id}/disclosure/draft")
def disclosure_draft(body: DisclosureIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """P21：根据使用日志起草 AI 使用声明，由学生确认、导师终审（G5）。"""
    u = usage_summary(ctx)
    lines = [
        f"- 使用的模型：{'、'.join(u['models']) or '无'}",
        f"- AI 任务及调用次数：{'; '.join(f'{get_prompt(t).name}×{n}' for t, n in sorted(u['tasks'].items()))}",
        f"- 题录筛选决定来源：{u['screen_decisions']}；人工确认比例 {u['screen_human_ratio']:.0%}",
        f"- AI 抽取证据卡 {u['cards_ai']} 张，人工核对比例 {u['card_verified_ratio']:.0%}",
        f"- 正文人工撰写字符占比 {u['human_char_ratio']:.0%}；接受的 AI 语言建议 {u['accepted_language']} 条；外部粘贴 {u['pasted_chars']} 字符",
        f"- 断言核查 {u['claims_checked']} 条，人工逐条确认比例 {u['claims_human_resolved_ratio']:.0%}",
    ]
    res = gateway.call(ctx.db, task="P21", user=ctx.user, project=ctx.project, params={"usage_summary": "\n".join(lines), "venue": body.venue, "language": body.language}, ctx={"usage": u})
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text, "usage": u}


# ------------------------------------------------------------------ F6 导出


def _title(ctx: ProjectCtx) -> str:
    ch = charter_data(ctx.db, ctx.project.id)
    return ch.get("working_title") or ch.get("mentor_topic") or ctx.project.name


def _cited_bib(ctx: ProjectCtx) -> str:
    keys = set()
    for p in ctx.db.scalars(select(Paragraph).where(Paragraph.project_id == ctx.project.id)):
        keys.update(citekeys_in(p.text))
    recs = ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.citekey.in_(keys))) if keys else []
    return "\n".join(exporter.bibtex_entry(r) for r in recs)


@router.get("/{project_id}/export/manuscript.md", response_class=PlainTextResponse)
def export_md(ctx: ProjectCtx = Depends(get_project_ctx)):
    return PlainTextResponse(exporter.manuscript_markdown(ctx.db, ctx.project, _title(ctx)), headers={"Content-Disposition": "attachment; filename=manuscript.md"})


@router.get("/{project_id}/export/references.bib", response_class=PlainTextResponse)
def export_bib(ctx: ProjectCtx = Depends(get_project_ctx)):
    return PlainTextResponse(_cited_bib(ctx), headers={"Content-Disposition": "attachment; filename=references.bib"})


@router.post("/{project_id}/export/manuscript.docx")
async def export_docx(csl: UploadFile | None = File(None), ctx: ProjectCtx = Depends(get_project_ctx)):
    md = exporter.manuscript_markdown(ctx.db, ctx.project, _title(ctx))
    try:
        data = exporter.to_docx(md, _cited_bib(ctx), await csl.read() if csl else None)
    except RuntimeError as e:
        raise HTTPException(501, str(e))
    audit.log(ctx.db, "export.docx", project_id=ctx.project.id, actor_id=ctx.user.id)
    ctx.db.commit()
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers={"Content-Disposition": "attachment; filename=manuscript.docx"})


@router.get("/{project_id}/export/check-report.csv", response_class=PlainTextResponse)
def export_checks(ctx: ProjectCtx = Depends(get_project_ctx)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["段落", "版本", "句号", "断言", "引用", "AI 判定", "人工判定", "原文依据", "位置", "差异说明", "修改建议", "状态", "导师抽查"])
    for c in ctx.db.scalars(select(ClaimCheck).where(ClaimCheck.project_id == ctx.project.id, ClaimCheck.status != "stale").order_by(ClaimCheck.paragraph_id, ClaimCheck.sentence)):
        w.writerow([c.paragraph_id, c.paragraph_revision, c.sentence, c.claim, c.citekey, c.ai_verdict, c.human_verdict, c.quote, c.location, c.note, c.suggestion, c.status, "是" if c.mentor_sampled else ""])
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=check-report.csv"})


@router.get("/{project_id}/export/package.zip")
def export_package(ctx: ProjectCtx = Depends(get_project_ctx)):
    data = exporter.export_package(ctx.db, ctx.project)
    audit.log(ctx.db, "export.package", project_id=ctx.project.id, actor_id=ctx.user.id)
    ctx.db.commit()
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename={ctx.project.id}.zip"})


@router.post("/import-package")
async def import_package(file: UploadFile = File(...), db=Depends(get_db), user: User = Depends(get_current_user)):
    """X5：项目包导入（单机版 ↔ 服务器版迁移、归档恢复）。"""
    try:
        p = exporter.import_package(db, await file.read(), tenant_id=user.tenant_id, owner_id=user.id)
    except (KeyError, ValueError) as e:
        db.rollback()
        raise HTTPException(422, f"项目包格式错误：{e}")
    audit.log(db, "project.import", project_id=p.id, actor_id=user.id)
    db.commit()
    return {"project_id": p.id, "name": p.name}
