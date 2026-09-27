"""Prompt 注册表加载、参数渲染与输出校验（X3、9.6）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LEVELS = ["L0", "L1", "L2", "L3"]


def level_ge(have: str, need: str) -> bool:
    return LEVELS.index(have) >= LEVELS.index(need)


@dataclass
class PromptSpec:
    id: str
    name: str
    version: str
    stage: int
    min_level: str
    output: str
    temperature: float
    template: str
    params: list[str] = field(default_factory=list)
    schema: dict[str, Any] | None = None
    changelog: str = ""

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def render(self, params: dict[str, Any]) -> str:
        def sub(m: re.Match) -> str:
            v = params.get(m.group(1), "")
            if isinstance(v, (list, dict)):
                return json.dumps(v, ensure_ascii=False, indent=1)
            return str(v) if v not in (None, "") else "（未提供）"

        return re.sub(r"\{\{(\w+)\}\}", sub, self.template)


@lru_cache
def _load() -> tuple[str, dict[str, PromptSpec]]:
    raw = yaml.safe_load((Path(__file__).parent.parent / "prompts" / "registry.yaml").read_text("utf-8"))
    specs = {}
    for pid, d in raw["prompts"].items():
        specs[pid] = PromptSpec(
            id=pid,
            name=d["name"],
            version=d["version"],
            stage=d["stage"],
            min_level=d["min_level"],
            output=d["output"],
            temperature=float(d.get("temperature", 0)),
            template=d["template"],
            params=d.get("params", []),
            schema=d.get("schema"),
            changelog=d.get("changelog", ""),
        )
    return raw["common_constraint"], specs


def common_constraint() -> str:
    return _load()[0]


def get_prompt(pid: str) -> PromptSpec:
    specs = _load()[1]
    if pid not in specs:
        raise KeyError(f"未知 Prompt：{pid}")
    return specs[pid]


def all_prompts() -> list[PromptSpec]:
    return list(_load()[1].values())


# ------------------------------------------------------------------ 输出解析与校验


def extract_json(text: str) -> Any:
    """从模型输出中取出 JSON（兼容 ```json 代码块与前后说明文字）。"""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, flags=re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        raise ValueError("输出中没有 JSON")
    start = min(starts)
    end = max(t.rfind("]"), t.rfind("}"))
    return json.loads(t[start : end + 1])


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """JSON Schema 的精简子集校验：type / enum / required / properties / items。"""
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 取值 {value!r} 不在 {schema['enum']}")
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        py = tuple(_TYPES[x] for x in types)
        ok = isinstance(value, py) and not (isinstance(value, bool) and "boolean" not in types and bool not in py)
        if not ok:
            errors.append(f"{path}: 类型应为 {types}")
            return errors
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path}: 缺少字段 {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in value and value[k] is not None:
                errors.extend(validate(value[k], sub, f"{path}.{k}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors
