"""离线规则引擎（provider kind = "rules"）。

在没有配置任何大模型时（完全离线的单机版、演示、自动化测试），平台仍可跑通全流程：
分类 / 抽取 / 核查类任务用可解释的启发式规则给出“建议”，生成类任务返回提示，
说明需要配置云端 API 或本地模型。所有输出与真实模型一样经过 schema 校验并记录日志。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..services import query_syntax
from ..services.text import citekeys_in, numbers_in, overlap, split_sentences, strip_citations, tokens

ANIMAL = re.compile(r"\b(mice|mouse|rats?|murine|zebrafish|rabbits?|pigs?|db/db|ob/ob|in vivo)\b", re.I)
CELL = re.compile(r"\b(cells?|cardiomyocytes?|in vitro|cell line|h9c2|hl-1)\b", re.I)
TRIAL = re.compile(r"\b(randomi[sz]ed|placebo|trial)\b", re.I)
HUMAN = re.compile(r"\b(patients?|participants?|subjects|cohort|humans?|adults?|children)\b", re.I)
OVERGEN = re.compile(r"\b(prove[sd]?|always|never|all|undoubtedly|clearly|definitely|must)\b|证明了|一定|毫无疑问|所有|必然", re.I)
TRANSITION = re.compile(r"^(moreover|furthermore|additionally|in addition|notably|interestingly)\b|^(此外|另外|值得注意的是|同时)", re.I)
CAUSAL = re.compile(r"\b(causes?|leads? to|results? in|drives?)\b|导致|引起", re.I)


def _guess_type(text: str) -> str:
    if TRIAL.search(text):
        return "clinical_trial"
    if ANIMAL.search(text):
        return "animal"
    if CELL.search(text):
        return "cell"
    if HUMAN.search(text):
        return "human"
    return ""


def _truncate_words(s: str, n: int) -> str:
    w = s.split()
    return s if len(w) <= n else " ".join(w[:n])


def run(task: str, params: dict[str, Any], ctx: dict[str, Any]) -> str:
    fn = globals().get(f"_{task.lower()}")
    if fn is None:
        return (
            f"【离线规则引擎】{task} 属于生成 / 分析类任务，规则引擎无法完成。"
            "请在“模型网关配置”中接入云端 API（如 DeepSeek）或本地模型（Ollama / vLLM），"
            "或使用“导出 Prompt”复制到网页版 AI 后把结果粘贴回平台。"
        )
    return json.dumps(fn(params, ctx), ensure_ascii=False) if task not in _TEXT_TASKS else fn(params, ctx)


_TEXT_TASKS = {"P12", "P21"}


def _p06(params, ctx):
    groups = [
        {"concept": c.get("name", ""), "mesh": c.get("mesh", []), "free_terms": c.get("terms", []) or [c.get("name", "")], "note": ""}
        for c in ctx.get("concepts", [])
    ]
    return {
        "query": query_syntax.build_pubmed(groups),
        "groups": groups,
        "risks": ["规则组装：未自动扩展同义词与缩写，请补充自由词并在 MeSH Database 核实主题词"],
    }


def _p07(params, ctx):
    return query_syntax.convert(params["pubmed_query"], ctx.get("target", "scopus"))


def _p10(params, ctx):
    include_terms = set(tokens(" ".join(ctx.get("include_terms", []))))
    exclude_terms = set(tokens(" ".join(ctx.get("exclude_terms", []))))
    out = []
    for r in ctx.get("records", []):
        abstract = r.get("abstract") or ""
        text = f"{r.get('title', '')}. {abstract}"
        if len(abstract) < 50:
            out.append({"id": r["id"], "level": "U", "decision": "uncertain", "reason": "摘要缺失或过短，需人工判断", "evidence_quote": "", "key_finding": "", "sections": []})
            continue
        toks = set(tokens(text))
        cover = len(include_terms & toks) / max(1, len(include_terms))
        title_cover = len(include_terms & set(tokens(r.get("title", "")))) / max(1, len(include_terms))
        excl = len(exclude_terms & toks) / max(1, len(exclude_terms)) if exclude_terms else 0
        sents = split_sentences(abstract) or [abstract]
        best = max(sents, key=lambda s: len(include_terms & set(tokens(s))))
        quote = _truncate_words(best, 30)
        if cover >= 0.6 or title_cover >= 0.5:
            level, decision = "A", "include"
        elif cover >= 0.35:
            level, decision = "B", "include"
        elif cover > 0.1:
            level, decision = "C", "uncertain"
        else:
            level, decision = "D", "exclude"
        if excl > 0.5 and level in ("B", "C"):
            decision = "uncertain"
        reason = f"纳入标准词项覆盖 {cover:.0%}（标题 {title_cover:.0%}）" + (f"；命中排除标准词项 {excl:.0%}" if excl else "")
        out.append({"id": r["id"], "level": level, "decision": decision, "reason": reason, "evidence_quote": quote, "key_finding": "" if level == "D" else strip_citations(best)[:200], "sections": []})
    return out


def _p11(params, ctx):
    terms = set(tokens(" ".join(ctx.get("terms", []))))
    cards = []
    for p in ctx.get("paragraphs", []):
        sec = (p.get("section") or "").lower()
        if any(k in sec for k in ("method", "reference", "acknowledg", "funding", "conflict")):
            continue
        for s in split_sentences(p.get("text", "")):
            toks = set(tokens(s))
            hit = len(terms & toks)
            has_num = bool(re.search(r"\d", s)) and bool(re.search(r"(p\s*[<=]|%|fold|increase|decrease|reduc|elevat|higher|lower|significant)", s, re.I))
            if hit >= 2 or (hit >= 1 and has_num):
                cards.append(((hit + has_num), {
                    "claim": s,
                    "evidence_type": "review" if ctx.get("is_review") else _guess_type(p.get("text", "")),
                    "model_or_population": "",
                    "quantitative": "; ".join(re.findall(r"[^,;]*\d+(?:\.\d+)?\s*(?:%|-fold|fold)[^,;]*", s))[:300],
                    "quote": s,
                    "location": p.get("id", ""),
                    "limitations": "",
                    "outline_nodes": [],
                    "secondary": bool(ctx.get("is_review")),
                }))
    cards.sort(key=lambda x: -x[0])
    return [c for _, c in cards[: ctx.get("max_cards", 8)]]


def _p15(params, ctx):
    comments = []
    animal_cards = any(c.get("evidence_type") in ("animal", "cell") for c in ctx.get("cards", []))
    for s in ctx.get("sentences", []):
        n, text = s["n"], s["text"]
        plain = strip_citations(text)
        if not citekeys_in(text) and (numbers_in(plain) or CAUSAL.search(plain)):
            comments.append({"sentence": n, "category": "证据", "issue": "该句包含事实性陈述，但没有引用支持", "direction": "这一结论来自哪张证据卡？如果是你的推断，能否用措辞表明？"})
        if OVERGEN.search(plain):
            comments.append({"sentence": n, "category": "过度概括", "issue": "使用了绝对化表述", "direction": "所引研究的样本和模型是否足以支持这种确定程度？"})
        if animal_cards and HUMAN.search(plain) and not ANIMAL.search(plain):
            comments.append({"sentence": n, "category": "过度概括", "issue": "证据卡含动物 / 细胞实验，句子却以人群表述", "direction": "是否需要说明证据来自哪种模型？"})
        if CAUSAL.search(plain) and not TRIAL.search(plain):
            comments.append({"sentence": n, "category": "因果", "issue": "使用了因果措辞", "direction": "所引证据是干预实验还是相关性观察？措辞是否匹配？"})
        if TRANSITION.search(plain):
            comments.append({"sentence": n, "category": "衔接", "issue": "以套话式过渡词开头", "direction": "这里与上一句的真实逻辑关系是什么（因果、转折、递进）？"})
        if len(plain.split()) > 45 or len(re.findall(r"[一-鿿]", plain)) > 90:
            comments.append({"sentence": n, "category": "用词", "issue": "句子过长", "direction": "能否拆成两句，让每句只承载一个信息？"})
    top3 = [f"第 {c['sentence']} 句：{c['issue']}" for c in comments[:3]]
    return {"comments": comments, "top3": top3}


def _p16(params, ctx):
    cards = ctx.get("cards", [])[:5]
    points = [{"text": _truncate_words(strip_citations(c.get("claim", "")), 15), "card_ids": [c.get("card_id")]} for c in cards]
    return {
        "points": points,
        "order": "现象 → 机制 → 矛盾 → 意义",
        "rationale": "规则引擎按证据卡顺序列出要点，请按论证逻辑自行调整",
        "author_judgment": "比较不同模型 / 人群间结果差异的原因",
    }


def _p19(params, ctx):
    sources: dict[str, dict] = ctx.get("sources", {})
    claims = []
    for s in ctx.get("sentences", []):
        keys = citekeys_in(s["text"])
        plain = strip_citations(s["text"])
        if not keys:
            claims.append({"sentence": s["n"], "claim": plain, "citekey": "", "verdict": "na", "quote": "", "location": "", "note": "未附引用，按作者观点处理", "suggestion": "如属事实性陈述请补充引用；属作者观点请在措辞上体现"})
            continue
        for key in keys:
            src = sources.get(key)
            if not src or not src.get("paragraphs"):
                claims.append({"sentence": s["n"], "claim": plain, "citekey": key, "verdict": "undetermined", "quote": "未找到", "location": "", "note": "文献库中没有该文献的原文或摘要", "suggestion": "获取全文后重新核查"})
                continue
            best, best_score, loc = "", 0.0, ""
            for p in src["paragraphs"]:
                for sent in split_sentences(p["text"]):
                    sc = overlap(plain, sent)
                    if sc > best_score:
                        best, best_score, loc = sent, sc, p.get("id", "")
            nums_ok = numbers_in(plain) <= numbers_in(best) if numbers_in(plain) else True
            if best_score >= 0.5 and nums_ok:
                verdict = "strong"
            elif best_score >= 0.25:
                verdict = "partial"
            else:
                verdict = "none"
            if src.get("abstract_only") and verdict == "strong":
                verdict = "undetermined"
            note = f"词项覆盖 {best_score:.0%}" + ("" if nums_ok else "；数值与原文不一致") + ("；仅摘要" if src.get("abstract_only") else "")
            if src.get("is_review"):
                note += "；所引为综述，如为原始研究发现请改引原始研究"
            claims.append({"sentence": s["n"], "claim": plain, "citekey": key, "verdict": verdict, "quote": best if verdict != "none" else "未找到", "location": loc, "note": note, "suggestion": {"strong": "", "partial": "回到原文核对数值、限定条件与研究对象", "none": "修正或删除该表述；不要另找文献来支撑", "undetermined": "获取全文后重新核查"}[verdict]})
    summary: dict[str, int] = {}
    for c in claims:
        summary[c["verdict"]] = summary.get(c["verdict"], 0) + 1
    return {"claims": claims, "summary": summary}


def _lang(params, ctx):
    sugg = []
    for s in ctx.get("sentences", []):
        text = s["text"]
        for pat, rep, why in [
            (r"  +", " ", "多余空格"),
            (r"\bIn addition, ", "", "删除套话式过渡词，改用明确的逻辑连接"),
            (r"\bvery important\b", "critical", "避免口语化强调"),
            (r"\ba lot of\b", "numerous", "学术语体"),
            (r"，，", "，", "重复标点"),
        ]:
            m = re.search(pat, text)
            if m:
                sugg.append({"sentence": s["n"], "original": m.group(0), "revised": rep, "reason": why})
    return {"suggestions": sugg}


def _p12(params, ctx):
    lines = ["## 证据地图（规则统计）", ""]
    for node in ctx.get("nodes", []):
        cnt, a = node["count"], node["a_count"]
        flag = " ⚠ 证据不足" if cnt < ctx.get("min_cards", 8) or a == 0 else ""
        lines.append(f"- {node['id']} {node['title']}（证据卡 {cnt} 张，其中 A 级文献 {a} 篇）{flag}")
    if ctx.get("unassigned"):
        lines += ["", f"未映射到任何节点的证据卡：{ctx['unassigned']} 张（请人工归类或判断是否为遗漏主题）"]
    lines += ["", "> 离线规则引擎只做计数与缺口提示；矛盾证据识别与结构建议需接入大模型。"]
    return "\n".join(lines)


def _p21(params, ctx):
    u = ctx.get("usage", {})
    models = "、".join(u.get("models", [])) or "（无）"
    tasks = "、".join(u.get("task_names", [])) or "（无）"
    zh = (
        f"在本文准备过程中，作者使用了综述协作平台（调用模型：{models}）辅助完成{tasks}。"
        f"平台共记录 AI 调用 {u.get('runs', 0)} 次；题录筛选决定中经人工确认的比例为 {u.get('screen_human_ratio', 0):.0%}，"
        f"证据卡人工核对比例为 {u.get('card_verified_ratio', 0):.0%}，正文人工撰写字符占比为 {u.get('human_char_ratio', 0):.0%}，"
        f"采纳的 AI 语言建议 {u.get('accepted_language', 0)} 条。所有 AI 输出均经作者逐条核查；文章的论点、结构与正文由作者撰写，作者对全部内容负责。"
    )
    en = (
        f"During the preparation of this work, the authors used ReviewDesk ({models}) to assist with {', '.join(u.get('task_names_en', [])) or 'none'}. "
        f"All AI outputs were reviewed and verified by the authors ({u.get('runs', 0)} logged AI calls; "
        f"{u.get('human_char_ratio', 0):.0%} of the main text characters were typed by the authors). "
        "The arguments, structure and text were written by the authors, who take full responsibility for the content of this publication."
    )
    return f"**中文**\n\n{zh}\n\n**English**\n\n{en}"
