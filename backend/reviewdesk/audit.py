"""审计日志：追加写入 + 哈希链，任何篡改都会破坏链条（X4、9.7）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, utcnow


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _digest(prev: str, entry: dict[str, Any]) -> str:
    raw = prev + json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def log(
    db: Session,
    action: str,
    *,
    project_id: str | None = None,
    actor_id: str | None = None,
    object_type: str = "",
    object_id: str | Any = "",
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    last = db.scalar(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    prev = last.hash if last else ""
    at = utcnow()
    entry = {
        "project_id": project_id,
        "actor_id": actor_id,
        "action": action,
        "object_type": object_type,
        "object_id": str(object_id),
        "detail": detail or {},
        "at": at.isoformat(),
    }
    row = AuditLog(**{**entry, "at": at}, prev_hash=prev, hash=_digest(prev, entry))
    db.add(row)
    db.flush()
    return row


def verify_chain(db: Session) -> dict[str, Any]:
    prev = ""
    count = 0
    for row in db.scalars(select(AuditLog).order_by(AuditLog.id)):
        at = _utc(row.at).isoformat()
        entry = {
            "project_id": row.project_id,
            "actor_id": row.actor_id,
            "action": row.action,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "detail": row.detail or {},
            "at": at,
        }
        if row.prev_hash != prev or row.hash != _digest(prev, entry):
            return {"ok": False, "broken_at": row.id, "checked": count}
        prev = row.hash
        count += 1
    return {"ok": True, "checked": count}
