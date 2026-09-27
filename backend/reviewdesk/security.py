"""身份认证、角色与项目级权限（X1），以及 API 密钥加密存储（X2）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Project, ProjectMember, User

_ITER = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def create_token(user: User) -> str:
    s = get_settings()
    payload = {
        "sub": user.id,
        "tid": user.tenant_id,
        "role": user.role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=s.token_ttl_hours),
    }
    return jwt.encode(payload, s.secret_key, algorithm="HS256")


def _fernet() -> Fernet:
    key = hashlib.sha256(("reviewdesk-keys:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        return ""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    s = get_settings()
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    if token:
        try:
            payload = jwt.decode(token, s.secret_key, algorithms=["HS256"])
        except jwt.PyJWTError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
        user = db.get(User, payload["sub"])
        if user and user.active:
            return user
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    if s.resolved_single_user:
        # 单机版：单用户模式，自动使用内置管理员账号
        user = db.scalar(select(User).where(User.email == s.bootstrap_admin_email))
        if user:
            return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要登录")


def require_role(*roles: str):
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles and user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行此操作")
        return user

    return dep


def project_role(db: Session, project: Project, user: User) -> str | None:
    """返回用户在项目中的角色：owner/student/mentor/admin；无权限返回 None。"""
    if user.tenant_id != project.tenant_id:
        return None
    if user.role == "admin":
        return "admin"
    m = db.scalar(
        select(ProjectMember).where(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)
    )
    if m:
        return m.role
    return "student" if project.owner_id == user.id else None


class ProjectCtx:
    def __init__(self, project: Project, user: User, role: str, db: Session):
        self.project = project
        self.user = user
        self.role = role
        self.db = db

    @property
    def is_mentor(self) -> bool:
        # 单机单用户模式下，管理员同时承担导师角色（导出项目包后也可交导师在服务器版审批）
        return self.role in ("mentor", "admin")

    def require_mentor(self) -> None:
        if not self.is_mentor:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "只有导师可以执行此操作")

    def require_writer(self) -> None:
        if self.role not in ("student", "admin"):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "只有项目学生（作者）可以执行此操作")


def get_project_ctx(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> ProjectCtx:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    role = project_role(db, project, user)
    if role is None:
        raise HTTPException(403, "无权访问该项目")
    return ProjectCtx(project, user, role, db)
