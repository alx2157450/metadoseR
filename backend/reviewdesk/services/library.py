"""文献库：题录导入（.nbib / RIS / BibTeX / 知网导出）、citekey 生成与去重（C1、C2）。"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Record

# ------------------------------------------------------------------ 解析器


def parse_nbib(text: str) -> list[dict]:
    """PubMed → Send to → Citation manager 导出的 MEDLINE (.nbib) 格式。"""
    records, cur, last = [], {}, None
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur:
                records.append(cur)
                cur, last = {}, None
            continue
        m = re.match(r"^([A-Z]{2,4})\s*-\s(.*)$", line)
        if m:
            tag, val = m.group(1), m.group(2).strip()
            cur.setdefault(tag, []).append(val)
            last = tag
        elif last and line.startswith("      "):
            cur[last][-1] += " " + line.strip()
    out = []
    for r in records:
        ids = r.get("AID", []) + r.get("LID", [])
        doi = next((re.sub(r"\s*\[doi\]", "", x) for x in ids if "[doi]" in x), None)
        dp = (r.get("DP") or [""])[0]
        out.append(
            {
                "pmid": (r.get("PMID") or [None])[0],
                "pmcid": (r.get("PMC") or [None])[0],
                "doi": doi,
                "title": " ".join(r.get("TI", [])),
                "authors": r.get("AU", []),
                "year": int(dp[:4]) if dp[:4].isdigit() else None,
                "journal": (r.get("JT") or r.get("TA") or [""])[0],
                "abstract": " ".join(r.get("AB", [])),
                "mesh": [re.sub(r"/.*$", "", x).lstrip("*") for x in r.get("MH", [])],
                "pub_types": r.get("PT", []),
                "source": "nbib",
            }
        )
    return out


def parse_ris(text: str) -> list[dict]:
    """RIS（EndNote / 知网 / 万方导出）。"""
    out, cur = [], {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$", line)
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "ER":
            if cur:
                out.append(cur)
            cur = {}
            continue
        cur.setdefault(tag, []).append(val)
    if cur:
        out.append(cur)
    res = []
    for r in out:
        y = (r.get("PY") or r.get("Y1") or r.get("DA") or [""])[0]
        ym = re.search(r"\d{4}", y)
        doi = (r.get("DO") or [None])[0]
        text_all = " ".join(sum(r.values(), []))
        pmid = None
        for an in r.get("AN", []) + r.get("ID", []):
            if an.isdigit():
                pmid = an
        res.append(
            {
                "pmid": pmid,
                "doi": doi,
                "title": " ".join(r.get("TI") or r.get("T1") or []),
                "authors": r.get("AU") or r.get("A1") or [],
                "year": int(ym.group(0)) if ym else None,
                "journal": (r.get("JO") or r.get("T2") or r.get("JF") or [""])[0],
                "abstract": " ".join(r.get("AB") or r.get("N2") or []),
                "mesh": r.get("KW", []),
                "pub_types": r.get("TY", []),
                "source": "cnki" if re.search(r"[一-鿿]", text_all) else "ris",
            }
        )
    return res


def _bib_fields(body: str) -> dict[str, str]:
    fields, i = {}, 0
    while i < len(body):
        m = re.compile(r"\s*,?\s*(\w[\w\-]*)\s*=\s*").match(body, i)
        if not m:
            break
        name, i = m.group(1).lower(), m.end()
        if i >= len(body):
            break
        if body[i] == "{":
            depth, j = 0, i
            while j < len(body):
                depth += body[j] == "{"
                depth -= body[j] == "}"
                j += 1
                if depth == 0:
                    break
            val, i = body[i + 1 : j - 1], j
        elif body[i] == '"':
            j = body.index('"', i + 1)
            val, i = body[i + 1 : j], j + 1
        else:
            m2 = re.compile(r"[^,}]+").match(body, i)
            val, i = (m2.group(0), m2.end()) if m2 else ("", i + 1)
        fields[name] = re.sub(r"\s+", " ", val.replace("{", "").replace("}", "")).strip()
    return fields


def parse_bibtex(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        kind = m.group(1).lower()
        if kind in ("comment", "string", "preamble"):
            continue
        start, depth, j = m.end(), 1, m.end()
        while j < len(text) and depth:
            depth += text[j] == "{"
            depth -= text[j] == "}"
            j += 1
        f = _bib_fields(text[start : j - 1])
        y = re.search(r"\d{4}", f.get("year", "") or f.get("date", ""))
        out.append(
            {
                "citekey": m.group(2),
                "pmid": f.get("pmid"),
                "pmcid": f.get("pmcid"),
                "doi": f.get("doi"),
                "title": f.get("title", ""),
                "authors": [a.strip() for a in re.split(r"\s+and\s+", f.get("author", "")) if a.strip()],
                "year": int(y.group(0)) if y else None,
                "journal": f.get("journal") or f.get("journaltitle") or f.get("booktitle", ""),
                "abstract": f.get("abstract", ""),
                "mesh": [k.strip() for k in re.split(r"[;,]", f.get("keywords", "")) if k.strip()],
                "pub_types": [kind],
                "url": f.get("url", ""),
                "source": "bibtex",
            }
        )
    return out


def parse_any(filename: str, text: str) -> list[dict]:
    name = filename.lower()
    if name.endswith(".bib") or text.lstrip().startswith("@"):
        return parse_bibtex(text)
    if name.endswith((".nbib", ".medline")) or re.search(r"^PMID-\s", text, re.M):
        return parse_nbib(text)
    if name.endswith((".ris", ".enw", ".txt")) or re.search(r"^TY\s{1,2}-", text, re.M):
        return parse_ris(text)
    raise ValueError("无法识别的文件格式：支持 .nbib / .ris / .bib（知网、万方请导出为 RIS 或 EndNote 格式）")


# ------------------------------------------------------------------ citekey（Better BibTeX 风格）


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def make_citekey(rec: dict) -> str:
    first = (rec.get("authors") or ["anon"])[0]
    last = first.split(",")[0].split()[0] if "," in first else first.split()[0] if first.split() else "anon"
    last = re.sub(r"[^a-z]", "", _ascii(last).lower()) or "anon"
    words = [w for w in re.findall(r"[a-z]+", _ascii(rec.get("title", "")).lower()) if w not in {"a", "an", "the", "of", "on", "in", "and", "for", "to"}]
    return f"{last}{rec.get('year') or 'nd'}{words[0] if words else ''}"


def unique_citekey(db: Session, project_id: str, base: str) -> str:
    existing = set(db.scalars(select(Record.citekey).where(Record.project_id == project_id, Record.citekey.like(f"{base}%"))))
    if base not in existing:
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        if base + suffix not in existing:
            return base + suffix
    return f"{base}-{len(existing)}"


# ------------------------------------------------------------------ 去重（PMID / DOI 精确 + 标题模糊）


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", (t or "").lower())


def norm_doi(d: str | None) -> str | None:
    if not d:
        return None
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", d.strip().lower())


def find_duplicate(db: Session, project_id: str, rec: dict, *, fuzzy: float = 0.93) -> tuple[Record | None, str]:
    """返回 (已存在记录, 匹配方式)。匹配方式：pmid / doi / title（模糊，需人工确认）。"""
    if rec.get("pmid"):
        r = db.scalar(select(Record).where(Record.project_id == project_id, Record.pmid == rec["pmid"], Record.duplicate_of.is_(None)))
        if r:
            return r, "pmid"
    doi = norm_doi(rec.get("doi"))
    if doi:
        r = db.scalar(select(Record).where(Record.project_id == project_id, Record.doi == doi, Record.duplicate_of.is_(None)))
        if r:
            return r, "doi"
    nt = norm_title(rec.get("title", ""))
    if len(nt) >= 20:
        q = select(Record).where(Record.project_id == project_id, Record.duplicate_of.is_(None))
        if rec.get("year"):
            q = q.where((Record.year.is_(None)) | (Record.year.between(rec["year"] - 1, rec["year"] + 1)))
        for r in db.scalars(q):
            other = norm_title(r.title)
            if other and SequenceMatcher(None, nt, other).ratio() >= fuzzy:
                return r, "title"
    return None, ""


def merge_into(existing: Record, rec: dict) -> list[str]:
    """保留信息最完整的一条：只补空缺字段，返回被补全的字段名。"""
    filled = []
    for f in ("pmid", "pmcid", "doi", "abstract", "journal", "year", "url"):
        v = rec.get(f)
        if v and not getattr(existing, f):
            setattr(existing, f, norm_doi(v) if f == "doi" else v)
            filled.append(f)
    for f in ("authors", "mesh", "pub_types"):
        if rec.get(f) and not getattr(existing, f):
            setattr(existing, f, rec[f])
            filled.append(f)
    return filled


def import_records(db: Session, project_id: str, items: list[dict], *, source: str, seed_ids: set[str] | None = None) -> dict:
    """导入并去重。精确匹配（PMID / DOI）自动合并；标题模糊匹配标记为“可疑重复”待人工确认。"""
    seed_ids = seed_ids or set()
    added = merged = suspect = 0
    new_records: list[Record] = []
    for it in items:
        existing, how = find_duplicate(db, project_id, it)
        if existing and how in ("pmid", "doi"):
            merge_into(existing, it)
            merged += 1
            continue
        key = it.get("citekey") or make_citekey(it)
        rec = Record(
            project_id=project_id,
            citekey=unique_citekey(db, project_id, key),
            pmid=it.get("pmid"),
            pmcid=it.get("pmcid"),
            doi=norm_doi(it.get("doi")),
            title=it.get("title", ""),
            authors=it.get("authors", []),
            year=it.get("year"),
            journal=it.get("journal", ""),
            abstract=it.get("abstract", ""),
            mesh=it.get("mesh", []),
            pub_types=it.get("pub_types", []),
            source=it.get("source") or source,
            url=it.get("url", ""),
            is_seed=bool(it.get("pmid") in seed_ids or norm_doi(it.get("doi")) in seed_ids),
        )
        if existing and how == "title":
            rec.extra = {"suspect_duplicate_of": existing.id}
            suspect += 1
        db.add(rec)
        db.flush()
        new_records.append(rec)
        added += 1
    return {"added": added, "merged": merged, "suspect_duplicates": suspect, "record_ids": [r.id for r in new_records]}
