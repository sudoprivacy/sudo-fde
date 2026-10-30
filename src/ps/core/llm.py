"""Shared pipeline LLM types. SaaS calls use fde_server.router.RouterLLM exclusively.

The upstream direct-provider implementation is deliberately omitted.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ps.config import Settings


class LLMUnavailable(RuntimeError):
    pass


class LLMBadOutput(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.latency_ms += other.latency_ms


@dataclass
class LLMCall:
    task: str                    # 例如 interview.hypothesis，fake 后端据此分派
    alias: str                   # reasoner | critic
    system: str
    user: str
    schema: dict[str, Any]
    temperature: float
    data: dict[str, Any] = field(default_factory=dict)   # 渲染提示词用的结构化输入


class LLM(Protocol):
    name: str

    def complete_json(self, call: LLMCall) -> tuple[dict[str, Any], Usage]: ...


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LLMBadOutput("no JSON object in output")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMBadOutput(str(e)) from e


