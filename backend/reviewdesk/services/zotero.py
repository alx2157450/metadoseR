"""Zotero 双向同步（C4，替代原“工具 2 BibTeX 标签同步器”）。

原做法改写 .bib 后重新导入 Zotero 会生成重复条目并丢失附件与笔记。
这里一律通过 Zotero Web API 在**原条目**上写入标签与子笔记，以 citekey / DOI / PMID 为主键匹配。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx

from ..config import get_settings


@dataclass
class ZoteroConfig:
    library_type: str  # users | groups
    library_id: str
    api_key: str
    base_url: str = ""

    @property
    def prefix(self) -> str:
        return f"{(self.base_url or get_settings().zotero_api_base).rstrip('/')}/{self.library_type}/{self.library_id}"


def item_keys(item: dict) -> dict[str, str]:
    """从 Zotero 条目中提取 citekey / DOI / PMID（Better BibTeX 写在 extra 的“Citation Key:”）。"""
    d = item.get("data", item)
    extra = d.get("extra", "") or ""
    out = {}
    ck = d.get("citationKey") or (re.search(r"^Citation Key:\s*(\S+)", extra, re.M) or [None, None])[1]
    if ck:
        out["citekey"] = ck
    if d.get("DOI"):
        out["doi"] = d["DOI"].lower()
    pm = re.search(r"^PMID:\s*(\d+)", extra, re.M)
    if pm:
        out["pmid"] = pm.group(1)
    return out


def match_item(items: list[dict], *, citekey: str | None, doi: str | None, pmid: str | None) -> dict | None:
    for it in items:
        k = item_keys(it)
        if citekey and k.get("citekey") == citekey:
            return it
    for it in items:
        k = item_keys(it)
        if (doi and k.get("doi") == doi.lower()) or (pmid and k.get("pmid") == pmid):
            return it
    return None


def note_html(record) -> str:
    parts = [f"<h2>综述协作平台 · {html.escape(record.citekey)}</h2>"]
    if record.relevance_level:
        parts.append(f"<p><b>相关性等级：</b>{record.relevance_level}</p>")
    if record.ai_key_finding:
        parts.append(f"<p><b>关键信息：</b>{html.escape(record.ai_key_finding)}</p>")
    if record.ai_quote:
        parts.append(f"<blockquote>{html.escape(record.ai_quote)}</blockquote>")
    if record.assigned_sections:
        parts.append(f"<p><b>大纲章节：</b>{', '.join(map(html.escape, record.assigned_sections))}</p>")
    return "".join(parts)


class ZoteroClient:  # pragma: no cover - 需要 Zotero 账号
    def __init__(self, cfg: ZoteroConfig):
        self.cfg = cfg
        self.h = {"Zotero-API-Key": cfg.api_key, "Zotero-API-Version": "3"}

    def search(self, q: str) -> list[dict]:
        r = httpx.get(f"{self.cfg.prefix}/items", params={"q": q, "qmode": "everything", "limit": 25}, headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()

    def ensure_collection(self, name: str) -> str:
        r = httpx.get(f"{self.cfg.prefix}/collections", params={"limit": 100}, headers=self.h, timeout=30)
        r.raise_for_status()
        for c in r.json():
            if c["data"]["name"] == name:
                return c["key"]
        r = httpx.post(f"{self.cfg.prefix}/collections", json=[{"name": name}], headers=self.h, timeout=30)
        r.raise_for_status()
        return r.json()["successful"]["0"]["key"]

    def update_item(self, item: dict, *, tags: list[str], collection: str | None) -> None:
        d = item["data"]
        cur = {t["tag"] for t in d.get("tags", [])}
        patch = {"tags": [{"tag": t} for t in sorted(cur | set(tags))]}
        if collection:
            patch["collections"] = sorted(set(d.get("collections", [])) | {collection})
        r = httpx.patch(
            f"{self.cfg.prefix}/items/{item['key']}",
            json=patch,
            headers={**self.h, "If-Unmodified-Since-Version": str(item["version"])},
            timeout=30,
        )
        r.raise_for_status()

    def add_note(self, parent_key: str, note: str) -> None:
        r = httpx.post(
            f"{self.cfg.prefix}/items",
            json=[{"itemType": "note", "parentItem": parent_key, "note": note, "tags": [{"tag": "reviewdesk"}]}],
            headers=self.h,
            timeout=30,
        )
        r.raise_for_status()

    def sync_records(self, records, collection_name: str) -> dict:
        coll = self.ensure_collection(collection_name)
        done, missing = [], []
        for rec in records:
            q = rec.doi or rec.pmid or rec.title[:80]
            item = match_item(self.search(q), citekey=rec.citekey, doi=rec.doi, pmid=rec.pmid)
            if not item:
                missing.append(rec.citekey)
                continue
            tags = [f"level:{rec.relevance_level}"] if rec.relevance_level else []
            tags += [f"section:{s}" for s in rec.assigned_sections or []]
            tags += ["reviewdesk:included"] if rec.decision == "include" else []
            self.update_item(item, tags=tags, collection=coll)
            self.add_note(item["key"], note_html(rec))
            done.append(rec.citekey)
        return {"synced": done, "not_found_in_zotero": missing}
