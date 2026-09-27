"""后台任务：基于数据库 jobs 表的轻量队列。

- 单机版（worker_mode=inline）：进程内线程执行，无需 Redis；
- 服务器 / 云端版（worker_mode=external）：独立 worker 进程（``reviewdesk worker``）轮询 jobs 表，
  可多副本水平扩展（PostgreSQL 下用 SELECT … FOR UPDATE SKIP LOCKED 抢占任务）。

长任务逐条入库、可断点续跑：处理函数会跳过已完成的条目，失败后重新入队即可继续（9.7）。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import SessionLocal
from .models import Job, User, utcnow

log = logging.getLogger("reviewdesk.jobs")

Handler = Callable[[Session, Job, User], dict]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(db: Session, kind: str, payload: dict[str, Any], *, project_id: str | None, user_id: str) -> Job:
    job = Job(kind=kind, payload=payload, project_id=project_id, user_id=user_id)
    db.add(job)
    db.commit()
    if get_settings().resolved_worker_mode == "inline":
        threading.Thread(target=run_job, args=(job.id,), daemon=True).start()
    return job


def set_progress(db: Session, job: Job, **progress) -> None:
    job.progress = {**(job.progress or {}), **progress}
    db.commit()


def run_job(job_id: int) -> None:
    from . import pipelines  # noqa: F401  注册处理函数

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status not in ("queued", "running"):
            return
        job.status, job.started_at, job.attempts = "running", utcnow(), job.attempts + 1
        db.commit()
        user = db.get(User, job.user_id)
        try:
            result = HANDLERS[job.kind](db, job, user)
            job.status, job.result, job.error = "done", result or {}, ""
        except Exception as e:  # noqa: BLE001
            db.rollback()
            job = db.get(Job, job_id)
            job.status = "failed"
            job.error = f"{e}\n{traceback.format_exc(limit=3)}"
            log.exception("job %s failed", job_id)
        job.finished_at = utcnow()
        db.commit()
    finally:
        db.close()


def claim_next(db: Session) -> int | None:
    q = select(Job).where(Job.status == "queued").order_by(Job.id).limit(1)
    if db.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    job = db.scalar(q)
    if not job:
        return None
    job.status = "running"
    db.commit()
    return job.id


def worker_loop(poll: float = 1.0, stop: threading.Event | None = None) -> None:
    log.info("worker started")
    while not (stop and stop.is_set()):
        db = SessionLocal()
        try:
            jid = claim_next(db)
        finally:
            db.close()
        if jid is None:
            time.sleep(poll)
            continue
        run_job(jid)


def recover_stale() -> None:
    """启动时把中断的 running 任务重新入队（断点续跑）。"""
    db = SessionLocal()
    try:
        for job in db.scalars(select(Job).where(Job.status == "running")):
            job.status = "queued"
        db.commit()
        if get_settings().resolved_worker_mode == "inline":
            for jid in db.scalars(select(Job.id).where(Job.status == "queued")):
                threading.Thread(target=run_job, args=(jid,), daemon=True).start()
    finally:
        db.close()
