"""核心数据模型（规范 9.5）。

对象：租户、用户、项目、任务书、闸门审批、检索、文献记录、全文、证据卡、大纲、
段落与文本来源、审阅批注、断言核查、AI 调用日志、审计日志、模型网关配置、后台任务。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


# ---------------------------------------------------------------- 平台基础 X1


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("TEN"))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("USR"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    # student | mentor | admin
    role: Mapped[str] = mapped_column(String(20), default="student")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 模块 A：项目与命题


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("PRJ"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(300))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 导师设定，学生不能自行调高（1.4）
    ai_level_max: Mapped[str] = mapped_column(String(4), default="L1")
    # “数据不外发”：强制只调用本地模型（9.1）
    data_local_only: Mapped[bool] = mapped_column(Boolean, default=False)
    review_type: Mapped[str] = mapped_column(String(30), default="narrative")
    budget_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 流程状态标记：calibration_passed、calibration_override 等
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # student | mentor
    role: Mapped[str] = mapped_column(String(20))
    project: Mapped[Project] = relationship(back_populates="members")


class Charter(Base):
    """《综述任务书》，每次修改生成新版本；导师批准（G0）后锁定。"""

    __tablename__ = "charters"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # draft | submitted | approved | rejected
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GateApproval(Base):
    """人工闸门 G0–G5 的审批记录。"""

    __tablename__ = "gate_approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    gate: Mapped[str] = mapped_column(String(4))
    object_version: Mapped[str] = mapped_column(String(100))
    # submitted | approved | rejected
    decision: Mapped[str] = mapped_column(String(20))
    by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    comment: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 模块 B：检索


class SearchQuery(Base):
    __tablename__ = "search_queries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    database: Mapped[str] = mapped_column(String(40), default="pubmed")
    query: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(20), default="human")  # human | ai_accepted
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SearchRun(Base):
    """一次检索执行，即 PRISMA-S 检索日志的一行。"""

    __tablename__ = "search_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    query_id: Mapped[int | None] = mapped_column(ForeignKey("search_queries.id"), nullable=True)
    database: Mapped[str] = mapped_column(String(40))
    platform: Mapped[str] = mapped_column(String(80), default="")
    query: Mapped[str] = mapped_column(Text)
    limits: Mapped[str] = mapped_column(String(300), default="none")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    # database | supplementary（引文追溯、预印本、注册库、手工导入等）
    kind: Mapped[str] = mapped_column(String(20), default="database")
    recall: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    precision_sample: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


# ---------------------------------------------------------------- 模块 C：文献库与筛选


class Record(Base):
    __tablename__ = "records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    citekey: Mapped[str] = mapped_column(String(120), index=True)
    pmid: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    pmcid: Mapped[str | None] = mapped_column(String(20), nullable=True)
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    authors: Mapped[list[str]] = mapped_column(JSON, default=list)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal: Mapped[str] = mapped_column(String(400), default="")
    abstract: Mapped[str] = mapped_column(Text, default="")
    mesh: Mapped[list[str]] = mapped_column(JSON, default=list)
    pub_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(60), default="")
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("records.id"), nullable=True)
    # none | retracted | concern | correction | unknown
    retraction_status: Mapped[str] = mapped_column(String(20), default="unknown")
    resolve_status: Mapped[str] = mapped_column(String(20), default="unchecked")  # ok | mismatch | unresolved
    oa_status: Mapped[str] = mapped_column(String(20), default="unknown")
    # AI 预判（P10）
    ai_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ai_level: Mapped[str | None] = mapped_column(String(2), nullable=True)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    ai_quote: Mapped[str] = mapped_column(Text, default="")
    ai_key_finding: Mapped[str] = mapped_column(Text, default="")
    ai_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 校准轮学生独立标注（盲评，在 AI 预判前完成）
    calibration_label: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 最终决定：include | exclude | uncertain
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # human | ai_then_human | ai_unreviewed
    decision_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    relevance_level: Mapped[str | None] = mapped_column(String(2), nullable=True)
    assigned_sections: Mapped[list[str]] = mapped_column(JSON, default=list)
    fulltext_status: Mapped[str] = mapped_column(String(20), default="none")  # none | parsed | failed
    url: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScreeningVote(Base):
    """系统综述双人独立筛选：每位评审者一票；AI 只能作为第三意见（4.2）。"""

    __tablename__ = "screening_votes"
    __table_args__ = (UniqueConstraint("record_id", "user_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FullText(Base):
    """结构化全文。受版权保护的全文仅上传者本人可用（9.7）。"""

    __tablename__ = "fulltexts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"), unique=True)
    source: Mapped[str] = mapped_column(String(40))  # PMC_OA | EuropePMC | upload | grobid
    is_open_access: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # [{"id": "p12", "section": "Results", "text": "..."}]
    paragraphs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JournalMetric(Base):
    """机构已授权的期刊指标表（导入），只作参考，不作为纳入依据（9.4）。"""

    __tablename__ = "journal_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    journal: Mapped[str] = mapped_column(String(400), index=True)
    issn: Mapped[str] = mapped_column(String(40), default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(200), default="")


# ---------------------------------------------------------------- 模块 D：证据与大纲


class EvidenceCard(Base):
    __tablename__ = "evidence_cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: new_id("EC"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"))
    citekey: Mapped[str] = mapped_column(String(120))
    claim: Mapped[str] = mapped_column(Text)
    evidence_type: Mapped[str] = mapped_column(String(30), default="")
    model_or_population: Mapped[str] = mapped_column(Text, default="")
    quantitative: Mapped[str] = mapped_column(Text, default="")
    quote: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    limitations: Mapped[str] = mapped_column(Text, default="")
    outline_nodes: Mapped[list[str]] = mapped_column(JSON, default=list)
    secondary: Mapped[bool] = mapped_column(Boolean, default=False)
    # unverified | verified | modified
    verify_status: Mapped[str] = mapped_column(String(20), default="unverified")
    quote_found: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extracted_by_run: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outline(Base):
    """大纲版本。kind=student_draft 为学生独立初版，提交前不显示 AI 证据地图（5.2）。"""

    __tablename__ = "outlines"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="revision")  # student_draft | revision
    # [{"id": "2.1", "title": "...", "thesis": "本节要证明什么", "level": 2}]
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    gate_status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | submitted | approved | rejected
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 模块 E：写作


class Paragraph(Base):
    __tablename__ = "paragraphs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("P"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    outline_node: Mapped[str] = mapped_column(String(40))
    order: Mapped[int] = mapped_column(Integer, default=0)
    thesis: Mapped[str] = mapped_column(Text, default="")
    card_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 文本来源片段：[{"text": "...", "origin": "human|ai_accepted|pasted", "ai_run": null}]
    spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="drafting")  # drafting | done
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def text(self) -> str:
        return "".join(s.get("text", "") for s in self.spans or [])


class ParagraphRevision(Base):
    """版本历史：保留每段文字的修改过程与时间戳（7.4）。"""

    __tablename__ = "paragraph_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paragraph_id: Mapped[str] = mapped_column(ForeignKey("paragraphs.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Annotation(Base):
    """挂在段落（句子）上的批注：AI 审阅意见、导师批注、语言建议。"""

    __tablename__ = "annotations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    paragraph_id: Mapped[str] = mapped_column(ForeignKey("paragraphs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # ai_review | mentor | language | skeleton
    sentence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | resolved | accepted | rejected
    ai_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 模块 F：质量与交付


class ClaimCheck(Base):
    __tablename__ = "claim_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: new_id("CK"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    paragraph_id: Mapped[str] = mapped_column(ForeignKey("paragraphs.id", ondelete="CASCADE"), index=True)
    paragraph_revision: Mapped[int] = mapped_column(Integer)
    sentence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claim: Mapped[str] = mapped_column(Text)
    citekey: Mapped[str] = mapped_column(String(120), default="")
    # strong | partial | none | undetermined | na
    ai_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    human_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quote: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    # pending | resolved | stale
    status: Mapped[str] = mapped_column(String(20), default="pending")
    mentor_sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChecklistState(Base):
    """SANRA 自评与检查清单（F3）。"""

    __tablename__ = "checklist_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 模型网关、留痕与任务


class ProviderConfig(Base):
    """模型提供方：OpenAI 兼容接口（DeepSeek、通义千问、OpenAI、LiteLLM 代理、Ollama、vLLM）或 mock。"""

    __tablename__ = "provider_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(30), default="openai_compatible")  # openai_compatible | mock
    base_url: Mapped[str] = mapped_column(String(300), default="")
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    default_model: Mapped[str] = mapped_column(String(120), default="")
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    price_in: Mapped[float] = mapped_column(Float, default=0.0)  # 每百万 token
    price_out: Mapped[float] = mapped_column(Float, default=0.0)
    context_tokens: Mapped[int] = mapped_column(Integer, default=32000)


class TaskRoute(Base):
    """按任务类型选择模型，例如筛选用本地模型、审阅用云端强模型（X2）。"""

    __tablename__ = "task_routes"
    __table_args__ = (UniqueConstraint("tenant_id", "task"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    task: Mapped[str] = mapped_column(String(20))  # "*" 或 Prompt 编号
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120), default="")
    local_fallback: Mapped[str] = mapped_column(String(60), default="")


class ModelRun(Base):
    """AI 调用日志：模型、版本、Prompt 版本、采样参数、输入输出哈希（1.1、9.7）。"""

    __tablename__ = "model_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), unique=True, default=lambda: new_id("RUN"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    task: Mapped[str] = mapped_column(String(20))
    prompt_version: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120))
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    input_hash: Mapped[str] = mapped_column(String(80))
    output_hash: Mapped[str] = mapped_column(String(80), default="")
    input_text: Mapped[str] = mapped_column(Text, default="")
    output_text: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | error | invalid_output
    error: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """追加写入的审计日志，哈希链防篡改（9.7）。"""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(80))
    object_type: Mapped[str] = mapped_column(String(40), default="")
    object_id: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="")


class Job(Base):
    """后台长任务（批量预筛、证据卡抽取、检索拉取等），可断点续跑（9.7）。"""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HttpCache(Base):
    """外部接口缓存（NCBI、Crossref、OpenAlex 等），降低限额压力（9.7）。"""

    __tablename__ = "http_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
