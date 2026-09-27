"""外部连接器：PubMed E-utilities、Europe PMC、Crossref、OpenAlex、Unpaywall。

统一做缓存、限速、退避重试（9.7）；离线模式下禁止外部请求。
NCBI：无 key 每秒 3 次、有 key 每秒 10 次。
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..models import HttpCache

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"

_locks: dict[str, threading.Lock] = {}
_last: dict[str, float] = {}


def _throttle(host: str, per_sec: float) -> None:
    lock = _locks.setdefault(host, threading.Lock())
    with lock:
        wait = _last.get(host, 0) + 1.0 / per_sec - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


def http_get(url: str, params: dict[str, Any] | None = None, *, cache_hours: float = 24, per_sec: float = 5, headers: dict | None = None) -> str:
    s = get_settings()
    if s.offline:
        raise HTTPException(503, "当前为离线模式，无法访问外部数据源；请改用文件导入")
    full = str(httpx.URL(url, params={k: v for k, v in (params or {}).items() if v is not None}))
    key = hashlib.sha256(full.encode()).hexdigest()
    db: Session = SessionLocal()
    try:
        if cache_hours > 0:
            hit = db.get(HttpCache, key)
            if hit:
                at = hit.at if hit.at.tzinfo else hit.at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - at < timedelta(hours=cache_hours):
                    return hit.body
        host = httpx.URL(url).host
        last_exc: Exception | None = None
        for attempt in range(4):
            _throttle(host, per_sec)
            try:
                r = httpx.get(full, timeout=s.http_timeout, headers=headers or {}, follow_redirects=True)
                if r.status_code == 404:
                    return ""
                if r.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                r.raise_for_status()
                body = r.text
                if cache_hours > 0:
                    db.merge(HttpCache(key=key, url=full, body=body, at=datetime.now(timezone.utc)))
                    db.commit()
                return body
            except httpx.HTTPError as e:
                last_exc = e
                time.sleep(2**attempt)
        raise HTTPException(502, f"外部接口请求失败：{httpx.URL(url).host}（{last_exc}）")
    finally:
        db.close()


# ------------------------------------------------------------------ PubMed


def _ncbi_params(**kw) -> dict:
    s = get_settings()
    p = {"tool": "reviewdesk", "email": s.ncbi_email, "api_key": s.ncbi_api_key}
    p.update(kw)
    return p


def _ncbi_rate() -> float:
    return 9 if get_settings().ncbi_api_key else 2.8


def pubmed_search(query: str, retmax: int = 10000) -> dict:
    import json

    body = http_get(f"{EUTILS}/esearch.fcgi", _ncbi_params(db="pubmed", term=query, retmax=retmax, retmode="json"), cache_hours=6, per_sec=_ncbi_rate())
    d = json.loads(body)["esearchresult"]
    return {"count": int(d.get("count", 0)), "ids": d.get("idlist", []), "translation": d.get("querytranslation", ""), "errors": d.get("errorlist", {}), "warnings": d.get("warninglist", {})}


def pubmed_count(query: str) -> int:
    return pubmed_search(query, retmax=0)["count"]


def mesh_exists(term: str) -> bool:
    import json

    body = http_get(f"{EUTILS}/esearch.fcgi", _ncbi_params(db="mesh", term=f'"{term}"[MeSH Terms]', retmode="json"), cache_hours=24 * 30, per_sec=_ncbi_rate())
    return int(json.loads(body)["esearchresult"].get("count", 0)) > 0


def _text(el: ET.Element | None) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_pubmed_xml(xml: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml)
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if a is None:
            continue
        ids = {i.get("IdType"): _text(i) for i in art.iter("ArticleId")}
        year = None
        for path in ("Journal/JournalIssue/PubDate/Year", "ArticleDate/Year", "Journal/JournalIssue/PubDate/MedlineDate"):
            y = _text(a.find(path))
            if y and re.match(r"\d{4}", y):
                year = int(y[:4])
                break
        authors = []
        for au in a.iter("Author"):
            last, init = _text(au.find("LastName")), _text(au.find("Initials"))
            coll = _text(au.find("CollectiveName"))
            if last or coll:
                authors.append(f"{last} {init}".strip() if last else coll)
        abstract_parts = []
        for t in a.iter("AbstractText"):
            label = t.get("Label")
            abstract_parts.append((f"{label}: " if label else "") + _text(t))
        out.append(
            {
                "pmid": _text(mc.find("PMID")),
                "doi": ids.get("doi"),
                "pmcid": ids.get("pmc"),
                "title": _text(a.find("ArticleTitle")),
                "authors": authors,
                "year": year,
                "journal": _text(a.find("Journal/Title")) or _text(a.find("Journal/ISOAbbreviation")),
                "abstract": "\n".join(abstract_parts),
                "mesh": [_text(d) for d in mc.iter("DescriptorName")],
                "pub_types": [_text(p) for p in a.iter("PublicationType")],
                "source": "pubmed",
            }
        )
    return out


def pubmed_fetch(pmids: list[str]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(pmids), 200):
        chunk = pmids[i : i + 200]
        xml = http_get(f"{EUTILS}/efetch.fcgi", _ncbi_params(db="pubmed", id=",".join(chunk), retmode="xml"), cache_hours=24 * 7, per_sec=_ncbi_rate())
        if xml:
            out.extend(parse_pubmed_xml(xml))
    return out


def pubmed_year_trend(query: str, start: int, end: int) -> dict[int, int]:
    return {y: pubmed_count(f"({query}) AND {y}[dp]") for y in range(start, end + 1)}


# ------------------------------------------------------------------ Europe PMC 全文与预印本


def parse_jats(xml: str) -> list[dict]:
    """JATS XML → 带章节与段落编号的结构化全文。"""
    root = ET.fromstring(xml)
    paras: list[dict] = []
    abstract = root.find(".//abstract")
    if abstract is not None:
        for p in abstract.iter("p"):
            t = _text(p)
            if t:
                paras.append({"id": f"p{len(paras) + 1}", "section": "Abstract", "text": t})
    body = root.find(".//body")
    if body is not None:

        def walk(el: ET.Element, section: str):
            for child in el:
                if child.tag == "sec":
                    title = _text(child.find("title")) or section
                    walk(child, title if section in ("", "Body") else f"{section} / {title}")
                elif child.tag == "p":
                    t = _text(child)
                    if t:
                        paras.append({"id": f"p{len(paras) + 1}", "section": section or "Body", "text": t})

        walk(body, "Body")
    return paras


def europepmc_fulltext(pmcid: str) -> list[dict]:
    xml = http_get(f"{EPMC}/{pmcid}/fullTextXML", cache_hours=24 * 30)
    return parse_jats(xml) if xml else []


def europepmc_preprints(query: str, size: int = 100) -> list[dict]:
    import json

    body = http_get(f"{EPMC}/search", {"query": f"({query}) AND SRC:PPR", "format": "json", "resultType": "core", "pageSize": size}, cache_hours=6)
    out = []
    for r in json.loads(body).get("resultList", {}).get("result", []):
        out.append(
            {
                "title": r.get("title", ""),
                "authors": [a.strip() for a in (r.get("authorString") or "").split(",") if a.strip()],
                "year": int(r["pubYear"]) if r.get("pubYear") else None,
                "journal": (r.get("bookOrReportDetails") or {}).get("publisher", "Preprint"),
                "abstract": r.get("abstractText", ""),
                "doi": r.get("doi"),
                "pub_types": ["Preprint"],
                "source": "europepmc_preprint",
            }
        )
    return out


# ------------------------------------------------------------------ Crossref：元数据、撤稿与更正


def crossref_work(doi: str) -> dict | None:
    import json

    s = get_settings()
    body = http_get(f"{CROSSREF}/works/{doi}", {"mailto": s.crossref_mailto}, cache_hours=24 * 7, per_sec=10)
    if not body:
        return None
    return json.loads(body).get("message")


def retraction_status(work: dict) -> str:
    """Crossref 已收录 Retraction Watch 数据：检查 updated-by / update-to 中的撤稿、关注声明与更正。"""
    kinds = [u.get("type", "").lower() for u in (work.get("updated-by") or []) + (work.get("update-to") or [])]
    rel = work.get("relation") or {}
    if any("retract" in k for k in kinds) or "is-retracted-by" in rel:
        return "retracted"
    if any("concern" in k for k in kinds):
        return "concern"
    if any(k in ("correction", "erratum", "corrigendum") for k in kinds):
        return "correction"
    return "none"


def crossref_to_record(work: dict) -> dict:
    year = None
    for k in ("published-print", "published-online", "issued"):
        parts = (work.get(k) or {}).get("date-parts") or [[None]]
        if parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    return {
        "doi": work.get("DOI"),
        "title": (work.get("title") or [""])[0],
        "authors": [f"{a.get('family', '')} {''.join(x[0] for x in a.get('given', '').split() if x)}".strip() for a in work.get("author", [])],
        "year": year,
        "journal": (work.get("container-title") or [""])[0],
        "retraction_status": retraction_status(work),
    }


# ------------------------------------------------------------------ OpenAlex：引文追溯（B8）


def openalex_citations(doi: str, direction: str = "backward", limit: int = 200) -> list[dict]:
    import json

    s = get_settings()
    base = {"api_key": s.openalex_api_key}
    work_body = http_get(f"{OPENALEX}/works/https://doi.org/{doi}", base, cache_hours=24 * 7)
    if not work_body:
        return []
    work = json.loads(work_body)
    if direction == "backward":
        ids = [w.rsplit("/", 1)[-1] for w in work.get("referenced_works", [])][:limit]
        if not ids:
            return []
        body = http_get(f"{OPENALEX}/works", {**base, "filter": "openalex:" + "|".join(ids[:100]), "per-page": 100}, cache_hours=24 * 7)
    else:
        body = http_get(f"{OPENALEX}/works", {**base, "filter": f"cites:{work['id'].rsplit('/', 1)[-1]}", "per-page": min(limit, 200)}, cache_hours=24)
    out = []
    for w in json.loads(body).get("results", []):
        ids = w.get("ids") or {}
        out.append(
            {
                "title": w.get("title") or "",
                "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
                "pmid": (ids.get("pmid") or "").rsplit("/", 1)[-1] or None,
                "year": w.get("publication_year"),
                "journal": ((w.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
                "authors": [a["author"]["display_name"] for a in w.get("authorships", [])[:20]],
                "abstract": "",
                "source": f"openalex_{direction}",
            }
        )
    return out


# ------------------------------------------------------------------ Unpaywall：开放获取状态


def unpaywall(doi: str) -> dict | None:
    import json

    s = get_settings()
    if not s.unpaywall_email:
        return None
    body = http_get(f"https://api.unpaywall.org/v2/{doi}", {"email": s.unpaywall_email}, cache_hours=24 * 7)
    return json.loads(body) if body else None
