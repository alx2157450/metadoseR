"""交付（F6）：稿件（Markdown / Word）、参考文献、核查报告与项目包导入导出（X5）。

Word 通过 Pandoc 导出：若安装了 Better BibTeX 的 Lua 过滤器（zotero.lua），引用会成为
可在 Zotero Word 插件中刷新的域代码；否则由 citeproc 按 CSL 样式渲染为静态格式。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Annotation,
    AuditLog,
    Charter,
    ChecklistState,
    ClaimCheck,
    EvidenceCard,
    GateApproval,
    ModelRun,
    Outline,
    Paragraph,
    ParagraphRevision,
    Project,
    Record,
    SearchQuery,
    SearchRun,
)

PACKAGE_VERSION = 1


def _bib_escape(s: str) -> str:
    return (s or "").replace("{", "\\{").replace("}", "\\}")


def bibtex_entry(r: Record) -> str:
    fields = {
        "title": r.title,
        "author": " and ".join(r.authors or []),
        "journal": r.journal,
        "year": str(r.year or ""),
        "doi": r.doi or "",
        "pmid": r.pmid or "",
        "pmcid": r.pmcid or "",
        "url": r.url or "",
    }
    if r.retraction_status == "retracted":
        fields["note"] = "Retracted publication"
    elif any((p or "").lower() == "preprint" for p in r.pub_types or []):
        fields["note"] = "Preprint, not peer reviewed"
    body = ",\n".join(f"  {k} = {{{_bib_escape(v)}}}" for k, v in fields.items() if v)
    return f"@article{{{r.citekey},\n{body}\n}}\n"


def manuscript_markdown(db: Session, project: Project, title: str) -> str:
    outline = db.scalar(select(Outline).where(Outline.project_id == project.id).order_by(Outline.version.desc()).limit(1))
    paras = list(db.scalars(select(Paragraph).where(Paragraph.project_id == project.id).order_by(Paragraph.outline_node, Paragraph.order)))
    by_node: dict[str, list[Paragraph]] = {}
    for p in paras:
        by_node.setdefault(p.outline_node, []).append(p)
    lines = ["---", f'title: "{title}"', "bibliography: references.bib", "---", ""]
    nodes = outline.nodes if outline else [{"id": k, "title": k, "level": 1} for k in by_node]
    for n in nodes:
        lines.append("#" * min(6, n.get("level", 1)) + f" {n.get('title', n['id'])}")
        lines.append("")
        for p in by_node.get(n["id"], []):
            lines += [p.text.strip(), ""]
    lines += ["# References", ""]
    return "\n".join(lines)


def pandoc_available() -> bool:
    return shutil.which("pandoc") is not None


def to_docx(markdown: str, bib: str, csl: bytes | None = None) -> bytes:
    """Pandoc 导出 Word。优先使用 Better BibTeX 的 zotero.lua 过滤器（REVIEWDESK_ZOTERO_LUA）。"""
    if not pandoc_available():
        raise RuntimeError("服务器未安装 pandoc：请使用 Docker 镜像部署，或下载 Markdown + BibTeX 后在本机转换")
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        (dp / "manuscript.md").write_text(markdown, "utf-8")
        (dp / "references.bib").write_text(bib, "utf-8")
        cmd = ["pandoc", "manuscript.md", "-o", "manuscript.docx"]
        lua = os.environ.get("REVIEWDESK_ZOTERO_LUA")
        if lua and Path(lua).exists():
            cmd += ["--lua-filter", lua, "-M", "zotero_library=1"]
        else:
            cmd += ["--citeproc"]
            if csl:
                (dp / "style.csl").write_bytes(csl)
                cmd += ["--csl", "style.csl"]
        subprocess.run(cmd, cwd=d, check=True, capture_output=True, timeout=120)
        return (dp / "manuscript.docx").read_bytes()


# ------------------------------------------------------------------ 项目包（X5）


def _dump(objs) -> list[dict[str, Any]]:
    from ..api.common import row

    return [row(o) for o in objs]


PACKAGE_TABLES = [
    ("charters", Charter),
    ("gate_approvals", GateApproval),
    ("search_queries", SearchQuery),
    ("search_runs", SearchRun),
    ("records", Record),
    ("evidence_cards", EvidenceCard),
    ("outlines", Outline),
    ("paragraphs", Paragraph),
    ("paragraph_revisions", ParagraphRevision),
    ("annotations", Annotation),
    ("claim_checks", ClaimCheck),
    ("checklist_states", ChecklistState),
    ("model_runs", ModelRun),
    ("audit_logs", AuditLog),
]


def export_package(db: Session, project: Project) -> bytes:
    """导出完整项目包：任务书、检索式、筛选决定、证据卡、各版本稿件、审计日志。

    受版权保护的全文不进入项目包（9.7）；开放获取全文可在导入后重新获取。
    """
    from ..api.common import row

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {"package_version": PACKAGE_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "project": row(project)}
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1, default=str))
        para_ids = list(db.scalars(select(Paragraph.id).where(Paragraph.project_id == project.id)))
        for name, model in PACKAGE_TABLES:
            if model is ParagraphRevision:
                objs = db.scalars(select(model).where(model.paragraph_id.in_(para_ids))) if para_ids else []
            else:
                objs = db.scalars(select(model).where(model.project_id == project.id))
            z.writestr(f"{name}.json", json.dumps(_dump(objs), ensure_ascii=False, default=str))
    return buf.getvalue()


def _parse_dt(v: Any) -> Any:
    if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and v[10] == "T":
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return v
    return v


def import_package(db: Session, data: bytes, *, tenant_id: str, owner_id: str) -> Project:
    """导入项目包（单机版 ↔ 服务器版迁移）。生成新的项目 ID，并重映射内部主键与外键。"""
    from ..models import new_id

    z = zipfile.ZipFile(io.BytesIO(data))
    manifest = json.loads(z.read("manifest.json"))
    src = manifest["project"]
    p = Project(
        tenant_id=tenant_id, name=src["name"] + "（导入）", owner_id=owner_id, ai_level_max=src.get("ai_level_max", "L1"),
        data_local_only=src.get("data_local_only", False), review_type=src.get("review_type", "narrative"), state=src.get("state") or {},
    )
    db.add(p)
    db.flush()
    rec_map: dict[int, int] = {}
    para_map: dict[str, str] = {}
    user_cols = {"created_by", "approved_by", "by", "run_by", "decided_by", "verified_by", "author_id", "resolved_by", "user_id", "uploaded_by", "actor_id"}
    for name, model in PACKAGE_TABLES:
        items = json.loads(z.read(f"{name}.json"))
        if model is Record:
            items.sort(key=lambda x: x["id"])
        for it in items:
            it = {k: _parse_dt(v) for k, v in it.items()}
            old_id = it.pop("id", None)
            it["project_id"] = p.id if "project_id" in it else it.get("project_id")
            for c in user_cols & it.keys():
                if it[c] is not None:
                    it[c] = owner_id
            if model is Record and it.get("duplicate_of"):
                it["duplicate_of"] = rec_map.get(it["duplicate_of"])
            if "record_id" in it:
                it["record_id"] = rec_map.get(it["record_id"])
                if it["record_id"] is None:
                    continue
            if model is Paragraph:
                para_map[old_id] = new_id("P")
                old_id, it["id"] = None, para_map[old_id]
            if "paragraph_id" in it:
                it["paragraph_id"] = para_map.get(it["paragraph_id"])
                if it["paragraph_id"] is None:
                    continue
            for uniq, prefix in (("card_id", "EC"), ("check_id", "CK"), ("run_id", "RUN")):
                if uniq in it and model is not Record and it[uniq]:
                    col = getattr(model, uniq)
                    if db.scalar(select(col).where(col == it[uniq])):
                        it[uniq] = new_id(prefix)
            if model is AuditLog:
                # 审计日志通过 audit.log 重新追加，保持哈希链完整；原始时间与操作人写入 detail
                from .. import audit

                audit.log(
                    db, "imported." + it["action"], project_id=p.id, actor_id=owner_id, object_type=it.get("object_type", ""),
                    object_id=it.get("object_id", ""), detail={**(it.get("detail") or {}), "orig_at": str(it.get("at")), "orig_actor": it.get("actor_id"), "imported_from": src["id"]},
                )
                continue
            if model is Record and "query_id" in it:
                it.pop("query_id")
            if model is SearchRun:
                it["query_id"] = None
            obj = model(**{k: v for k, v in it.items() if k in model.__table__.columns.keys()})
            db.add(obj)
            db.flush()
            if model is Record:
                rec_map[old_id] = obj.id
    return p
