"""模块 A：项目与命题管理（阶段一），以及人工闸门 G0–G5 与通用 AI 任务入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..gateway import gateway
from ..gateway.prompts import LEVELS, all_prompts, get_prompt, level_ge
from ..models import Charter, GateApproval, Job, ModelRun, Outline, Project, ProjectMember, User, utcnow
from ..pipelines import approved_charter, latest_charter, outline_text
from ..security import ProjectCtx, get_current_user, get_project_ctx, project_role
from .common import row, rows

router = APIRouter(prefix="/api/projects")

GATES = {
    "G0": "导师批准任务书",
    "G1": "检索式与检索日志抽查",
    "G2": "确认纳入库与 PRISMA",
    "G3": "批准大纲",
    "G4": "核查结果抽查",
    "G5": "导师终审",
}

CHARTER_FIELDS = [
    "mentor_topic", "background", "discipline", "working_title", "review_type", "core_questions", "include", "exclude",
    "time_language", "target_journals", "scale", "seed_ids", "milestones", "concepts", "lab_direction", "deadline",
]


class ProjectIn(BaseModel):
    name: str
    mentor_topic: str = ""
    mentor_email: str | None = None
    student_email: str | None = None


def _project_out(db: Session, p: Project, user: User) -> dict:
    ch = latest_charter(db, p.id)
    ap = approved_charter(db, p.id)
    members = []
    for m in p.members:
        u = db.get(User, m.user_id)
        members.append({"user_id": m.user_id, "role": m.role, "name": u.name if u else "", "email": u.email if u else ""})
    owner = db.get(User, p.owner_id)
    return row(
        p,
        extra={
            "my_role": project_role(db, p, user),
            "owner": {"id": owner.id, "name": owner.name, "email": owner.email} if owner else None,
            "members": members,
            "charter_version": ch.version if ch else None,
            "charter_status": ch.status if ch else None,
            "approved_charter_version": ap.version if ap else None,
            "mentor_topic": (ch.data.get("mentor_topic") if ch else "") or "",
        },
    )


@router.get("")
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = select(Project).where(Project.tenant_id == user.tenant_id).order_by(Project.created_at.desc())
    return [_project_out(db, p, user) for p in db.scalars(q) if project_role(db, p, user)]


def _user_by_email(db: Session, email: str, tenant_id: str) -> User:
    u = db.scalar(select(User).where(User.email == email.strip().lower(), User.tenant_id == tenant_id))
    if not u:
        raise HTTPException(404, f"用户 {email} 不存在，请先由管理员创建账号")
    return u


@router.post("")
def create_project(body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owner = user
    if user.role == "mentor" and body.student_email:
        owner = _user_by_email(db, body.student_email, user.tenant_id)
    p = Project(tenant_id=user.tenant_id, name=body.name, owner_id=owner.id)
    db.add(p)
    db.flush()
    if user.role == "mentor":
        db.add(ProjectMember(project_id=p.id, user_id=user.id, role="mentor"))
    elif body.mentor_email:
        mentor = _user_by_email(db, body.mentor_email, user.tenant_id)
        db.add(ProjectMember(project_id=p.id, user_id=mentor.id, role="mentor"))
    # A1 命题录入：第一版任务书以导师原题起步
    db.add(Charter(project_id=p.id, version=1, data={"mentor_topic": body.mentor_topic or body.name, "review_type": "narrative"}, created_by=user.id))
    audit.log(db, "project.create", project_id=p.id, actor_id=user.id, detail={"name": body.name})
    db.commit()
    db.refresh(p)
    return _project_out(db, p, user)


@router.get("/{project_id}")
def get_project(ctx: ProjectCtx = Depends(get_project_ctx)):
    return _project_out(ctx.db, ctx.project, ctx.user)


class MemberIn(BaseModel):
    email: str
    role: str = "mentor"


@router.post("/{project_id}/members")
def add_member(body: MemberIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    if ctx.role not in ("student", "mentor", "admin"):
        raise HTTPException(403, "无权添加成员")
    if body.role not in ("student", "mentor"):
        raise HTTPException(400, "角色必须为 student 或 mentor")
    if body.role == "mentor" and not ctx.is_mentor and any(m.role == "mentor" for m in ctx.project.members):
        raise HTTPException(403, "项目已有导师；新增导师需由导师或管理员操作")
    u = _user_by_email(ctx.db, body.email, ctx.project.tenant_id)
    m = ctx.db.scalar(select(ProjectMember).where(ProjectMember.project_id == ctx.project.id, ProjectMember.user_id == u.id))
    if m:
        m.role = body.role
    else:
        ctx.db.add(ProjectMember(project_id=ctx.project.id, user_id=u.id, role=body.role))
    audit.log(ctx.db, "project.member", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="user", object_id=u.id, detail={"role": body.role})
    ctx.db.commit()
    ctx.db.refresh(ctx.project)
    return _project_out(ctx.db, ctx.project, ctx.user)


class SettingsIn(BaseModel):
    ai_level_max: str | None = None
    data_local_only: bool | None = None
    budget_limit: float | None = Field(default=None, ge=0)
    review_type: str | None = None
    name: str | None = None


@router.put("/{project_id}/settings")
def update_settings(body: SettingsIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """AI 介入级别与“数据不外发”由导师设定，学生不能自行调高（1.4、A7）。"""
    changes = body.model_dump(exclude_none=True)
    mentor_only = {"ai_level_max", "data_local_only", "budget_limit"}
    if mentor_only & changes.keys():
        ctx.require_mentor()
    if "ai_level_max" in changes and changes["ai_level_max"] not in LEVELS:
        raise HTTPException(400, "AI 介入级别必须为 L0–L3")
    for k, v in changes.items():
        setattr(ctx.project, k, v)
    audit.log(ctx.db, "project.settings", project_id=ctx.project.id, actor_id=ctx.user.id, detail=changes)
    ctx.db.commit()
    return _project_out(ctx.db, ctx.project, ctx.user)


# ------------------------------------------------------------------ 《综述任务书》与 G0（A6）


@router.get("/{project_id}/charter")
def list_charters(ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(Charter).where(Charter.project_id == ctx.project.id).order_by(Charter.version.desc())
    return rows(ctx.db.scalars(q))


class CharterIn(BaseModel):
    data: dict[str, Any]


def _clean_charter(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data[k] for k in CHARTER_FIELDS if k in data}
    for k in ("core_questions", "include", "exclude", "seed_ids"):
        v = out.get(k)
        if isinstance(v, str):
            out[k] = [x.strip() for x in v.splitlines() if x.strip()]
    terms: list[str] = []
    for c in out.get("concepts", []) or []:
        terms += [c.get("name", "")] + list(c.get("terms", []) or [])
    out["concepts_terms"] = [t for t in terms if t]
    return out


@router.post("/{project_id}/charter")
def save_charter(body: CharterIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """保存任务书。已提交或已批准的版本锁定，修改生成新版本并需重新走 G0。"""
    if ctx.role not in ("student", "mentor", "admin"):
        raise HTTPException(403, "无权修改任务书")
    cur = latest_charter(ctx.db, ctx.project.id)
    data = _clean_charter(body.data)
    if cur and cur.status == "draft":
        cur.data = data
        c = cur
    else:
        c = Charter(project_id=ctx.project.id, version=(cur.version + 1) if cur else 1, data=data, created_by=ctx.user.id)
        ctx.db.add(c)
    if data.get("review_type"):
        ctx.project.review_type = data["review_type"]
    ctx.db.flush()
    audit.log(ctx.db, "charter.save", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="charter", object_id=c.version)
    ctx.db.commit()
    return row(c)


class DecisionIn(BaseModel):
    decision: str = "approved"  # approved | rejected
    comment: str = ""


@router.post("/{project_id}/charter/{version}/submit")
def submit_charter(version: int, ctx: ProjectCtx = Depends(get_project_ctx)):
    c = ctx.db.scalar(select(Charter).where(Charter.project_id == ctx.project.id, Charter.version == version))
    if not c:
        raise HTTPException(404, "任务书版本不存在")
    if c.status != "draft":
        raise HTTPException(409, "只有草稿可以提交")
    missing = [k for k in ("mentor_topic", "core_questions", "include", "exclude") if not c.data.get(k)]
    if missing:
        raise HTTPException(422, f"任务书缺少必填字段：{', '.join(missing)}")
    c.status = "submitted"
    ctx.db.add(GateApproval(project_id=ctx.project.id, gate="G0", object_version=f"charter@v{version}", decision="submitted", by=ctx.user.id))
    audit.log(ctx.db, "gate.submit", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="G0", object_id=f"charter@v{version}")
    ctx.db.commit()
    return row(c)


@router.post("/{project_id}/charter/{version}/decide")
def decide_charter(version: int, body: DecisionIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    ctx.require_mentor()
    c = ctx.db.scalar(select(Charter).where(Charter.project_id == ctx.project.id, Charter.version == version))
    if not c:
        raise HTTPException(404, "任务书版本不存在")
    if c.status not in ("submitted", "draft"):
        raise HTTPException(409, "该版本已处理")
    if body.decision not in ("approved", "rejected"):
        raise HTTPException(400, "decision 必须为 approved 或 rejected")
    c.status = body.decision
    if body.decision == "approved":
        c.approved_by, c.approved_at = ctx.user.id, utcnow()
        c.data = {**c.data, "ai_level_max": ctx.project.ai_level_max, "data_local_only": ctx.project.data_local_only}
    ctx.db.add(GateApproval(project_id=ctx.project.id, gate="G0", object_version=f"charter@v{version}", decision=body.decision, by=ctx.user.id, comment=body.comment))
    audit.log(ctx.db, "gate.decide", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="G0", object_id=f"charter@v{version}", detail={"decision": body.decision, "comment": body.comment})
    ctx.db.commit()
    return row(c)


# ------------------------------------------------------------------ 闸门 G1–G5


@router.get("/{project_id}/gates")
def gates(ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(GateApproval).where(GateApproval.project_id == ctx.project.id).order_by(GateApproval.at)
    hist = rows(ctx.db.scalars(q))
    status = {}
    for g, name in GATES.items():
        last = [h for h in hist if h["gate"] == g]
        status[g] = {"name": name, "status": last[-1]["decision"] if last else "pending", "object_version": last[-1]["object_version"] if last else None}
    return {"status": status, "history": hist}


class GateSubmitIn(BaseModel):
    object_version: str
    comment: str = ""


@router.post("/{project_id}/gates/{gate}/submit")
def gate_submit(gate: str, body: GateSubmitIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    if gate not in GATES or gate == "G0":
        raise HTTPException(400, "G0 请通过任务书提交；可用闸门 G1–G5")
    ctx.db.add(GateApproval(project_id=ctx.project.id, gate=gate, object_version=body.object_version, decision="submitted", by=ctx.user.id, comment=body.comment))
    if gate == "G3" and body.object_version.startswith("outline@v"):
        o = ctx.db.scalar(select(Outline).where(Outline.project_id == ctx.project.id, Outline.version == int(body.object_version.split("@v")[1])))
        if o:
            o.gate_status = "submitted"
    audit.log(ctx.db, "gate.submit", project_id=ctx.project.id, actor_id=ctx.user.id, object_type=gate, object_id=body.object_version)
    ctx.db.commit()
    return gates(ctx)


@router.post("/{project_id}/gates/{gate}/decide")
def gate_decide(gate: str, body: DecisionIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    ctx.require_mentor()
    if gate not in GATES or gate == "G0":
        raise HTTPException(400, "G0 请通过任务书审批")
    last = ctx.db.scalar(
        select(GateApproval).where(GateApproval.project_id == ctx.project.id, GateApproval.gate == gate).order_by(GateApproval.id.desc()).limit(1)
    )
    if not last or last.decision != "submitted":
        raise HTTPException(409, "该闸门没有待审批的提交")
    ctx.db.add(GateApproval(project_id=ctx.project.id, gate=gate, object_version=last.object_version, decision=body.decision, by=ctx.user.id, comment=body.comment))
    if gate == "G3" and last.object_version.startswith("outline@v"):
        o = ctx.db.scalar(select(Outline).where(Outline.project_id == ctx.project.id, Outline.version == int(last.object_version.split("@v")[1])))
        if o:
            o.gate_status = body.decision
    audit.log(ctx.db, "gate.decide", project_id=ctx.project.id, actor_id=ctx.user.id, object_type=gate, object_id=last.object_version, detail=body.model_dump())
    ctx.db.commit()
    return gates(ctx)


# ------------------------------------------------------------------ 通用 AI 任务（阶段一 P01–P05 及其他分析类 Prompt）


def default_params(db: Session, project: Project) -> dict[str, Any]:
    c = approved_charter(db, project.id) or latest_charter(db, project.id)
    d = c.data if c else {}
    o = db.scalar(select(Outline).where(Outline.project_id == project.id).order_by(Outline.version.desc()).limit(1))
    concepts = "、".join(x.get("name", "") for x in d.get("concepts", []) or [])
    return {
        "discipline": d.get("discipline", "医学"),
        "mentor_topic": d.get("mentor_topic", project.name),
        "background": d.get("background", ""),
        "seeds": "、".join(d.get("seed_ids", []) or []),
        "working_title": d.get("working_title") or d.get("mentor_topic", project.name),
        "core_questions": "；".join(d.get("core_questions", []) or []),
        "boundaries": "纳入：" + "；".join(d.get("include", []) or []) + "。排除：" + "；".join(d.get("exclude", []) or []),
        "include": "；".join(d.get("include", []) or []),
        "exclude": "；".join(d.get("exclude", []) or []),
        "concepts": concepts,
        "deadline": d.get("deadline", "约 12 周"),
        "lab_direction": d.get("lab_direction", "（项目 / 实验方向）"),
        "journal": ", ".join(j.get("name", "") for j in d.get("target_journals", []) or []) or "目标期刊",
        "outline": outline_text(o),
        "extra_view": "导师指定的视角",
        "min_cards": 8,
        "seed_pmids": "、".join(d.get("seed_ids", []) or []),
        "goal": "；".join(d.get("core_questions", []) or []),
    }


class AiIn(BaseModel):
    params: dict[str, Any] = {}
    provider: str | None = None


@router.get("/{project_id}/ai/available")
def ai_available(ctx: ProjectCtx = Depends(get_project_ctx)):
    return [
        {"id": p.id, "name": p.name, "ref": p.ref, "stage": p.stage, "min_level": p.min_level, "enabled": level_ge(ctx.project.ai_level_max, p.min_level)}
        for p in all_prompts()
    ]


@router.post("/{project_id}/ai/{task}")
def run_ai(task: str, body: AiIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    try:
        get_prompt(task)
    except KeyError:
        raise HTTPException(404, "Prompt 不存在")
    params = {**default_params(ctx.db, ctx.project), **body.params}
    res = gateway.call(ctx.db, task=task, params=params, user=ctx.user, project=ctx.project, provider_override=body.provider)
    audit.log(ctx.db, "ai.run", project_id=ctx.project.id, actor_id=ctx.user.id, object_type="model_run", object_id=res.run_id, detail={"task": task})
    ctx.db.commit()
    return {"run_id": res.run_id, "text": res.text, "data": res.data, "valid": res.valid, "errors": res.errors, "provider": res.provider, "model": res.model}


@router.post("/{project_id}/ai/{task}/render")
def render_ai(task: str, body: AiIn, ctx: ProjectCtx = Depends(get_project_ctx)):
    """导出渲染好的 Prompt，便于复制到网页版 AI（兼容模式）；同样受介入级别约束。"""
    gateway.check_level(ctx.project, task)
    params = {**default_params(ctx.db, ctx.project), **body.params}
    system, prompt = gateway.render(task, params)
    return {"ref": get_prompt(task).ref, "system": system, "prompt": prompt}


@router.get("/{project_id}/runs")
def list_runs(task: str | None = None, limit: int = 50, ctx: ProjectCtx = Depends(get_project_ctx)):
    q = select(ModelRun).where(ModelRun.project_id == ctx.project.id).order_by(ModelRun.id.desc()).limit(min(limit, 500))
    if task:
        q = q.where(ModelRun.task == task)
    return rows(ctx.db.scalars(q), exclude={"input_text"})


@router.get("/{project_id}/runs/{run_id}")
def get_run(run_id: str, ctx: ProjectCtx = Depends(get_project_ctx)):
    r = ctx.db.scalar(select(ModelRun).where(ModelRun.run_id == run_id, ModelRun.project_id == ctx.project.id))
    if not r:
        raise HTTPException(404, "调用记录不存在")
    return row(r)


@router.get("/{project_id}/jobs")
def list_jobs(ctx: ProjectCtx = Depends(get_project_ctx)):
    return rows(ctx.db.scalars(select(Job).where(Job.project_id == ctx.project.id).order_by(Job.id.desc()).limit(50)))
