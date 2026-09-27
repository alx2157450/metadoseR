"""API 公共工具。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import inspect


def row(obj: Any, *, exclude: set[str] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 ORM 对象转成可 JSON 序列化的 dict。"""
    if obj is None:
        return {}
    out: dict[str, Any] = {}
    for attr in inspect(obj).mapper.column_attrs:
        k = attr.key
        if exclude and k in exclude:
            continue
        v = getattr(obj, k)
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    if extra:
        out.update(extra)
    return out


def rows(objs, **kw) -> list[dict[str, Any]]:
    return [row(o, **kw) for o in objs]


def get_or_404(db, model, key, project_id: str | None = None, what: str = "对象"):
    obj = db.get(model, key)
    if obj is None or (project_id is not None and getattr(obj, "project_id", project_id) != project_id):
        raise HTTPException(404, f"{what}不存在")
    return obj
