"""业务流水线：批量 AI 任务与外部数据任务的实现，供 API 同步调用或后台 worker 执行。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .gateway import gateway
from .jobs import handler, set_progress
from .models import Charter, ClaimCheck, EvidenceCard, FullText, Job, Outline, Paragraph, Project, Record, SearchRun, User
from .services import connectors
from .services.fulltext import fulltext_as_prompt
from .services.library import import_records, norm_doi
from .services.screening import rank_records
from .services.text import citekeys_in, quote_in, split_sentences, tokens

# ------------------------------------------------------------------ 任务书


def latest_charter(db: Session, project_id: str) -> Charter | None:
    return db.scalar(select(Charter).where(Charter.project_id == project_id).order_by(Charter.version.desc()).limit(1))


def approved_charter(db: Session, project_id: str) -> Charter | None:
    return db.scalar(
        select(Charter).where(Charter.project_id == project_id, Charter.status == "approved").order_by(Charter.version.desc()).limit(1)
    )


def charter_data(db: Session, project_id: str) -> dict[str, Any]:
    c = approved_charter(db, project_id) or latest_charter(db, project_id)
    return c.data if c else {}


def seed_ids(db: Session, project_id: str) -> set[str]:
    d = charter_data(db, project_id)
    out = set()
    for s in d.get("seed_ids", []) or []:
        s = str(s).strip()
        if s:
            out.add(norm_doi(s) if "/" in s else s)
    return out


def latest_outline(db: Session, project_id: str) -> Outline | None:
    return db.scalar(select(Outline).where(Outline.project_id == project_id).order_by(Outline.version.desc()).limit(1))


def outline_text(outline: Outline | None) -> str:
    if not outline:
        return "（尚无大纲）"
    return "\n".join(f"{'  ' * (n.get('level', 1) - 1)}{n['id']} {n.get('title', '')} —— {n.get('thesis', '')}" for n in outline.nodes)


# ------------------------------------------------------------------ 检索：PubMed 拉取（C1）


@handler("pubmed_import")
def pubmed_import(db: Session, job: Job, user: User) -> dict:
    run = db.get(SearchRun, job.payload["run_id"])
    ids = run.ids
    done = job.progress.get("done", 0)
    totals = {"added": 0, "merged": 0, "suspect_duplicates": 0}
    seeds = seed_ids(db, run.project_id)
    for i in range(done, len(ids), 200):
        items = connectors.pubmed_fetch(ids[i : i + 200])
        res = import_records(db, run.project_id, items, source=f"pubmed:run{run.id}", seed_ids=seeds)
        for k in totals:
            totals[k] += res[k]
        run.imported_count += res["added"]
        set_progress(db, job, done=min(len(ids), i + 200), total=len(ids))
    audit.log(db, "records.import", project_id=run.project_id, actor_id=user.id, object_type="search_run", object_id=run.id, detail=totals)
    db.commit()
    return totals


# ------------------------------------------------------------------ 元数据补全、撤稿检查、引文真实性（C3 / F2）


def _title_sim(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    return len(ta & tb) / max(1, len(ta | tb))


def enrich_record(rec: Record) -> None:
    if rec.doi:
        work = connectors.crossref_work(rec.doi)
        if not work:
            rec.resolve_status = "unresolved"
            return
        meta = connectors.crossref_to_record(work)
        rec.retraction_status = meta["retraction_status"]
        ok_title = _title_sim(rec.title, meta["title"]) >= 0.6 if rec.title else True
        ok_year = not rec.year or not meta["year"] or abs(rec.year - meta["year"]) <= 1
        rec.resolve_status = "ok" if ok_title and ok_year else "mismatch"
        for f in ("title", "journal", "year"):
            if not getattr(rec, f) and meta.get(f):
                setattr(rec, f, meta[f])
        if not rec.authors and meta["authors"]:
            rec.authors = meta["authors"]
        rec.extra = {**(rec.extra or {}), "crossref_title": meta["title"], "crossref_year": meta["year"]}
    elif rec.pmid:
        items = connectors.pubmed_fetch([rec.pmid])
        if not items:
            rec.resolve_status = "unresolved"
            return
        it = items[0]
        rec.resolve_status = "ok" if _title_sim(rec.title, it["title"]) >= 0.6 else "mismatch"
        if "Retracted Publication" in it["pub_types"]:
            rec.retraction_status = "retracted"
        elif rec.retraction_status == "unknown":
            rec.retraction_status = "none"
        if it.get("doi") and not rec.doi:
            rec.doi = norm_doi(it["doi"])
    else:
        rec.resolve_status = "unresolved"
    if any(pt.lower() == "preprint" for pt in rec.pub_types or []):
        rec.extra = {**(rec.extra or {}), "preprint": True}


@handler("enrich")
def enrich(db: Session, job: Job, user: User) -> dict:
    ids = job.payload["record_ids"]
    done = job.progress.get("done", 0)
    for i, rid in enumerate(ids[done:], start=done):
        rec = db.get(Record, rid)
        if rec:
            try:
                enrich_record(rec)
            except Exception as e:  # noqa: BLE001  单条失败不影响整批
                rec.resolve_status = "unresolved"
                rec.extra = {**(rec.extra or {}), "enrich_error": str(e)[:200]}
        set_progress(db, job, done=i + 1, total=len(ids))
    return {"checked": len(ids)}


# ------------------------------------------------------------------ AI 预筛与分级（C6 / C7，P10）


def _record_line(r: Record) -> str:
    return (
        f"[{r.id}] {r.title}\n出版类型：{', '.join(r.pub_types or [])}；MeSH：{', '.join((r.mesh or [])[:12])}\n"
        f"摘要：{r.abstract or '（无摘要）'}"
    )


def prescreen(db: Session, project: Project, user: User, records: list[Record], job: Job | None = None) -> dict:
    ch = charter_data(db, project.id)
    include = ch.get("include", [])
    exclude = ch.get("exclude", [])
    title = ch.get("working_title") or ch.get("mentor_topic") or project.name
    per_item = 450
    size = gateway.batch_size_for(db, user, project, "P10", per_item)
    todo = [r for r in records if r.ai_decision is None]
    done = invalid = 0
    for i in range(0, len(todo), size):
        batch = todo[i : i + size]
        res = gateway.call(
            db,
            task="P10",
            user=user,
            project=project,
            params={
                "working_title": title,
                "include": "；".join(include) or "（见任务书）",
                "exclude": "；".join(exclude) or "（无）",
                "records": "\n\n".join(_record_line(r) for r in batch),
                "n": len(batch),
            },
            ctx={
                "records": [{"id": r.id, "title": r.title, "abstract": r.abstract} for r in batch],
                "include_terms": include + [title] + ch.get("concepts_terms", []),
                "exclude_terms": exclude,
            },
        )
        by_id = {str(x.get("id")): x for x in (res.data or [])}
        for r in batch:
            x = by_id.get(str(r.id))
            if not x:
                invalid += 1
                continue
            r.ai_level = x["level"]
            r.ai_decision = x["decision"]
            r.ai_reason = x.get("reason", "")
            quote = x.get("evidence_quote", "")
            # 依据句必须来自摘要，否则不采信（避免“凭印象”分级）
            r.ai_quote = quote if not quote or quote_in(quote, f"{r.title} {r.abstract}") else f"[未在摘要中找到] {quote}"
            r.ai_key_finding = x.get("key_finding", "")
            r.ai_run_id = res.run_id
            if not r.relevance_level:
                r.relevance_level = x["level"]
            done += 1
        db.commit()
        if job:
            set_progress(db, job, done=min(len(todo), i + size), total=len(todo))
    all_recs = list(db.scalars(select(Record).where(Record.project_id == project.id, Record.duplicate_of.is_(None))))
    for rid, score in rank_records(all_recs).items():
        db.get(Record, rid).rank_score = score
    db.commit()
    return {"prescreened": done, "missing_or_invalid": invalid, "batch_size": size}


@handler("prescreen")
def prescreen_job(db: Session, job: Job, user: User) -> dict:
    project = db.get(Project, job.project_id)
    ids = job.payload.get("record_ids")
    q = select(Record).where(Record.project_id == project.id, Record.duplicate_of.is_(None))
    if ids:
        q = q.where(Record.id.in_(ids))
    return prescreen(db, project, user, list(db.scalars(q)), job)


# ------------------------------------------------------------------ 全文（C8）


@handler("fulltext_fetch")
def fulltext_fetch(db: Session, job: Job, user: User) -> dict:
    ids = job.payload["record_ids"]
    got = 0
    for i, rid in enumerate(ids):
        rec = db.get(Record, rid)
        if not rec or rec.fulltext_status == "parsed":
            continue
        pmcid = rec.pmcid
        if not pmcid and rec.doi:
            up = connectors.unpaywall(rec.doi)
            if up:
                rec.oa_status = up.get("oa_status") or rec.oa_status
        if pmcid:
            try:
                paras = connectors.europepmc_fulltext(pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}")
            except Exception:  # noqa: BLE001
                paras = []
            if paras:
                db.add(FullText(record_id=rec.id, source="EuropePMC", is_open_access=True, paragraphs=paras))
                rec.fulltext_status, rec.oa_status = "parsed", "open"
                got += 1
        set_progress(db, job, done=i + 1, total=len(ids))
    db.commit()
    return {"fetched": got, "requested": len(ids)}


# ------------------------------------------------------------------ 证据卡抽取（D1，P11）


def extract_cards(db: Session, project: Project, user: User, rec: Record) -> dict:
    ft = db.scalar(select(FullText).where(FullText.record_id == rec.id))
    if not ft:
        return {"record": rec.citekey, "cards": 0, "error": "尚无结构化全文"}
    ch = charter_data(db, project.id)
    outline = latest_outline(db, project.id)
    is_review = any("review" in (p or "").lower() for p in rec.pub_types or [])
    res = gateway.call(
        db,
        task="P11",
        user=user,
        project=project,
        params={
            "working_title": ch.get("working_title") or project.name,
            "outline": outline_text(outline),
            "citekey": rec.citekey,
            "fulltext": fulltext_as_prompt(ft.paragraphs),
        },
        ctx={
            "paragraphs": ft.paragraphs,
            "terms": ch.get("include", []) + [ch.get("working_title", ""), ch.get("mentor_topic", "")] + ch.get("concepts_terms", []),
            "is_review": is_review,
        },
    )
    full = " ".join(p["text"] for p in ft.paragraphs)
    n = 0
    for c in res.data or []:
        card = EvidenceCard(
            project_id=project.id,
            record_id=rec.id,
            citekey=rec.citekey,
            claim=c.get("claim", ""),
            evidence_type=c.get("evidence_type", "") or "",
            model_or_population=c.get("model_or_population", "") or "",
            quantitative=c.get("quantitative", "") or "",
            quote=c.get("quote", "") or "",
            location=c.get("location", "") or "",
            limitations=c.get("limitations", "") or "",
            outline_nodes=[str(x) for x in c.get("outline_nodes", []) or []],
            secondary=bool(c.get("secondary")),
            # 引句必须逐字出现在原文中，否则标记给学生重点核对
            quote_found=quote_in(c.get("quote", ""), full),
            extracted_by_run=res.run_id,
        )
        db.add(card)
        n += 1
    db.commit()
    return {"record": rec.citekey, "cards": n, "run_id": res.run_id, "valid": res.valid}


@handler("extract_cards")
def extract_cards_job(db: Session, job: Job, user: User) -> dict:
    project = db.get(Project, job.project_id)
    ids = job.payload["record_ids"]
    done_ids = set(job.progress.get("done_ids", []))
    results = []
    for rid in ids:
        if rid in done_ids:
            continue
        rec = db.get(Record, rid)
        if rec:
            results.append(extract_cards(db, project, user, rec))
        done_ids.add(rid)
        set_progress(db, job, done_ids=sorted(done_ids), done=len(done_ids), total=len(ids))
    return {"results": results}


# ------------------------------------------------------------------ 断言—引文核查（F1，P19）


def numbered_sentences(text: str) -> list[dict]:
    return [{"n": i + 1, "text": s} for i, s in enumerate(split_sentences(text))]


def source_context(db: Session, project_id: str, citekeys: list[str]) -> tuple[str, dict[str, dict]]:
    blocks, ctx = [], {}
    for key in citekeys:
        rec = db.scalar(select(Record).where(Record.project_id == project_id, Record.citekey == key))
        if not rec:
            blocks.append(f"### {key}\n（文献库中不存在该 citekey）")
            continue
        ft = db.scalar(select(FullText).where(FullText.record_id == rec.id))
        is_review = any("review" in (p or "").lower() for p in rec.pub_types or [])
        if ft:
            paras = ft.paragraphs
            blocks.append(f"### {key}（全文）\n" + fulltext_as_prompt(paras, 20000))
            ctx[key] = {"paragraphs": paras, "abstract_only": False, "is_review": is_review}
        elif rec.abstract:
            paras = [{"id": "abstract", "section": "Abstract", "text": rec.abstract}]
            blocks.append(f"### {key}（仅摘要）\n{rec.abstract}")
            ctx[key] = {"paragraphs": paras, "abstract_only": True, "is_review": is_review}
        else:
            blocks.append(f"### {key}\n（无全文与摘要）")
    return "\n\n".join(blocks), ctx


def check_paragraph(db: Session, project: Project, user: User, para: Paragraph) -> dict:
    sents = numbered_sentences(para.text)
    keys = citekeys_in(para.text)
    sources, ctx = source_context(db, project.id, keys)
    res = gateway.call(
        db,
        task="P19",
        user=user,
        project=project,
        params={"sentences": "\n".join(f"({s['n']}) {s['text']}" for s in sents), "sources": sources},
        ctx={"sentences": sents, "sources": ctx},
    )
    # 旧的待核查记录作废，重新生成
    for old in db.scalars(select(ClaimCheck).where(ClaimCheck.paragraph_id == para.id, ClaimCheck.status != "resolved")):
        old.status = "stale"
    n = 0
    for c in (res.data or {}).get("claims", []):
        db.add(
            ClaimCheck(
                project_id=project.id,
                paragraph_id=para.id,
                paragraph_revision=para.revision,
                sentence=c.get("sentence"),
                claim=c.get("claim", ""),
                citekey=c.get("citekey", "") or "",
                ai_verdict=c.get("verdict"),
                quote=c.get("quote", "") or "",
                location=c.get("location", "") or "",
                note=c.get("note", "") or "",
                suggestion=c.get("suggestion", "") or "",
                ai_run_id=res.run_id,
            )
        )
        n += 1
    db.commit()
    return {"paragraph": para.id, "claims": n, "run_id": res.run_id, "valid": res.valid}


@handler("claim_check")
def claim_check_job(db: Session, job: Job, user: User) -> dict:
    project = db.get(Project, job.project_id)
    done_ids = set(job.progress.get("done_ids", []))
    out = []
    for pid in job.payload["paragraph_ids"]:
        if pid in done_ids:
            continue
        para = db.get(Paragraph, pid)
        if para and para.text.strip():
            out.append(check_paragraph(db, project, user, para))
        done_ids.add(pid)
        set_progress(db, job, done_ids=sorted(done_ids), done=len(done_ids), total=len(job.payload["paragraph_ids"]))
    return {"results": out}
