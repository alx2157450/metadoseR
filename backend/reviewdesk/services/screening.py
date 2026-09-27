"""人机协同筛选（4.2）：校准指标、主动学习排序、排除抽查与 PRISMA 计数。"""
from __future__ import annotations

import math
import random
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Record, SearchRun
from .text import tokens

INCLUDE_LIKE = {"include", "uncertain"}


def calibration_metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs: [(人工标注, AI 预判)]，“应纳入”= include；uncertain 视为 AI 送人工（按纳入计）。

    返回敏感度、特异度、Cohen's κ（纳入 vs 排除二分类）与混淆矩阵。
    """
    tp = fn = fp = tn = 0
    for human, ai in pairs:
        h = human == "include"
        a = ai in INCLUDE_LIKE
        if h and a:
            tp += 1
        elif h and not a:
            fn += 1
        elif not h and a:
            fp += 1
        else:
            tn += 1
    n = tp + fn + fp + tn
    sens = tp / (tp + fn) if tp + fn else None
    spec = tn / (tn + fp) if tn + fp else None
    po = (tp + tn) / n if n else None
    if n:
        pe = ((tp + fn) * (tp + fp) + (tn + fp) * (tn + fn)) / (n * n)
        kappa = (po - pe) / (1 - pe) if pe != 1 else 1.0
    else:
        kappa = None
    return {"n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn, "sensitivity": sens, "specificity": spec, "agreement": po, "kappa": kappa}


def calibration_sample_size(total: int) -> int:
    """随机 100 条或总量的 10%，取较大者（且不超过总量）。"""
    return min(total, max(100, math.ceil(total * 0.1)))


def rank_records(records: list[Record]) -> dict[int, float]:
    """借鉴 ASReview 主动学习思路：用已作出的纳入/排除决定训练一个朴素贝叶斯打分器，

    把最可能相关的题录排在前面。没有足够标注时退化为 AI 等级排序。
    """
    level_prior = {"A": 4, "B": 3, "U": 2.5, "C": 2, "D": 0}
    labeled = [(r, (r.decision or r.calibration_label)) for r in records if (r.decision or r.calibration_label) in ("include", "exclude")]
    pos = [r for r, d in labeled if d == "include"]
    neg = [r for r, d in labeled if d == "exclude"]
    scores: dict[int, float] = {}
    if len(pos) >= 2 and len(neg) >= 2:
        cp, cn = Counter(), Counter()
        for r in pos:
            cp.update(set(tokens(f"{r.title} {r.abstract}")))
        for r in neg:
            cn.update(set(tokens(f"{r.title} {r.abstract}")))
        vocab = set(cp) | set(cn)
        prior = math.log(len(pos) / len(neg))
        for r in records:
            s = prior
            for t in set(tokens(f"{r.title} {r.abstract}")) & vocab:
                s += math.log((cp[t] + 1) / (len(pos) + 2)) - math.log((cn[t] + 1) / (len(neg) + 2))
            scores[r.id] = s + level_prior.get(r.ai_level or "", 1)
    else:
        for r in records:
            scores[r.id] = level_prior.get(r.ai_level or "", 1)
    return scores


def sample_excluded_for_audit(db: Session, project_id: str, ratio: float = 0.2, seed: int | None = None) -> list[int]:
    """AI 预判为“排除”且尚无人工决定的题录，至少随机抽查 20%。"""
    rows = list(
        db.scalars(
            select(Record).where(
                Record.project_id == project_id,
                Record.duplicate_of.is_(None),
                Record.ai_decision == "exclude",
                Record.decision.is_(None),
                Record.audit_sampled.is_(False),
            )
        )
    )
    k = math.ceil(len(rows) * ratio)
    picked = random.Random(seed).sample(rows, k) if k else []
    for r in picked:
        r.audit_sampled = True
    return [r.id for r in picked]


def prisma_counts(db: Session, project_id: str) -> dict:
    """PRISMA 2020 流程图数据；区分“人工排除”与“AI 预判后人工确认排除”（4.5）。"""
    runs = list(db.scalars(select(SearchRun).where(SearchRun.project_id == project_id)))
    db_runs = [r for r in runs if r.kind == "database"]
    other = [r for r in runs if r.kind != "database"]
    recs = list(db.scalars(select(Record).where(Record.project_id == project_id)))
    dups = sum(1 for r in recs if r.duplicate_of is not None)
    unique = [r for r in recs if r.duplicate_of is None]
    screened = [r for r in unique if r.decision is not None]
    excl = [r for r in screened if r.decision == "exclude"]
    incl = [r for r in screened if r.decision == "include"]
    return {
        "identified_databases": {r.database: r.hit_count for r in db_runs},
        "identified_database_total": sum(r.hit_count for r in db_runs),
        "identified_other": sum(r.imported_count for r in other),
        "records_imported": len(recs),
        "duplicates_removed": dups,
        "records_screened": len(unique),
        "screened_decided": len(screened),
        "awaiting_decision": len(unique) - len(screened),
        "excluded": len(excl),
        "excluded_by_source": dict(Counter(r.decision_source or "human" for r in excl)),
        "uncertain": sum(1 for r in screened if r.decision == "uncertain"),
        "included": len(incl),
        "included_by_source": dict(Counter(r.decision_source or "human" for r in incl)),
        "fulltext_parsed": sum(1 for r in incl if r.fulltext_status == "parsed"),
        "retracted_included": sum(1 for r in incl if r.retraction_status == "retracted"),
    }
