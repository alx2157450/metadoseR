"""全文获取与解析（C8）：PMC / Europe PMC 开放获取全文、上传文件、GROBID 解析 PDF。

输出统一为带章节与段落编号的结构化全文：[{"id": "p12", "section": "Results", "text": "..."}]，
证据卡的“原文定位”和断言核查都依赖这些段落编号。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx
from fastapi import HTTPException

from ..config import get_settings
from .connectors import parse_jats

TEI = "{http://www.tei-c.org/ns/1.0}"


def parse_plain(text: str) -> list[dict]:
    paras, section = [], "Body"
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        b = block.strip()
        if not b:
            continue
        h = re.match(r"^#{1,6}\s+(.+)$", b)
        if h and "\n" not in b:
            section = h.group(1).strip()
            continue
        if len(b) < 80 and "\n" not in b and not b.endswith((".", "。")) and re.match(r"^(\d+(\.\d+)*\.?\s+)?[A-Z一-鿿]", b):
            section = b
            continue
        paras.append({"id": f"p{len(paras) + 1}", "section": section, "text": re.sub(r"\s*\n\s*", " ", b)})
    return paras


def parse_tei(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    paras: list[dict] = []
    for ab in root.iter(f"{TEI}abstract"):
        for p in ab.iter(f"{TEI}p"):
            t = "".join(p.itertext()).strip()
            if t:
                paras.append({"id": f"p{len(paras) + 1}", "section": "Abstract", "text": t})
    body = root.find(f".//{TEI}body")
    if body is not None:
        for div in body.iter(f"{TEI}div"):
            head = div.find(f"{TEI}head")
            sec = "".join(head.itertext()).strip() if head is not None else "Body"
            for p in div.findall(f"{TEI}p"):
                t = "".join(p.itertext()).strip()
                if t:
                    paras.append({"id": f"p{len(paras) + 1}", "section": sec, "text": t})
    return paras


def grobid_pdf(data: bytes) -> list[dict]:  # pragma: no cover - 需要 GROBID 服务
    s = get_settings()
    if not s.grobid_url:
        raise HTTPException(
            422,
            "PDF 解析需要 GROBID 服务：请在服务器版启用 grobid 容器并设置 REVIEWDESK_GROBID_URL，"
            "或上传 JATS XML / 纯文本全文",
        )
    r = httpx.post(
        s.grobid_url.rstrip("/") + "/api/processFulltextDocument",
        files={"input": ("paper.pdf", data, "application/pdf")},
        data={"segmentSentences": "0"},
        timeout=300,
    )
    r.raise_for_status()
    return parse_tei(r.text)


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], str]:
    name = filename.lower()
    if name.endswith(".pdf"):
        return grobid_pdf(data), "grobid"
    text = data.decode("utf-8", errors="replace")
    if name.endswith((".xml", ".nxml")):
        if "tei-c.org" in text[:2000]:
            return parse_tei(text), "tei"
        return parse_jats(text), "jats"
    return parse_plain(text), "text"


def fulltext_as_prompt(paragraphs: list[dict], max_chars: int = 60000) -> str:
    out, n = [], 0
    for p in paragraphs:
        line = f"[{p['id']}] ({p.get('section', '')}) {p['text']}"
        n += len(line)
        if n > max_chars:
            out.append("……（全文过长，已截断）")
            break
        out.append(line)
    return "\n".join(out)
