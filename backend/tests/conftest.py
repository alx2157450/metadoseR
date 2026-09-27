import os
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEWDESK_PROFILE", "local")
    monkeypatch.setenv("REVIEWDESK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REVIEWDESK_WORKER_MODE", "external")  # 测试中同步执行任务
    monkeypatch.setenv("REVIEWDESK_OFFLINE", "true")
    monkeypatch.setenv("REVIEWDESK_SINGLE_USER", "false")
    from reviewdesk import config, db, storage

    config.get_settings.cache_clear()
    db.reset_engine()
    storage._storage = None
    from fastapi.testclient import TestClient

    from reviewdesk.main import create_app

    with TestClient(create_app()) as c:
        yield c
    db.reset_engine()


def login(client, email="admin@local", password="admin"):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def run_jobs():
    from sqlalchemy import select

    from reviewdesk.db import SessionLocal
    from reviewdesk.jobs import run_job
    from reviewdesk.models import Job

    db = SessionLocal()
    ids = list(db.scalars(select(Job.id).where(Job.status == "queued")))
    db.close()
    for i in ids:
        run_job(i)
    return ids
