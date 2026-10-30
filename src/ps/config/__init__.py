"""运行配置：阈值来自 default.yaml，部署相关的值来自环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_YAML = Path(__file__).with_name("default.yaml")


def _load_yaml() -> dict[str, Any]:
    path = Path(os.environ.get("PS_CONFIG_FILE", _DEFAULT_YAML))
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_tokens(raw: str) -> dict[str, str]:
    """PS_SERVICE_TOKENS="token1:tenant_a,token2:tenant_b" → {token: tenant}。"""
    out: dict[str, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        token, _, tenant = pair.partition(":")
        if token and tenant:
            out[token] = tenant
    return out


@dataclass
class Settings:
    db_url: str
    mode: str                      # saas | box（阶段一只跑 saas，K57）
    service_tokens: dict[str, str]
    actor_secret: str
    llm_backend: str               # openai | fake
    llm_base_url: str
    llm_api_key: str
    model_reasoner: str
    model_critic: str
    llm_schema_mode: str           # json_schema | json_object | none
    thresholds: dict[str, Any] = field(default_factory=dict)

    def t(self, dotted: str) -> Any:
        node: Any = self.thresholds
        for part in dotted.split("."):
            node = node[part]
        return node


def load_settings() -> Settings:
    default_db = Path.home() / ".sudo-fde" / "pattern.db"
    default_db.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        db_url=os.environ.get("PS_DB_URL", f"sqlite:///{default_db}"),
        mode=os.environ.get("PS_MODE", "saas"),
        service_tokens=_parse_tokens(os.environ.get("PS_SERVICE_TOKENS", "dev-token:tenant_dev")),
        actor_secret=os.environ.get("PS_ACTOR_SECRET", "dev-actor-secret"),
        llm_backend="sudorouter",
        llm_base_url="",
        llm_api_key="",
        model_reasoner=os.environ.get("PS_MODEL_REASONER", ""),
        model_critic=os.environ.get("PS_MODEL_CRITIC", "") or os.environ.get("PS_MODEL_REASONER", ""),
        llm_schema_mode=os.environ.get("PS_LLM_SCHEMA_MODE", "json_schema"),
        thresholds=_load_yaml(),
    )


@lru_cache
def get_settings() -> Settings:
    return load_settings()
