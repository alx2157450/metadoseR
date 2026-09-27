"""文本工具：中英文分句、引用解析、词项切分与相似度。"""
from __future__ import annotations

import math
import re
from collections import Counter

CITE_RE = re.compile(r"\[@([^\]]+)\]")
_CITEKEY_RE = re.compile(r"@?([\w:.\-/]+)")

STOPWORDS = set(
    """a an the of in on at to for from by with and or not no is are was were be been being this that these those
    it its as we our their they which who whom whose than then there here into onto over under between among
    also may might can could should would will shall do does did has have had such via using used use both either
    however moreover furthermore additionally while whereas although though but if when where how what why
    study studies result results show shows showed shown suggest suggests suggested found find data""".split()
)


def split_sentences(text: str) -> list[str]:
    """按中英文句末标点分句，保留引用标记在所属句内。"""
    text = text.strip()
    if not text:
        return []
    # 引用内部的分号 / 句点不能断句：先用占位符保护
    cites: list[str] = []

    def protect(m: re.Match) -> str:
        cites.append(m.group(0))
        return f"\x00{len(cites) - 1}\x00"

    guarded = CITE_RE.sub(protect, text)
    parts = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z一-鿿\x00(])", guarded)
    parts = [re.sub(r"\x00(\d+)\x00", lambda m: cites[int(m.group(1))], p) for p in parts]
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 句末引用“…. [@a]”并入上一句
        if out and re.fullmatch(r"(\[@[^\]]+\]\s*)+[.。]?", p):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def citekeys_in(text: str) -> list[str]:
    keys: list[str] = []
    for group in CITE_RE.findall(text):
        for part in re.split(r"[;,]", group):
            m = _CITEKEY_RE.search(part.strip())
            if m and m.group(1) not in keys:
                keys.append(m.group(1))
    return keys


def strip_citations(text: str) -> str:
    return CITE_RE.sub("", text).strip()


def tokens(text: str) -> list[str]:
    """英文按词、中文按二元组切分，去停用词。"""
    text = text.lower()
    out = [w for w in re.findall(r"[a-z][a-z0-9\-]{1,}|\d+(?:\.\d+)?", text) if w not in STOPWORDS]
    for run in re.findall(r"[一-鿿]+", text):
        out.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return out


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def overlap(a: str, b: str) -> float:
    """a 的词项在 b 中出现的比例（用于断言在原文中的覆盖度）。"""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta:
        return 0.0
    return len(ta & tb) / len(ta)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w一-鿿]+", " ", text.lower())).strip()


def quote_in(quote: str, source: str) -> bool:
    """检查引句是否逐字出现在原文中（忽略空白与标点差异）。"""
    q = normalize(quote)
    return bool(q) and q in normalize(source)


class BM25:
    """轻量 BM25，用于章节文献包、证据问答等本地检索（单机版无需向量库）。"""

    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.docs = [tokens(d) for d in docs]
        self.k1, self.b = k1, b
        self.avgdl = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in self.docs]

    def scores(self, query: str) -> list[float]:
        q = tokens(query)
        res = []
        for tf, d in zip(self.tf, self.docs):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / (self.avgdl or 1)))
            res.append(s)
        return res
