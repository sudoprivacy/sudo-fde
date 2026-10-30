"""K49 输入闸（规格 §9.1 §9.2）：在持久化与模型调用之前，对请求里所有字符串递归检查。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

RULES_FILE = Path(__file__).with_name("guard_rules.yaml")


@dataclass(frozen=True)
class Hit:
    path: str
    rule_id: str
    label: str
    level: str          # sensitive | uncertain


@lru_cache
def _rules() -> list[tuple[str, str, str, re.Pattern[str]]]:
    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    out = []
    for level in ("sensitive", "uncertain"):
        for r in data.get(level, []):
            out.append((level, r["id"], r["label"], re.compile(r["pattern"])))
    return out


def _walk(node: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            # 键本身也可携带文本
            if isinstance(k, str):
                yield f"{path}.<key>", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


# 不检查的结构字段：ID、枚举等由 schema 约束、不承载自由文本的位置
_SKIP_SUFFIXES = (".id", ".call_id", ".workspace_id", ".artifact_id", ".question_id",
                  ".kind", ".capability", ".sensitivity", ".location", ".decision",
                  ".reason_code", ".snapshot_id", ".decided_at",
                  # v1.1 revise / corrections：证据 ID 与修订路径是结构标识（随机 ID 可能含长串数字）
                  ".evidence_id", ".instruction_evidence_id", ".path", ".mode", ".applied_at")


def scan_text(text: str) -> list[tuple[str, str, str]]:
    return [(level, rid, label) for level, rid, label, rx in _rules() if rx.search(text)]


# 目录里的结构标识（表名、列名、类型等）是 K49 允许出域的元数据，不是自由文本；注释与枚举值照常检查
_CATALOG_STRUCT = re.compile(r"\.(catalog|scan_scope)(?:\..*)?(\.(name|type|ref_table|schema|dialect|kind|shape|looks_like|"
                             r"source|source_id|status|id|evidence_id|snapshot_id|stats_method|system_ref)"
                             r"|\.(primary_key|columns|ref_columns|permission_errors|unmapped_recommended_systems|waived_systems)\[\d+\])$")


def scan(payload: Any, root: str = "$") -> list[Hit]:
    hits: list[Hit] = []
    for path, text in _walk(payload, root):
        # The SaaS BFF constructs these source locators from validated catalogs and
        # document IDs. Numeric identifiers are metadata, while text is still scanned.
        if re.fullmatch(r"\$\.context\.evidence\[\d+\]\.(ref|observation\.locator\.(source_id|asset|column|fk))", path):
            continue
        if path.endswith(_SKIP_SUFFIXES) or "evidence_ids[" in path or ".evidence[" in path and path.endswith("]"):
            continue
        if _CATALOG_STRUCT.search(path):
            continue
        for level, rid, label in scan_text(text):
            hits.append(Hit(path=path, rule_id=rid, label=label, level=level))
    return hits


def verdict(hits: list[Hit]) -> str:
    if any(h.level == "sensitive" for h in hits):
        return "sensitive"
    if hits:
        return "uncertain"
    return "clean"
