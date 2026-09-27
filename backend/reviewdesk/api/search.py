"""模块 B：检索策略构建与验证（阶段二）。"""
from __future__ import annotations

import csv
import io
import random
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from .. import audit, jobs
from ..gateway import gateway
from ..models import Record, SearchQuery, SearchRun
from ..pipelines import charter_data, seed_ids
from ..security import ProjectCtx, get_project_ctx
from ..services import connectors, query_syntax
from ..services.library import import_records, norm_doi
from .common import row, rows

router = APIRouter(prefix="/api/projects/{project_id}/search")


class QueryIn(BaseModel):
    query: str
    database: str = "pubmed"
    label: str = ""
    origin: str = "human"


@router.post("/validate")
def validate(body: QueryIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    if body.database != "pubmed":
        return {"ok": True, "issues": [{"level": "warning", "code": "not_pubmed", "message": "非 PubMed 检索式不做语法校验，请在目标库中试检", "fragment": ""}]}
    return query_syntax.validate_pubmed(body.query).as_dict()


class GenerateIn(BaseModel):
    concepts: list[dict] | None = None
    provider: str | None = None


@router.post("/generate")
def generate(body: GenerateIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """P06：由任务书核心概念生成 PubMed 检索式；结果自动经过语法校验器（B2）。"""
    ch = charter_data(ctx.db, ctx.project.id)
    concepts = body.concepts or ch.get("concepts", [])
    if not concepts:
        raise HTTPException(422, "请先在任务书中填写核心概念（或在请求中提供 concepts）")
    text = "、".join(f"{c.get('name')}（{'/'.join(c.get('terms', []) or [])}）" for c in concepts)
    res = gateway.call(
        ctx.db, task="P06", user=ctx.user, project=ctx.project,
        params={"mentor_topic": ch.get("mentor_topic", ctx.project.name), "concepts": text},
        ctx={"concepts": concepts}, provider_override=body.provider,
    )
    ctx.db.commit()
    out = {"run_id": res.run_id, "valid": res.valid, "errors": res.errors, "data": res.data, "text": res.text}
    if res.data:
        out["validation"] = query_syntax.validate_pubmed(res.data["query"]).as_dict()
    return out


class ConvertIn(BaseModel):
    query: str
    target: str  # scopus | wos | embase | cnki
    use_ai: bool = False


@router.post("/convert")
def convert(body: ConvertIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """B3 / P07：多库转换。默认用规则转换器；use_ai=True 时调用模型并记录日志。"""
    if not body.use_ai:
        return query_syntax.convert(body.query, body.target)
    names = {"scopus": "Scopus", "wos": "Web of Science 核心合集", "embase": "Embase", "cnki": "中国知网专业检索"}
    res = gateway.call(ctx.db, task="P07", user=ctx.user, project=ctx.project, params={"target_db": names.get(body.target, body.target), "pubmed_query": body.query}, ctx={"target": body.target})
    ctx.db.commit()
    return {**(res.data or {}), "run_id": res.run_id, "valid": res.valid, "errors": res.errors}


class MeshIn(BaseModel):
    terms: list[str]


@router.post("/mesh-check")
def mesh_check(body: MeshIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """B1：调用 NCBI MeSH 接口核实主题词是否存在。"""
    return {t: connectors.mesh_exists(t) for t in body.terms[:50]}


@router.get("/queries")
def list_queries(ctx: ProjectCtx = Depends(get_project_ctx)):
    return rows(ctx.db.scalars(select(SearchQuery).where(SearchQuery.project_id == ctx.project.id).order_by(SearchQuery.id.desc())))


@router.post("/queries")
def save_query(body: QueryIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    prev = ctx.db.scalar(select(SearchQuery).where(SearchQuery.project_id == ctx.project.id, SearchQuery.database == body.database).order_by(SearchQuery.version.desc()).limit(1))
    v = query_syntax.validate_pubmed(body.query).as_dict() if body.database == "pubmed" else {"ok": True, "issues": []}
    q = SearchQuery(project_id=ctx.project.id, version=(prev.version + 1) if prev else 1, database=body.database, query=body.query, label=body.label, origin=body.origin, validation=v, created_by=ctx.user.id)
    ctx.db.add(q)
    ctx.db.flush()
    audit.log(ctx.db, "search.query_save", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="query", object_id=q.id, detail={"database": body.database, "version": q.version})
    ctx.db.commit()
    return row(q)


@router.post("/queries/{qid}/final")
def mark_final(qid: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = ctx.db.get(SearchQuery, qid)
    if not q or q.project_id != ctx.project.id:
        raise HTTPException(404, "检索式不存在")
    if q.database == "pubmed" and not q.validation.get("ok"):
        raise HTTPException(422, "检索式未通过语法校验，不能定稿")
    for other in ctx.db.scalars(select(SearchQuery).where(SearchQuery.project_id == ctx.project.id, SearchQuery.database == q.database)):
        other.is_final = other.id == q.id
    audit.log(ctx.db, "search.query_final", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="query", object_id=q.id)
    ctx.db.commit()
    return row(q)


def recall_check(ctx: ProjectCtx, ids: list[str]) -> dict:
    """B4：种子文献召回检验。PMID 直接比对；DOI 种子先解析为 PMID。"""
    seeds = seed_ids(ctx.db, ctx.project.id)
    idset = set(ids)
    found, missing = [], []
    for s in sorted(seeds):
        pmid = s
        if not s.isdigit():
            rec = ctx.db.scalar(select(Record).where(Record.project_id == ctx.project.id, Record.doi == norm_doi(s)))
            pmid = rec.pmid if rec and rec.pmid else None
            if pmid is None:
                try:
                    res = connectors.pubmed_search(f"{s}[doi]", retmax=1)
                    pmid = res["ids"][0] if res["ids"] else None
                except HTTPException:
                    pmid = None
        (found if pmid and pmid in idset else missing).append(s)
    target = 1.0 if ctx.project.review_type in ("systematic", "meta") else 0.9
    rate = len(found) / len(seeds) if seeds else None
    return {"seeds": len(seeds), "found": found, "missing": missing, "recall": rate, "target": target, "passed": rate is not None and rate >= target}


class RunIn(BaseModel):
    query: str
    query_id: int | None = None


@router.post("/run")
def run_pubmed(body: RunIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    v = query_syntax.validate_pubmed(body.query)
    if not v.ok:
        raise HTTPException(422, {"message": "检索式未通过语法校验", "validation": v.as_dict()})
    res = connectors.pubmed_search(body.query)
    reviews = connectors.pubmed_count(f"({body.query}) AND (Review[pt] OR \"Systematic Review\"[pt])")
    this_year = date.today().year
    recent = connectors.pubmed_count(f"({body.query}) AND {this_year - 4}:{this_year}[dp]")
    run = SearchRun(
        project_id=ctx.project.id, query_id=body.query_id, database="pubmed", platform="PubMed (NCBI E-utilities)",
        query=body.query, hit_count=res["count"], ids=res["ids"], run_by=ctx.user.id,
    )
    run.recall = recall_check(ctx, res["ids"])
    ctx.db.add(run)
    ctx.db.flush()
    audit.log(ctx.db, "search.run", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="search_run", object_id=run.id, detail={"hits": res["count"]})
    ctx.db.commit()
    return {
        **row(run, exclude={"ids"}),
        "translation": res["translation"],
        "size": {"total": res["count"], "reviews": reviews, "recent_5y": recent, "recent_ratio": recent / res["count"] if res["count"] else None},
        "retrieved_ids": len(res["ids"]),
        "warnings": res["warnings"],
    }


@router.post("/runs/{run_id}/recall")
def rerun_recall(run_id: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    run = ctx.db.get(SearchRun, run_id)
    if not run or run.project_id != ctx.project.id:
        raise HTTPException(404, "检索记录不存在")
    run.recall = recall_check(ctx, run.ids)
    ctx.db.commit()
    return run.recall


@router.post("/runs/{run_id}/import")
def import_run(run_id: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    run = ctx.db.get(SearchRun, run_id)
    if not run or run.project_id != ctx.project.id:
        raise HTTPException(404, "检索记录不存在")
    job = jobs.enqueue(ctx.db, "pubmed_import", {"run_id": run.id}, project_id=ctx.project.id, user_id=ctx.user.id)
    return row(job)


class ManualRunIn(BaseModel):
    database: str
    platform: str = ""
    query: str
    hit_count: int
    limits: str = "none"
    kind: str = "database"
    run_date: str | None = None


@router.post("/runs/manual")
def manual_run(body: ManualRunIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """记录在其他数据库（Embase / WoS / Scopus / 知网 / 万方）试检的结果，写入 PRISMA-S 日志。"""
    run = SearchRun(project_id=ctx.project.id, database=body.database, platform=body.platform, query=body.query, hit_count=body.hit_count, limits=body.limits, kind=body.kind, run_by=ctx.user.id)
    if body.run_date:
        from datetime import datetime

        run.run_at = datetime.fromisoformat(body.run_date)
    ctx.db.add(run)
    ctx.db.flush()
    audit.log(ctx.db, "search.run_manual", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="search_run", object_id=run.id)
    ctx.db.commit()
    return row(run, exclude={"ids"})


@router.get("/runs")
def list_runs(ctx: ProjectCtx = Depends(get_project_ctx)):
    return rows(ctx.db.scalars(select(SearchRun).where(SearchRun.project_id == ctx.project.id).order_by(SearchRun.id)), exclude={"ids"})


@router.get("/log.csv", response_class=PlainTextResponse)
def search_log_csv(ctx: ProjectCtx = Depends(get_project_ctx)):
    """B7：PRISMA-S 检索日志。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["#", "数据库", "平台", "检索日期", "完整检索式", "限制条件", "命中数", "导入数", "类型", "种子召回"])
    for i, r in enumerate(ctx.db.scalars(select(SearchRun).where(SearchRun.project_id == ctx.project.id).order_by(SearchRun.id)), 1):
        rc = r.recall or {}
        w.writerow([i, r.database, r.platform, r.run_at.date().isoformat(), r.query, r.limits, r.hit_count, r.imported_count, r.kind, "" if rc.get("recall") is None else f"{rc['recall']:.0%}"])
    return PlainTextResponse("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=prisma-s-search-log.csv"})


# ------------------------------------------------------------------ B5：查准率抽查


@router.post("/runs/{run_id}/precision-sample")
def precision_sample(run_id: int, n: int = 30, ctx: ProjectCtx = Depends(get_project_ctx)):
    """随机抽取 30 条（不要只看最新 10 条，会高估相关度）。"""
    run = ctx.db.get(SearchRun, run_id)
    if not run or run.project_id != ctx.project.id:
        raise HTTPException(404, "检索记录不存在")
    ids = random.sample(run.ids, min(n, len(run.ids)))
    items = connectors.pubmed_fetch(ids) if ids else []
    run.precision_sample = [{"pmid": it["pmid"], "title": it["title"], "abstract": it["abstract"][:1500], "label": None} for it in items]
    ctx.db.commit()
    return run.precision_sample


class LabelsIn(BaseModel):
    labels: dict[str, str]  # pmid -> Y | N


@router.put("/runs/{run_id}/precision-sample")
def label_precision(run_id: int, body: LabelsIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    run = ctx.db.get(SearchRun, run_id)
    if not run or run.project_id != ctx.project.id:
        raise HTTPException(404, "检索记录不存在")
    sample = [dict(x) for x in run.precision_sample]
    for x in sample:
        if x["pmid"] in body.labels:
            x["label"] = body.labels[x["pmid"]]
    run.precision_sample = sample
    labeled = [x for x in sample if x["label"] in ("Y", "N")]
    prec = sum(x["label"] == "Y" for x in labeled) / len(labeled) if labeled else None
    ctx.db.commit()
    advice = None
    if prec is not None:
        advice = "≥ 50%，可进入筛选" if prec >= 0.5 else ("低于 30%，建议用 P09 诊断误检" if prec < 0.3 else "30%–50%，建议优化检索式后再筛选")
    return {"labeled": len(labeled), "precision": prec, "advice": advice}


@router.post("/runs/{run_id}/diagnose")
def diagnose(run_id: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    """P09：误检诊断与检索式优化。"""
    run = ctx.db.get(SearchRun, run_id)
    if not run or run.project_id != ctx.project.id:
        raise HTTPException(404, "检索记录不存在")
    labeled = [x for x in run.precision_sample if x.get("label") in ("Y", "N")]
    if not labeled:
        raise HTTPException(422, "请先完成查准率抽查标注")
    ch = charter_data(ctx.db, ctx.project.id)
    sample = "\n\n".join(f"[{i}] ({x['label']}) {x['title']}\n{x['abstract'][:800]}" for i, x in enumerate(labeled, 1))
    res = gateway.call(ctx.db, task="P09", user=ctx.user, project=ctx.project, params={"goal": "；".join(ch.get("core_questions", [])), "query": run.query, "n": len(labeled), "sample": sample, "seed_pmids": "、".join(ch.get("seed_ids", []))})
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text}


# ------------------------------------------------------------------ 摸底统计与补充检索（A4、B8、3.6）


class TrendIn(BaseModel):
    query: str
    years: int = 10


@router.post("/scoping")
def scoping(body: TrendIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """阶段一步骤 3：快速摸底——综述数量、原始研究数量、年度发文趋势与领域成熟度判断。"""
    y = date.today().year
    reviews_5 = connectors.pubmed_count(f"({body.query}) AND Review[pt] AND {y - 4}:{y}[dp]")
    reviews_10 = connectors.pubmed_count(f"({body.query}) AND Review[pt] AND {y - 9}:{y}[dp]")
    total_10 = connectors.pubmed_count(f"({body.query}) AND {y - 9}:{y}[dp]")
    trend = connectors.pubmed_year_trend(body.query, y - body.years + 1, y)
    if reviews_10 > 50:
        maturity = "领域综述积累充分：重叠风险高，必须做同题综述重叠分析（P04），切入角度要更窄、更新或跨领域"
    elif reviews_10 >= 10:
        maturity = "理想范围：已有综述可作为框架参考；分析时同时纳入高质量原始研究"
    elif reviews_10 >= 5:
        maturity = "综述相对较少：必须把原始研究一并纳入分析"
    else:
        maturity = "仅几篇：题目颗粒度过细或属新兴领域，与导师确认是否放宽到上位概念；补充预印本"
    return {"reviews_5y": reviews_5, "reviews_10y": reviews_10, "total_10y": total_10, "original_10y": total_10 - reviews_10, "trend": trend, "maturity": maturity}


class CitationsIn(BaseModel):
    record_id: int
    direction: str = "backward"  # backward | forward


@router.post("/citations")
def citation_trace(body: CitationsIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """B8：对 A 级核心文献做后向 / 前向引文追溯（OpenAlex），结果作为补充检索导入。"""
    rec = ctx.db.get(Record, body.record_id)
    if not rec or rec.project_id != ctx.project.id or not rec.doi:
        raise HTTPException(422, "需要带 DOI 的文献")
    items = connectors.openalex_citations(rec.doi, body.direction)
    run = SearchRun(project_id=ctx.project.id, database="OpenAlex", platform=f"引文追溯（{body.direction}）: {rec.citekey}", query=rec.doi, hit_count=len(items), kind="supplementary", run_by=ctx.user.id)
    ctx.db.add(run)
    ctx.db.flush()
    res = import_records(ctx.db, ctx.project.id, items, source=f"citations:{body.direction}", seed_ids=seed_ids(ctx.db, ctx.project.id))
    run.imported_count = res["added"]
    audit.log(ctx.db, "search.citations", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="record", object_id=rec.id, detail=res | {"record_ids": None})
    ctx.db.commit()
    return {k: v for k, v in res.items() if k != "record_ids"}


class PreprintIn(BaseModel):
    query: str


@router.post("/preprints")
def preprints(body: PreprintIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """3.6：通过 Europe PMC 检索 bioRxiv / medRxiv 预印本（引用时注明“预印本，未经同行评议”）。"""
    items = connectors.europepmc_preprints(body.query)
    run = SearchRun(project_id=ctx.project.id, database="Europe PMC (preprints)", platform="Europe PMC REST", query=f"({body.query}) AND SRC:PPR", hit_count=len(items), kind="supplementary", run_by=ctx.user.id)
    ctx.db.add(run)
    ctx.db.flush()
    res = import_records(ctx.db, ctx.project.id, items, source="preprint")
    run.imported_count = res["added"]
    ctx.db.commit()
    return {k: v for k, v in res.items() if k != "record_ids"}
