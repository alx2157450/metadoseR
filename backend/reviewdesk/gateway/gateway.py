"""模型网关：路由、介入级别闸门、“数据不外发”强制本地、预算、输出校验与调用留痕。

所有 AI 调用都必须经过 ``call()``：
1. 检查导师设定的 AI 介入上限（1.4）是否允许该 Prompt；
2. 按任务类型选择提供方（X2），“数据不外发”项目强制本地模型（9.1）；
3. 渲染版本化 Prompt，温度按注册表设置（9.6）；
4. JSON 输出按 schema 校验，失败自动重试一次，仍失败记为 invalid_output 转人工；
5. 写入 ModelRun（模型、版本、Prompt 版本、采样参数、输入输出哈希、token、费用）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ModelRun, Project, ProviderConfig, TaskRoute, User
from ..security import decrypt_secret
from . import rule_engine
from .prompts import common_constraint, extract_json, get_prompt, level_ge, validate


@dataclass
class Provider:
    name: str
    kind: str  # openai_compatible | rules
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    is_local: bool = False
    price_in: float = 0.0
    price_out: float = 0.0
    context_tokens: int = 32000


@dataclass
class RunResult:
    run_id: str
    text: str
    data: Any
    provider: str
    model: str
    valid: bool
    errors: list[str]


class GatewayError(HTTPException):
    pass


def builtin_providers() -> dict[str, Provider]:
    s = get_settings()
    out = {"rules": Provider("rules", "rules", model="rule-engine-1", is_local=True)}
    if s.openai_base_url:
        out["cloud"] = Provider("cloud", "openai_compatible", s.openai_base_url, s.openai_api_key or "", s.openai_model)
    if s.ollama_base_url:
        out["ollama"] = Provider("ollama", "openai_compatible", s.ollama_base_url, "ollama", s.ollama_model, is_local=True)
    return out


def list_providers(db: Session, tenant_id: str) -> dict[str, Provider]:
    providers = builtin_providers()
    for pc in db.scalars(select(ProviderConfig).where(ProviderConfig.tenant_id == tenant_id)):
        providers[pc.name] = Provider(
            pc.name,
            "rules" if pc.kind == "rules" else "openai_compatible",
            pc.base_url,
            decrypt_secret(pc.api_key_enc),
            pc.default_model,
            pc.is_local,
            pc.price_in,
            pc.price_out,
            pc.context_tokens,
        )
    return providers


def resolve_provider(db: Session, tenant_id: str, task: str, project: Project | None) -> tuple[Provider, str]:
    s = get_settings()
    providers = list_providers(db, tenant_id)
    route = db.scalar(select(TaskRoute).where(TaskRoute.tenant_id == tenant_id, TaskRoute.task == task)) or db.scalar(
        select(TaskRoute).where(TaskRoute.tenant_id == tenant_id, TaskRoute.task == "*")
    )
    default = s.default_provider if s.default_provider in providers else "rules"
    name = route.provider if route and route.provider in providers else default
    prov = providers[name]
    model = (route.model if route and route.model else "") or prov.model

    must_local = (project is not None and project.data_local_only) or s.offline
    if must_local and not prov.is_local:
        fb = route.local_fallback if route and route.local_fallback in providers else None
        candidates = [providers[fb]] if fb else []
        candidates += [p for p in providers.values() if p.is_local and p.kind != "rules"]
        candidates += [providers["rules"]]
        prov = candidates[0]
        model = prov.model
    return prov, model


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int(cjk * 0.7 + (len(text) - cjk) / 4) + 1


def _call_openai(prov: Provider, model: str, system: str, user: str, temperature: float) -> tuple[str, int, int]:
    s = get_settings()
    url = prov.base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {prov.api_key}"} if prov.api_key else {}
    body = {
        "model": model,
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=max(120.0, s.http_timeout))
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            d = r.json()
            text = d["choices"][0]["message"]["content"] or ""
            usage = d.get("usage") or {}
            return text, usage.get("prompt_tokens") or _estimate_tokens(system + user), usage.get("completion_tokens") or _estimate_tokens(text)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            last_err = e
            time.sleep(2**attempt)
    raise GatewayError(502, f"模型调用失败（{prov.name}/{model}）：{last_err}")


def check_level(project: Project | None, task: str) -> None:
    spec = get_prompt(task)
    if project is not None and not level_ge(project.ai_level_max, spec.min_level):
        raise GatewayError(
            403,
            f"{spec.id}「{spec.name}」需要 AI 介入级别 ≥ {spec.min_level}，本项目上限为 {project.ai_level_max}（由导师设定）",
        )


def check_budget(db: Session, project: Project | None) -> None:
    if project is None or project.budget_limit is None:
        return
    spent = db.scalar(select(func.coalesce(func.sum(ModelRun.cost), 0.0)).where(ModelRun.project_id == project.id)) or 0.0
    if spent >= project.budget_limit:
        raise GatewayError(402, f"项目 AI 费用已达预算上限（{spent:.2f} / {project.budget_limit:.2f}）")


def render(task: str, params: dict[str, Any]) -> tuple[str, str]:
    spec = get_prompt(task)
    return common_constraint(), spec.render(params)


def call(
    db: Session,
    *,
    task: str,
    params: dict[str, Any],
    user: User,
    project: Project | None = None,
    ctx: dict[str, Any] | None = None,
    provider_override: str | None = None,
) -> RunResult:
    spec = get_prompt(task)
    check_level(project, task)
    check_budget(db, project)
    prov, model = resolve_provider(db, user.tenant_id, task, project)
    if provider_override:
        providers = list_providers(db, user.tenant_id)
        if provider_override in providers:
            cand = providers[provider_override]
            if not ((project and project.data_local_only) or get_settings().offline) or cand.is_local:
                prov, model = cand, cand.model

    system, prompt = render(task, params)
    input_text = system + "\n\n" + prompt
    errors: list[str] = []
    data: Any = None
    text = ""
    tin = tout = 0
    status = "ok"
    for attempt in range(2):
        if prov.kind == "rules":
            text = rule_engine.run(task, params, ctx or {})
            tin, tout = _estimate_tokens(input_text), _estimate_tokens(text)
        else:
            extra = ""
            if attempt and errors:
                extra = "\n\n上一次输出未通过格式校验：" + "; ".join(errors[:5]) + "。请只输出符合要求的 JSON。"
            text, a, b = _call_openai(prov, model, system, prompt + extra, spec.temperature)
            tin, tout = tin + a, tout + b
        if spec.output != "json":
            data, errors = text, []
            break
        try:
            data = extract_json(text)
            errors = validate(data, spec.schema or {})
        except (ValueError, TypeError) as e:
            data, errors = None, [f"JSON 解析失败：{e}"]
        if not errors:
            break
    if errors:
        status = "invalid_output"

    cost = (tin * prov.price_in + tout * prov.price_out) / 1_000_000
    run = ModelRun(
        project_id=project.id if project else None,
        user_id=user.id,
        task=task,
        prompt_version=spec.ref,
        provider=prov.name,
        model=model,
        is_local=prov.is_local,
        temperature=spec.temperature,
        input_hash=_hash(input_text),
        output_hash=_hash(text),
        input_text=input_text,
        output_text=text,
        tokens_in=tin,
        tokens_out=tout,
        cost=cost,
        status=status,
        error="; ".join(errors),
    )
    db.add(run)
    db.flush()
    return RunResult(run.run_id, text, data if not errors else None, prov.name, model, not errors, errors)


def batch_size_for(db: Session, user: User, project: Project | None, task: str, per_item_tokens: int, reserve: int = 4000) -> int:
    """按所用模型的上下文长度自动分批（替代原“文献分割器”手工切分 100 篇 / 文件，C7）。"""
    prov, _ = resolve_provider(db, user.tenant_id, task, project)
    if prov.kind == "rules":
        return 200
    usable = max(2000, prov.context_tokens - reserve)
    # 输出约为输入的 1/3，留足余量
    return max(1, min(100, int(usable / (per_item_tokens * 1.4))))
