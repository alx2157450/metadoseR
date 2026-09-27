"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import __version__, jobs
from .api import auth, evidence, library, projects, quality, search, writing
from .config import get_settings
from .db import SessionLocal, init_db
from .models import Tenant, User
from .security import hash_password

log = logging.getLogger("reviewdesk")


def bootstrap() -> None:
    """首次启动：建表、创建默认租户与管理员账号。"""
    s = get_settings()
    init_db()
    db = SessionLocal()
    try:
        if not db.scalar(select(User).limit(1)):
            t = Tenant(name="default")
            db.add(t)
            db.flush()
            db.add(User(tenant_id=t.id, email=s.bootstrap_admin_email, name="管理员", password_hash=hash_password(s.bootstrap_admin_password), role="admin"))
            db.commit()
            if s.profile != "local" and s.bootstrap_admin_password == "admin":
                log.warning("已使用默认管理员密码创建账号，请立即修改（REVIEWDESK_BOOTSTRAP_ADMIN_PASSWORD）")
        if s.resolved_single_user:
            log.warning("单用户免登录模式：请只在本机访问（监听 127.0.0.1），不要把端口暴露到网络")
        if s.profile != "local" and s.secret_key.startswith("change-me-in-production"):
            log.warning("REVIEWDESK_SECRET_KEY 仍为默认值，服务器 / 云端部署必须修改")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    jobs.recover_stale()
    yield


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="综述协作平台 ReviewDesk", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])

    for r in (auth.router, projects.router, search.router, library.router, evidence.router, writing.router, quality.router):
        app.include_router(r)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "profile": s.profile}

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    static = s.static_dir or Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = static / path
            if path and f.is_file():
                return FileResponse(f)
            return FileResponse(static / "index.html")

    return app


app = create_app()
