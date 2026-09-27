"""模块 C：文献库与筛选（阶段三）。"""
from __future__ import annotations

import random
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import audit, jobs
from ..gateway import gateway
from ..gateway.prompts import extract_json, validate
from ..models import FullText, Record, ScreeningVote, SearchRun, utcnow
from ..pipelines import _record_line, charter_data, prescreen, seed_ids
from ..security import ProjectCtx, get_project_ctx
from ..services.export import bibtex_entry
from ..services.fulltext import parse_upload
from ..services.library import import_records, parse_any
from ..services.screening import calibration_metrics, calibration_sample_size, prisma_counts, rank_records, sample_excluded_for_audit
from ..services.zotero import ZoteroClient, ZoteroConfig
from ..storage import get_storage
from .common import row, rows

router = APIRouter(prefix="/api/projects/{project_id}")

SENSITIVITY_TARGET = 0.95
AI_FIELDS = {"ai_decision", "ai_level", "ai_reason", "ai_quote", "ai_key_finding", "ai_run_id", "rank_score"}


def _rec(ctx: ProjectCtx, rid: int) -> Record:
    r = ctx.db.get(Record, rid)
    if not r or r.project_id != ctx.project.id:
        raise HTTPException(404, "文献不存在")
    return r


# ------------------------------------------------------------------ C1 导入


@router.post("/records/import")
async def import_file(
    file: UploadFile = File(...),
    database: str = Form(""),
    query: str = Form(""),
    hit_count: int = Form(0),
    ctx: ProjectCtx = Depends(get_project_ctx),
):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        items = parse_any(file.filename or "", text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    run = SearchRun(
        project_id=ctx.project.id, database=database or f"文件导入：{file.filename}", platform="file import", query=query or "（文件导入）",
        hit_count=hit_count or len(items), kind="database" if database else "supplementary", run_by=ctx.user.id,
    )
    ctx.db.add(run)
    ctx.db.flush()
    res = import_records(ctx.db, ctx.project.id, items, source=database or "file", seed_ids=seed_ids(ctx.db, ctx.project.id))
    run.imported_count = res["added"]
    audit.log(ctx.db, "records.import_file", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="search_run", object_id=run.id, detail={k: v for k, v in res.items() if k != "record_ids"})
    ctx.db.commit()
    return {"parsed": len(items), **{k: v for k, v in res.items() if k != "record_ids"}}


@router.get("/records")
def list_records(
    status: str | None = None,
    level: str | None = None,
    q: str | None = None,
    sort: str = "rank",
    offset: int = 0,
    limit: int = 50,
    include_duplicates: bool = False,
    ctx: ProjectCtx = Depends(get_project_ctx),
):
    stmt = select(Record).where(Record.project_id == ctx.project.id)
    if not include_duplicates:
        stmt = stmt.where(Record.duplicate_of.is_(None))
    if status == "undecided":
        stmt = stmt.where(Record.decision.is_(None))
    elif status == "audit":
        stmt = stmt.where(Record.audit_sampled.is_(True), Record.decision.is_(None))
    elif status == "suspect":
        stmt = stmt.where(Record.extra["suspect_duplicate_of"].is_not(None))
    elif status in ("include", "exclude", "uncertain"):
        stmt = stmt.where(Record.decision == status)
    elif status == "ai_include":
        stmt = stmt.where(Record.decision.is_(None), Record.ai_decision.in_(["include", "uncertain"]))
    if level:
        stmt = stmt.where(Record.relevance_level == level)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Record.title.ilike(like) | Record.citekey.ilike(like) | Record.pmid.ilike(like) | Record.doi.ilike(like))
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery()))
    order = {"rank": Record.rank_score.desc().nulls_last(), "year": Record.year.desc().nulls_last(), "citekey": Record.citekey}.get(sort, Record.id)
    items = ctx.db.scalars(stmt.order_by(order, Record.id).offset(offset).limit(min(limit, 500)))
    return {"total": total, "items": rows(items)}


@router.get("/records/{rid}")
def get_record(rid: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    r = _rec(ctx, rid)
    ft = ctx.db.scalar(select(FullText).where(FullText.record_id == r.id))
    return row(r, extra={"fulltext_source": ft.source if ft else None})


class RecordPatch(BaseModel):
    relevance_level: str | None = None
    assigned_sections: list[str] | None = None
    is_seed: bool | None = None
    citekey: str | None = None


@router.patch("/records/{rid}")
def patch_record(rid: int, body: RecordPatch, ctx: ProjectCtx = Depends(get_project_ctx)):
    r = _rec(ctx, rid)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(r, k, v)
    audit.log(ctx.db, "record.update", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail=changes)
    ctx.db.commit()
    return row(r)


class DupIn(BaseModel):
    is_duplicate: bool


@router.post("/records/{rid}/duplicate")
def confirm_duplicate(rid: int, body: DupIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """可疑重复（标题模糊匹配）由人工确认。"""
    r = _rec(ctx, rid)
    of = (r.extra or {}).get("suspect_duplicate_of")
    if not of:
        raise HTTPException(409, "该记录不是可疑重复")
    extra = dict(r.extra)
    extra.pop("suspect_duplicate_of", None)
    r.extra = extra
    if body.is_duplicate:
        r.duplicate_of = of
    audit.log(ctx.db, "record.duplicate", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail={"of": of, "is_duplicate": body.is_duplicate})
    ctx.db.commit()
    return row(r)


# ------------------------------------------------------------------ C5 筛选校准


@router.post("/screening/calibration/sample")
def calibration_sample(ctx: ProjectCtx = Depends(get_project_ctx)):
    recs = list(ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))))
    already = [r for r in recs if (r.extra or {}).get("calibration")]
    n = calibration_sample_size(len(recs))
    pool = [r for r in recs if not (r.extra or {}).get("calibration")]
    need = max(0, n - len(already))
    for r in random.sample(pool, min(need, len(pool))):
        r.extra = {**(r.extra or {}), "calibration": True}
    ctx.db.commit()
    return {"sample_size": n, "total": len(recs)}


@router.get("/screening/calibration")
def calibration_list(ctx: ProjectCtx = Depends(get_project_ctx)):
    """盲评：学生完成标注前不显示该条的 AI 预判。"""
    recs = [r for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None)).order_by(Record.id)) if (r.extra or {}).get("calibration")]
    return [row(r, exclude=AI_FIELDS if r.calibration_label is None else None) for r in recs]


class LabelIn(BaseModel):
    label: str  # include | exclude


@router.put("/records/{rid}/calibration")
def calibration_label(rid: int, body: LabelIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    r = _rec(ctx, rid)
    if body.label not in ("include", "exclude"):
        raise HTTPException(400, "label 必须为 include 或 exclude")
    r.calibration_label = body.label
    audit.log(ctx.db, "screening.calibration_label", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail={"label": body.label})
    ctx.db.commit()
    return row(r)


@router.post("/screening/calibration/run-ai")
def calibration_run_ai(ctx: ProjectCtx = Depends(get_project_ctx)):
    recs = [r for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))) if (r.extra or {}).get("calibration")]
    unlabeled = [r for r in recs if r.calibration_label is None]
    if unlabeled:
        raise HTTPException(409, f"还有 {len(unlabeled)} 条校准样本未完成人工标注；请先独立筛选再运行 AI")
    return prescreen(ctx.db, ctx.project, ctx.user, recs)


@router.get("/screening/calibration/metrics")
def calibration_result(ctx: ProjectCtx = Depends(get_project_ctx)):
    recs = [r for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))) if (r.extra or {}).get("calibration") and r.calibration_label and r.ai_decision]
    m = calibration_metrics([(r.calibration_label, r.ai_decision) for r in recs])
    disagreements = [
        {"id": r.id, "citekey": r.citekey, "title": r.title, "human": r.calibration_label, "ai": r.ai_decision, "ai_level": r.ai_level, "ai_reason": r.ai_reason, "ai_quote": r.ai_quote}
        for r in recs
        if (r.calibration_label == "include") != (r.ai_decision in ("include", "uncertain"))
    ]
    state = ctx.project.state or {}
    return {**m, "target_sensitivity": SENSITIVITY_TARGET, "passed": bool(state.get("calibration_passed")), "disagreements": disagreements, "rounds": state.get("calibration_rounds", [])}


@router.post("/screening/calibration/accept")
def calibration_accept(ctx: ProjectCtx = Depends(get_project_ctx)):
    """校准达标后进入辅助模式；未达标需导师确认才可放行。"""
    m = calibration_result(ctx)
    ok = m["sensitivity"] is not None and m["sensitivity"] >= SENSITIVITY_TARGET
    if not ok:
        ctx.require_mentor()
    state = dict(ctx.project.state or {})
    state["calibration_passed"] = True
    state["calibration_rounds"] = state.get("calibration_rounds", []) + [{k: m[k] for k in ("n", "sensitivity", "specificity", "kappa")} | {"override": not ok, "at": utcnow().isoformat()}]
    ctx.project.state = state
    audit.log(ctx.db, "screening.calibration_accept", project_id=ctx.project.id, actor_id=ctx.user.id, detail={"sensitivity": m["sensitivity"], "override": not ok})
    ctx.db.commit()
    return {"passed": True, "override": not ok}


@router.post("/screening/calibration/reset")
def calibration_reset(ctx: ProjectCtx = Depends(get_project_ctx)):
    """修订筛选标准或 Prompt 后，清除校准样本的 AI 预判，再做一轮校准。"""
    n = 0
    for r in ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id)):
        if (r.extra or {}).get("calibration"):
            r.ai_decision = r.ai_level = r.ai_run_id = None
            r.ai_reason = r.ai_quote = r.ai_key_finding = ""
            n += 1
    state = dict(ctx.project.state or {})
    state["calibration_passed"] = False
    ctx.project.state = state
    audit.log(ctx.db, "screening.calibration_reset", project_id=ctx.project.id, actor_id=ctx.user.id)
    ctx.db.commit()
    return {"reset": n}


# ------------------------------------------------------------------ C6 / C7 辅助筛选与分级


@router.post("/screening/prescreen")
def prescreen_all(ctx: ProjectCtx = Depends(get_project_ctx)):
    if not (ctx.project.state or {}).get("calibration_passed"):
        raise HTTPException(409, "请先完成筛选校准（C5）并达到预设标准，才能进入 AI 辅助筛选")
    job = jobs.enqueue(ctx.db, "prescreen", {}, project_id=ctx.project.id, user_id=ctx.user.id)
    return row(job)


@router.post("/screening/rerank")
def rerank(ctx: ProjectCtx = Depends(get_project_ctx)):
    recs = list(ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))))
    for rid, s in rank_records(recs).items():
        ctx.db.get(Record, rid).rank_score = s
    ctx.db.commit()
    return {"ranked": len(recs)}


class DecisionIn(BaseModel):
    decision: str  # include | exclude | uncertain
    reason: str = ""


@router.post("/records/{rid}/decision")
def decide(rid: int, body: DecisionIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """最终决定由学生作出；记录决定来源（人工 / AI 预判后人工确认）。"""
    ctx.require_writer()
    if body.decision not in ("include", "exclude", "uncertain"):
        raise HTTPException(400, "decision 必须为 include / exclude / uncertain")
    r = _rec(ctx, rid)
    was_audit = r.audit_sampled and r.ai_decision == "exclude"
    r.decision, r.decision_reason = body.decision, body.reason
    r.decision_source = "ai_then_human" if r.ai_decision else "human"
    r.decided_by, r.decided_at = ctx.user.id, utcnow()
    if was_audit and body.decision == "include":
        # 抽查中发现漏检：回到校准轮（4.2）
        state = dict(ctx.project.state or {})
        state["calibration_passed"] = False
        state["audit_misses"] = state.get("audit_misses", 0) + 1
        ctx.project.state = state
    audit.log(ctx.db, "screening.decision", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail={"decision": body.decision, "source": r.decision_source, "audit_miss": was_audit and body.decision == "include"})
    ctx.db.commit()
    return row(r)


@router.post("/screening/audit-sample")
def audit_sample(ratio: float = 0.2, ctx: ProjectCtx = Depends(get_project_ctx)):
    ids = sample_excluded_for_audit(ctx.db, ctx.project.id, max(0.2, ratio))
    ctx.db.commit()
    return {"sampled": len(ids), "record_ids": ids}


@router.post("/screening/finalize-excludes")
def finalize_excludes(ctx: ProjectCtx = Depends(get_project_ctx)):
    """AI 预判“排除”的题录：抽查全部完成且未发现漏检后，其余按“AI 预判未复核”排除。"""
    ctx.require_writer()
    base = select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None), Record.decision.is_(None))
    pending_audit = ctx.db.scalar(select(func.count()).select_from(base.where(Record.audit_sampled.is_(True)).subquery()))
    if pending_audit:
        raise HTTPException(409, f"还有 {pending_audit} 条抽查样本未处理")
    if not ctx.db.scalar(select(func.count()).select_from(select(Record).where(Record.project_id == ctx.project.id, Record.audit_sampled.is_(True)).subquery())):
        raise HTTPException(409, "尚未进行排除抽查（至少随机抽查 20%）")
    if not (ctx.project.state or {}).get("calibration_passed"):
        raise HTTPException(409, "抽查发现漏检，已退回校准轮；请重新校准后再处理")
    if ctx.project.review_type in ("systematic", "meta"):
        raise HTTPException(409, "系统综述必须双人独立筛选，AI 不能替代任何一名评审者")
    rest = [r for r in ctx.db.scalars(base.where(Record.ai_decision == "exclude"))]
    for r in rest:
        r.decision, r.decision_source, r.decided_by, r.decided_at = "exclude", "ai_unreviewed", ctx.user.id, utcnow()
    audit.log(ctx.db, "screening.finalize_excludes", project_id=ctx.project.id, actor_id=ctx.user.id, detail={"count": len(rest)})
    ctx.db.commit()
    return {"excluded": len(rest)}


class VoteIn(BaseModel):
    decision: str
    reason: str = ""


@router.post("/records/{rid}/vote")
def vote(rid: int, body: VoteIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """系统综述双人独立筛选：两票一致自动定稿，不一致交导师仲裁。"""
    r = _rec(ctx, rid)
    v = ctx.db.scalar(select(ScreeningVote).where(ScreeningVote.record_id == r.id, ScreeningVote.user_id == ctx.user.id))
    if not v:
        v = ScreeningVote(record_id=r.id, user_id=ctx.user.id, decision=body.decision)
        ctx.db.add(v)
    v.decision, v.reason = body.decision, body.reason
    ctx.db.flush()
    votes = list(ctx.db.scalars(select(ScreeningVote).where(ScreeningVote.record_id == r.id)))
    status = "waiting"
    if len(votes) >= 2:
        ds = {x.decision for x in votes}
        if len(ds) == 1:
            r.decision, r.decision_source, r.decided_at = votes[0].decision, "human", utcnow()
            status = "agreed"
        else:
            r.extra = {**(r.extra or {}), "conflict": True}
            status = "conflict"
    audit.log(ctx.db, "screening.vote", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail={"decision": body.decision})
    ctx.db.commit()
    return {"status": status, "votes": rows(votes)}


# ------------------------------------------------------------------ 网页版 AI 兼容：导出分批文本、回填结果


@router.get("/screening/export-batches", response_class=PlainTextResponse)
def export_batches(size: int = 50, ctx: ProjectCtx = Depends(get_project_ctx)):
    recs = list(ctx.db.scalars(select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None), Record.ai_decision.is_(None)).order_by(Record.id)))
    ch = charter_data(ctx.db, ctx.project.id)
    out = []
    for i in range(0, len(recs), size):
        batch = recs[i : i + size]
        _, prompt = gateway.render("P10", {
            "working_title": ch.get("working_title") or ch.get("mentor_topic") or ctx.project.name,
            "include": "；".join(ch.get("include", [])), "exclude": "；".join(ch.get("exclude", [])),
            "records": "\n\n".join(_record_line(r) for r in batch), "n": len(batch),
        })
        out.append(f"==================== 第 {i // size + 1} 批（{len(batch)} 条）====================\n{prompt}")
    return PlainTextResponse("\n\n".join(out) or "没有待预筛的题录", headers={"Content-Disposition": "attachment; filename=P10-batches.txt"})


class PasteIn(BaseModel):
    text: str
    model: str = "web-ai"


@router.post("/screening/import-ai")
def import_ai(body: PasteIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """把网页版 AI 的 P10 输出粘贴回平台：校验 JSON 格式后合并入库，并记录为一次 AI 调用。"""
    from ..gateway.gateway import _hash
    from ..gateway.prompts import get_prompt
    from ..models import ModelRun

    try:
        data = extract_json(body.text)
    except ValueError as e:
        raise HTTPException(422, f"无法解析 JSON：{e}")
    errs = validate(data, get_prompt("P10").schema)
    if errs:
        raise HTTPException(422, {"message": "格式校验失败", "errors": errs[:20]})
    run = ModelRun(project_id=ctx.project.id, user_id=ctx.user.id, task="P10", prompt_version=get_prompt("P10").ref, provider="web-paste", model=body.model, input_hash="", output_hash=_hash(body.text), output_text=body.text)
    ctx.db.add(run)
    ctx.db.flush()
    n = 0
    for x in data:
        try:
            r = ctx.db.get(Record, int(x["id"]))
        except (TypeError, ValueError):
            continue
        if r and r.project_id == ctx.project.id:
            r.ai_level, r.ai_decision, r.ai_reason, r.ai_quote = x["level"], x["decision"], x.get("reason", ""), x.get("evidence_quote", "")
            r.ai_key_finding, r.ai_run_id = x.get("key_finding", ""), run.run_id
            r.relevance_level = r.relevance_level or x["level"]
            n += 1
    ctx.db.commit()
    return {"applied": n, "run_id": run.run_id}


# ------------------------------------------------------------------ C3 元数据补全 / C8 全文 / C9 PRISMA / C4 Zotero


@router.post("/records/enrich")
def enrich(only_included: bool = False, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Record.id).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))
    if only_included:
        q = q.where(Record.decision == "include")
    ids = list(ctx.db.scalars(q))
    return row(jobs.enqueue(ctx.db, "enrich", {"record_ids": ids}, project_id=ctx.project.id, user_id=ctx.user.id))


@router.post("/records/fulltext/fetch")
def fetch_fulltexts(ctx: ProjectCtx = Depends(get_project_ctx)):
    """开放获取全文优先（PMC / Europe PMC / Unpaywall）；A 级必须、B 级尽量。"""
    ids = list(ctx.db.scalars(select(Record.id).where(Record.project_id == ctx.project.id, Record.decision == "include", Record.fulltext_status != "parsed")))
    return row(jobs.enqueue(ctx.db, "fulltext_fetch", {"record_ids": ids}, project_id=ctx.project.id, user_id=ctx.user.id))


@router.post("/records/{rid}/fulltext")
async def upload_fulltext(rid: int, file: UploadFile = File(...), ctx: ProjectCtx = Depends(get_project_ctx)):
    """订阅全文由学生通过机构权限获取后上传；仅上传者本人可用，不在用户之间共享（9.7）。"""
    r = _rec(ctx, rid)
    data = await file.read()
    paras, source = parse_upload(file.filename or "", data)
    if not paras:
        raise HTTPException(422, "未能解析出任何段落")
    safe = re.sub(r"[^\w.\-]+", "_", Path(file.filename or "upload").name)[:120]
    key = f"{ctx.project.id}/fulltext/{r.id}/{ctx.user.id}/{safe}"
    get_storage().put(key, data)
    ft = ctx.db.scalar(select(FullText).where(FullText.record_id == r.id))
    if ft:
        ctx.db.delete(ft)
        ctx.db.flush()
    ctx.db.add(FullText(record_id=r.id, source=f"upload:{source}", is_open_access=False, uploaded_by=ctx.user.id, object_key=key, paragraphs=paras))
    r.fulltext_status = "parsed"
    audit.log(ctx.db, "fulltext.upload", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=r.id, detail={"paragraphs": len(paras), "parser": source})
    ctx.db.commit()
    return {"paragraphs": len(paras), "parser": source}


@router.get("/records/{rid}/fulltext")
def get_fulltext(rid: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    r = _rec(ctx, rid)
    ft = ctx.db.scalar(select(FullText).where(FullText.record_id == r.id))
    if not ft:
        raise HTTPException(404, "尚无结构化全文")
    if not ft.is_open_access and ft.uploaded_by not in (ctx.user.id, None) and not ctx.is_mentor:
        raise HTTPException(403, "受版权保护的全文仅供上传者本人使用")
    return {"source": ft.source, "is_open_access": ft.is_open_access, "paragraphs": ft.paragraphs}


@router.get("/prisma")
def prisma(ctx: ProjectCtx = Depends(get_project_ctx)):
    return prisma_counts(ctx.db, ctx.project.id)


class ZoteroIn(BaseModel):
    library_type: str = "users"
    library_id: str
    api_key: str
    collection: str | None = None
    only_included: bool = True


@router.post("/zotero/sync")
def zotero_sync(body: ZoteroIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))
    if body.only_included:
        q = q.where(Record.decision == "include")
    client = ZoteroClient(ZoteroConfig(body.library_type, body.library_id, body.api_key))
    res = client.sync_records(list(ctx.db.scalars(q)), body.collection or f"ReviewDesk · {ctx.project.name}")
    audit.log(ctx.db, "zotero.sync", project_id=ctx.project.id, actor_id=ctx.user.id, detail={"synced": len(res["synced"]), "missing": len(res["not_found_in_zotero"])})
    ctx.db.commit()
    return res


@router.get("/records.bib", response_class=PlainTextResponse)
def export_bib(only_included: bool = True, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Record).where(Record.project_id == ctx.project.id, Record.duplicate_of.is_(None))
    if only_included:
        q = q.where(Record.decision == "include")
    return PlainTextResponse("\n".join(bibtex_entry(r) for r in ctx.db.scalars(q)), headers={"Content-Disposition": "attachment; filename=references.bib"})
