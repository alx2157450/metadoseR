"""账号、角色与平台管理（X1–X4、X6）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, audit
from ..config import get_settings
from ..db import get_db
from ..gateway import gateway
from ..gateway.prompts import all_prompts, get_prompt
from ..models import AuditLog, Job, ModelRun, Project, ProviderConfig, TaskRoute, User
from ..security import (
    create_token,
    encrypt_secret,
    get_current_user,
    hash_password,
    project_role,
    require_role,
    verify_password,
)
from .common import row, rows

router = APIRouter(prefix="/api")


@router.get("/meta")
def meta():
    s = get_settings()
    return {
        "name": "综述协作平台 ReviewDesk",
        "version": __version__,
        "profile": s.profile,
        "single_user": s.resolved_single_user,
        "offline": s.offline,
        "worker_mode": s.resolved_worker_mode,
        "storage": s.resolved_storage_backend,
        "database": s.resolved_database_url.split(":", 1)[0],
    }


class LoginIn(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "邮箱或密码错误")
    audit.log(db, "auth.login", actor_id=user.id)
    db.commit()
    return {"token": create_token(user), "user": row(user, exclude={"password_hash"})}


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return row(user, exclude={"password_hash"})


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/password")
def change_password(body: PasswordIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not verify_password(body.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    user.password_hash = hash_password(body.new_password)
    audit.log(db, "auth.password_changed", actor_id=user.id)
    db.commit()
    return {"ok": True}


class UserIn(BaseModel):
    email: str
    name: str
    password: str
    role: str = "student"


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_role("mentor", "admin"))):
    return rows(db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.created_at)), exclude={"password_hash"})


@router.post("/users")
def create_user(body: UserIn, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    if body.role not in ("student", "mentor", "admin"):
        raise HTTPException(400, "角色必须为 student / mentor / admin")
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "该邮箱已存在")
    u = User(tenant_id=admin.tenant_id, email=email, name=body.name, password_hash=hash_password(body.password), role=body.role)
    db.add(u)
    db.flush()
    audit.log(db, "user.create", actor_id=admin.id, object_type="user", object_id=u.id, detail={"role": body.role})
    db.commit()
    return row(u, exclude={"password_hash"})


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    name: str | None = None


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    u = db.get(User, user_id)
    if not u or u.tenant_id != admin.tenant_id:
        raise HTTPException(404, "用户不存在")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(u, k, v)
    audit.log(db, "user.update", actor_id=admin.id, object_type="user", object_id=u.id, detail=body.model_dump(exclude_none=True))
    db.commit()
    return row(u, exclude={"password_hash"})


# ------------------------------------------------------------------ Prompt 注册表（X3）


@router.get("/prompts")
def prompts(_: User = Depends(get_current_user)):
    return [
        {"id": p.id, "name": p.name, "version": p.version, "ref": p.ref, "stage": p.stage, "min_level": p.min_level, "output": p.output, "temperature": p.temperature, "params": p.params, "changelog": p.changelog}
        for p in all_prompts()
    ]


@router.get("/prompts/{pid}")
def prompt_detail(pid: str, _: User = Depends(get_current_user)):
    try:
        p = get_prompt(pid)
    except KeyError:
        raise HTTPException(404, "Prompt 不存在")
    return {**p.__dict__, "ref": p.ref}


# ------------------------------------------------------------------ 模型网关配置（X2）


class ProviderIn(BaseModel):
    name: str
    kind: str = "openai_compatible"
    base_url: str = ""
    api_key: str | None = None
    default_model: str = ""
    is_local: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    context_tokens: int = 32000


def _provider_out(p: ProviderConfig) -> dict:
    return row(p, exclude={"api_key_enc"}, extra={"has_key": bool(p.api_key_enc)})


@router.get("/admin/providers")
def list_providers(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    configured = [_provider_out(p) for p in db.scalars(select(ProviderConfig).where(ProviderConfig.tenant_id == user.tenant_id))]
    builtin = [
        {"name": p.name, "kind": p.kind, "base_url": p.base_url, "default_model": p.model, "is_local": p.is_local, "builtin": True}
        for p in gateway.builtin_providers().values()
    ]
    return {"configured": configured, "builtin": builtin, "default": get_settings().default_provider}


@router.post("/admin/providers")
def upsert_provider(body: ProviderIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    if body.kind not in ("openai_compatible", "rules"):
        raise HTTPException(400, "kind 必须为 openai_compatible 或 rules")
    p = db.scalar(select(ProviderConfig).where(ProviderConfig.tenant_id == user.tenant_id, ProviderConfig.name == body.name))
    if not p:
        p = ProviderConfig(tenant_id=user.tenant_id, name=body.name)
        db.add(p)
    for k, v in body.model_dump(exclude={"api_key"}).items():
        setattr(p, k, v)
    if body.api_key is not None:
        p.api_key_enc = encrypt_secret(body.api_key)
    audit.log(db, "gateway.provider_upsert", actor_id=user.id, object_type="provider", object_id=body.name, detail={"base_url": body.base_url, "is_local": body.is_local})
    db.commit()
    return _provider_out(p)


@router.delete("/admin/providers/{name}")
def delete_provider(name: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    p = db.scalar(select(ProviderConfig).where(ProviderConfig.tenant_id == user.tenant_id, ProviderConfig.name == name))
    if p:
        db.delete(p)
        audit.log(db, "gateway.provider_delete", actor_id=user.id, object_type="provider", object_id=name)
        db.commit()
    return {"ok": True}


@router.post("/admin/providers/{name}/test")
def test_provider(name: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    providers = gateway.list_providers(db, user.tenant_id)
    if name not in providers:
        raise HTTPException(404, "提供方不存在")
    p = providers[name]
    if p.kind == "rules":
        return {"ok": True, "reply": "规则引擎可用"}
    text, tin, tout = gateway._call_openai(p, p.model, "You are a connectivity test.", "Reply with the single word: OK", 0)
    return {"ok": True, "reply": text[:200], "tokens_in": tin, "tokens_out": tout}


class RouteIn(BaseModel):
    task: str
    provider: str
    model: str = ""
    local_fallback: str = ""


@router.get("/admin/routes")
def list_routes(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    return rows(db.scalars(select(TaskRoute).where(TaskRoute.tenant_id == user.tenant_id).order_by(TaskRoute.task)))


@router.put("/admin/routes")
def upsert_route(body: RouteIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    r = db.scalar(select(TaskRoute).where(TaskRoute.tenant_id == user.tenant_id, TaskRoute.task == body.task))
    if not r:
        r = TaskRoute(tenant_id=user.tenant_id, task=body.task)
        db.add(r)
    r.provider, r.model, r.local_fallback = body.provider, body.model, body.local_fallback
    audit.log(db, "gateway.route_upsert", actor_id=user.id, object_type="route", object_id=body.task, detail=body.model_dump())
    db.commit()
    return row(r)


@router.delete("/admin/routes/{task}")
def delete_route(task: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    r = db.scalar(select(TaskRoute).where(TaskRoute.tenant_id == user.tenant_id, TaskRoute.task == task))
    if r:
        db.delete(r)
        db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 用量、审计与任务（X4、X6）


@router.get("/admin/usage")
def usage(db: Session = Depends(get_db), user: User = Depends(require_role("mentor", "admin"))):
    q = (
        select(ModelRun.project_id, ModelRun.user_id, ModelRun.provider, ModelRun.model, func.count(), func.sum(ModelRun.tokens_in), func.sum(ModelRun.tokens_out), func.sum(ModelRun.cost))
        .join(Project, Project.id == ModelRun.project_id)
        .where(Project.tenant_id == user.tenant_id)
        .group_by(ModelRun.project_id, ModelRun.user_id, ModelRun.provider, ModelRun.model)
    )
    return [
        {"project_id": a, "user_id": b, "provider": c, "model": d, "runs": e, "tokens_in": f or 0, "tokens_out": g or 0, "cost": round(h or 0, 4)}
        for a, b, c, d, e, f, g, h in db.execute(q)
    ]


@router.get("/admin/audit")
def audit_list(project_id: str | None = None, limit: int = 200, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 2000))
    if project_id:
        p = db.get(Project, project_id)
        if not p or project_role(db, p, user) is None:
            raise HTTPException(403, "无权访问")
        q = q.where(AuditLog.project_id == project_id)
    elif user.role != "admin":
        raise HTTPException(403, "只有管理员可以查看全局审计日志")
    return rows(db.scalars(q))


@router.get("/admin/audit/verify")
def audit_verify(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    return audit.verify_chain(db)


@router.get("/jobs/{job_id}")
def job_status(job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.project_id:
        p = db.get(Project, job.project_id)
        if project_role(db, p, user) is None:
            raise HTTPException(403, "无权访问")
    elif job.user_id != user.id:
        raise HTTPException(403, "无权访问")
    return row(job)
