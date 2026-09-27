"""检索式语法：PubMed 语法校验器（B2）、语义群组装（B1/P06）与多库转换（B3/P07 规则版）。

规范 3.2：原模板混用了 Scopus 语法（TITLE-ABS-KEY、W/n、? 通配符），粘贴到 PubMed
会被当作普通文本处理，导致结果失真。校验器在提交前自动拦截这些错误。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

KNOWN_TAGS = {
    "tiab", "ti", "ab", "mesh", "mh", "majr", "mesh:noexp", "mh:noexp", "majr:noexp", "pt", "dp", "la", "au",
    "1au", "lastau", "ta", "jour", "tw", "all", "all fields", "sb", "pmid", "uid", "nm", "sh", "ot", "ad", "affl",
    "filter", "pdat", "edat", "crdt", "mhda", "si", "gr", "is", "vi", "ip", "pg", "lid", "aid", "rn", "ps", "pa",
    "tt", "text word", "title/abstract", "title", "mesh terms", "publication type", "language", "mesh major topic",
}
LIMIT_TAGS = {"dp", "pdat", "edat", "crdt", "pt", "la", "publication type", "language", "filter", "sb"}


@dataclass
class Issue:
    level: str  # error | warning
    code: str
    message: str
    fragment: str = ""


@dataclass
class ValidationResult:
    ok: bool
    issues: list[Issue] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "issues": [i.__dict__ for i in self.issues]}


FOREIGN_SYNTAX = [
    (r"\bTITLE-ABS-KEY\s*\(|\bTITLE\s*\(|\bABS\s*\(|\bKEY\s*\(|\bAUTHKEY\s*\(", "scopus_field", "Scopus 字段语法（TITLE-ABS-KEY 等），PubMed 请用 [tiab] / [ti]"),
    (r"\b(?:W|PRE)/\d+\b", "scopus_proximity", "Scopus 邻近算符 W/n，PubMed 请用 \"词1 词2\"[tiab:~N]"),
    (r"\bNEAR/\d+\b", "wos_proximity", "WoS / Embase 邻近算符 NEAR/n，PubMed 请用 \"词1 词2\"[tiab:~N]"),
    (r"\b(?:TS|TI|AB|AK|SU|TKA|KY)\s*=", "field_equals", "WoS / 知网字段语法（TS= / SU= / TKA=），PubMed 请用 [tiab] / [Mesh]"),
    (r":(?:ti|ab|kw|de)(?:,(?:ti|ab|kw|de))*\b", "embase_field", "Embase.com 字段语法（:ti,ab），PubMed 请用 [tiab]"),
    (r"/exp\b|/de\b", "embase_emtree", "Embase Emtree 语法（/exp），PubMed 请用 [Mesh]"),
]

TERM_RE = re.compile(r'"[^"]*"(?:\[[^\]]+\])?|[^\s()"]+(?:\[[^\]]+\])?')


def validate_pubmed(query: str) -> ValidationResult:
    issues: list[Issue] = []
    q = query.strip()
    if not q:
        return ValidationResult(False, [Issue("error", "empty", "检索式为空")])

    for pat, code, msg in FOREIGN_SYNTAX:
        for m in re.finditer(pat, q, flags=re.I if code != "field_equals" else 0):
            issues.append(Issue("error", code, msg, m.group(0)))

    # 括号与引号配对
    depth = 0
    for ch in q:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            break
    if depth != 0:
        issues.append(Issue("error", "parens", "括号不配对"))
    if q.count('"') % 2:
        issues.append(Issue("error", "quotes", "引号不配对"))

    for m in TERM_RE.finditer(q):
        tok = m.group(0)
        tag_m = re.search(r"\[([^\]]+)\]$", tok)
        tag = tag_m.group(1).strip().lower() if tag_m else ""
        body = tok[: tag_m.start()] if tag_m else tok
        if body.upper() in ("AND", "OR", "NOT"):
            continue
        if "?" in body:
            issues.append(Issue("error", "wildcard_q", "PubMed 不支持 ? 通配符，拼写变体请用 OR 逐一列出", tok))
        if tag:
            prox = re.fullmatch(r"(ti|tiab):~\s*(\d+)", tag)
            if re.search(r":~", tag) and not prox:
                issues.append(Issue("error", "prox_field", "邻近检索仅支持 [ti] 与 [tiab]", tok))
            if prox:
                if not body.startswith('"'):
                    issues.append(Issue("error", "prox_quote", "邻近检索需要用引号括起多个词", tok))
                if "*" in body:
                    issues.append(Issue("error", "prox_trunc", "邻近检索的引号内不能含截词，否则邻近算符被忽略", tok))
            elif tag not in KNOWN_TAGS:
                issues.append(Issue("warning", "unknown_tag", f"未识别的字段标签 [{tag}]，请确认", tok))
            if tag in LIMIT_TAGS:
                issues.append(Issue("warning", "limit_in_query", "主检索式不建议包含年份 / 文献类型 / 语言限制（无限制原则），请在统计与过滤阶段使用", tok))
        for word in re.findall(r"[\w\-]+\*", body):
            if len(word) - 1 < 4:
                issues.append(Issue("error", "short_trunc", "截词 * 前至少保留 4 个字符", word))
        if re.fullmatch(r"(and|or|not)", body):
            issues.append(Issue("warning", "lower_bool", "布尔算符需大写（AND / OR / NOT），小写会被当作检索词", tok))

    # NOT 建议只在标题层面使用
    for m in re.finditer(r"\bNOT\s+(\(?[^()]*?\)?)(?=\s+(?:AND|OR|NOT)\b|\)|$)", q):
        seg = m.group(1)
        if seg and "[ti]" not in seg.lower():
            issues.append(Issue("warning", "not_scope", "NOT 建议只在标题层面（[ti]）使用，并记录排除理由", "NOT " + seg[:60]))

    return ValidationResult(not any(i.level == "error" for i in issues), issues)


# ------------------------------------------------------------------ 语义群组装


def _fmt_term(t: str, tag: str) -> str:
    t = t.strip().strip('"')
    if not t:
        return ""
    needs_quote = " " in t or "-" in t
    return f'"{t}"[{tag}]' if needs_quote else f"{t}[{tag}]"


def build_pubmed(groups: list[dict]) -> str:
    """groups: [{"concept": "...", "mesh": [...], "free_terms": [...]}] → 组内 OR、组间 AND。"""
    parts = []
    for g in groups:
        terms = [_fmt_term(m, "Mesh") for m in g.get("mesh", []) if m]
        terms += [_fmt_term(t, "tiab") for t in g.get("free_terms", []) if t]
        terms = [t for t in terms if t]
        if terms:
            parts.append("(" + " OR ".join(terms) + ")")
    return " AND ".join(parts)


# ------------------------------------------------------------------ 多库转换（规则版）


def _tokenize(q: str) -> list[str]:
    return re.findall(r'\(|\)|"[^"]*"(?:\[[^\]]+\])?|[^\s()"]+(?:\[[^\]]+\])?', q)


def convert(query: str, target: str) -> dict:
    """把 PubMed 检索式按字段规则转换为 scopus / wos / embase / cnki。

    MeSH → 目标库受控词无法自动一一对应，统一降级为题名/摘要/关键词字段并列入 unmapped，
    研究者必须在目标库中试检（规范 3.2、P07）。
    """
    target = target.lower()
    unmapped: list[str] = []
    out: list[str] = []
    for tok in _tokenize(query):
        if tok in ("(", ")") or tok.upper() in ("AND", "OR", "NOT"):
            out.append(tok.upper() if tok not in "()" else tok)
            continue
        m = re.match(r'^("?)(.*?)\1(?:\[([^\]]+)\])?$', tok)
        quoted, body, tag = (m.group(1), m.group(2), (m.group(3) or "").lower()) if m else ("", tok, "")
        prox = re.fullmatch(r"(ti|tiab):~\s*(\d+)", tag)
        words = body.split()
        phrase = f'"{body}"' if quoted or " " in body else body
        if tag.startswith("mesh") or tag in ("mh", "majr"):
            unmapped.append(f"MeSH：{body}")
            field = "kw"
        elif tag == "ti" or (prox and prox.group(1) == "ti"):
            field = "ti"
        else:
            field = "tiab"
        if target == "scopus":
            inner = f" W/{prox.group(2)} ".join(words) if prox else phrase
            fn = {"ti": "TITLE", "tiab": "TITLE-ABS-KEY", "kw": "TITLE-ABS-KEY"}[field]
            out.append(f"{fn}({inner})")
        elif target == "wos":
            inner = f" NEAR/{prox.group(2)} ".join(words) if prox else phrase
            fn = {"ti": "TI", "tiab": "TS", "kw": "TS"}[field]
            out.append(f"{fn}=({inner})")
        elif target == "embase":
            if field == "kw":
                out.append(f"'{body.lower()}'/exp")
            elif prox:
                out.append(f"({f' NEAR/{prox.group(2)} '.join(words)}):{'ti' if field == 'ti' else 'ti,ab'}")
            else:
                out.append(f"'{body}':{'ti' if field == 'ti' else 'ti,ab'}")
        elif target == "cnki":
            if prox:
                unmapped.append(f"邻近检索：{body}（知网不支持，已降级为短语）")
            fn = {"ti": "TI", "tiab": "TKA", "kw": "SU"}[field]
            out.append(f"{fn}='{body.rstrip('*')}'")
        else:
            raise ValueError(f"不支持的目标库：{target}")
    q = " ".join(out).replace("( ", "(").replace(" )", ")")
    notes = "规则转换结果，仅供起草。受控词（Emtree / 知网主题）需人工核对；请在目标库中试检并记录检索日期与命中数。"
    if target == "cnki":
        notes += " 知网请补充中文同义词与常见译名。"
    return {"database": target, "query": q, "unmapped": unmapped, "notes": notes}
