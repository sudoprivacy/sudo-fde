"""提示词加载与渲染。提示词文件 = front matter（version）＋ [系统] ＋ [用户] 两段 Jinja 模板。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True)


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    user: str

    def render(self, **data: Any) -> tuple[str, str]:
        data.setdefault("evidence_rules", evidence_rules())
        data.setdefault("data_rules", data_rules())
        return _env.from_string(self.system).render(**data), _env.from_string(self.user).render(**data)


@lru_cache
def evidence_rules() -> str:
    return (PROMPT_DIR / "shared" / "evidence_rules.md").read_text(encoding="utf-8").strip()


@lru_cache
def data_rules() -> str:
    return (PROMPT_DIR / "shared" / "data_rules.md").read_text(encoding="utf-8").strip()


@lru_cache
def load(name: str) -> Prompt:
    """name 形如 'interview/hypothesis_v1'。"""
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    _, fm, body = raw.split("---", 2)
    meta = yaml.safe_load(fm)
    sys_part, _, user_part = body.partition("[用户]")
    system = sys_part.replace("[系统]", "", 1).strip()
    return Prompt(name=name, version=meta["version"], system=system, user=user_part.strip())


def j(obj: Any) -> str:
    """提示词里嵌入结构化数据的统一格式。"""
    return json.dumps(obj, ensure_ascii=False, indent=1)


def render_evidence(evidence: list[dict[str, Any]]) -> str:
    lines = []
    for ev in evidence:
        text = ev.get("text") or ""
        lines.append(f"[{ev['id']}] ({ev['kind']}{', ' + ev['ref'] if ev.get('ref') else ''}) {text}")
    return "\n".join(lines) if lines else "（无）"
