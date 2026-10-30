"""目录预处理（阶段二设计 §5.1，规格 §6.5）：命名归一、外键推断、表角色。全部确定性，可重复。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any

import yaml

SEM_FILE = Path(__file__).with_name("semantics.yaml")
INFERRED_FK_CAP = 0.7          # 推断外键分数上限（规格 §6.5，A-11）


@dataclass(frozen=True)
class Vocab:
    version: int
    entities: dict[str, dict[str, Any]]
    columns: dict[str, dict[str, Any]]
    lookup_entity: dict[str, list[str]]
    lookup_column: dict[str, list[str]]
    noise: frozenset[str]
    as_columns: dict[str, list[str]]

    def all_labels(self) -> list[str]:
        return list(self.entities) + list(self.columns)

    def display(self, label: str) -> str:
        base = label.removeprefix("new:")
        if base.endswith("_ref") and base[:-4] in self.entities:
            return f"{self.entities[base[:-4]]['zh']}（引用）"
        return (self.entities.get(base) or self.columns.get(base) or {}).get("zh", base)

    def prompt_list(self) -> str:
        ent = "；".join(f"{k}={v['zh']}" for k, v in self.entities.items())
        col = "；".join(f"{k}={v['zh']}" for k, v in self.columns.items())
        return f"【实体标签】{ent}\n【字段标签】{col}\n【引用列】<实体标签>_ref，例如 supplier_ref"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@lru_cache
def vocab() -> Vocab:
    d = yaml.safe_load(SEM_FILE.read_text(encoding="utf-8"))
    le: dict[str, list[str]] = {}
    lc: dict[str, list[str]] = {}
    for label, spec in d["entities"].items():
        for w in map(str, [label, *spec.get("en", []), *spec.get("py", [])]):
            lst = le.setdefault(_norm(w), [])
            if label not in lst:
                lst.append(label)
    for label, spec in d["columns"].items():
        for w in map(str, [label, *spec.get("en", []), *spec.get("py", [])]):
            lst = lc.setdefault(_norm(w), [])
            if label not in lst:
                lst.append(label)
    return Vocab(d["version"], d["entities"], d["columns"], le, lc, frozenset(d.get("noise_tokens", [])),
                 d.get("realized_by_columns") or {})


def tokens(name: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    parts = [p.lower() for p in re.split(r"[^A-Za-z0-9]+|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", s) if p]
    return parts


def _singular(t: str) -> str:
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("ses") or t.endswith("xes"):
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


@dataclass
class Label:
    label: str
    confidence: float
    source: str                     # lexicon | llm | rule
    alt: list[str] = field(default_factory=list)


def label_table(name: str) -> Label | None:
    v = vocab()
    whole = _norm(name)
    for key in (whole, _singular(whole)):
        if key in v.lookup_entity:
            ls = v.lookup_entity[key]
            return Label(ls[0], 0.95 if len(ls) == 1 else 0.6, "lexicon", ls[1:])
    toks = [t for t in tokens(name) if t not in v.noise]
    hits: list[str] = []
    for t in reversed(toks):                    # 越靠后的 token 越像中心词（order_details → details）
        for key in (t, _singular(t)):
            for lab in v.lookup_entity.get(key, []):
                if lab not in hits:
                    hits.append(lab)
    if not hits:
        return None
    conf = 0.85 if len(toks) == 1 else 0.7
    return Label(hits[0], conf if len(hits) == 1 else conf - 0.2, "lexicon", hits[1:3])


def label_column(name: str, table_label: str | None, is_pk: bool, shape: str | None) -> Label | None:
    v = vocab()
    whole = _norm(name)
    if whole in v.lookup_column:
        ls = v.lookup_column[whole]
        lab = ls[0]
        if lab == "identifier" and not is_pk:
            pass
        else:
            return Label(lab, 0.9 if len(ls) == 1 else 0.6, "lexicon", ls[1:])
    toks = [t for t in tokens(name) if t not in v.noise]
    # <实体>ID / <实体>_dm / <实体>_bh → 引用列或主键
    if toks and toks[-1] in ("id", "code", "no", "dm", "bh", "key", "num", "number") and len(toks) >= 2:
        ent_key = _norm("".join(toks[:-1]))
        ents = v.lookup_entity.get(ent_key) or v.lookup_entity.get(_singular(ent_key)) or []
        if ents:
            if is_pk or ents[0] == table_label:
                return Label("identifier", 0.9, "rule")
            return Label(f"{ents[0]}_ref", 0.85, "rule", [f"{e}_ref" for e in ents[1:2]])
    if whole in ("id",) or (is_pk and toks and toks[-1] in ("id", "code", "dm", "bh")):
        return Label("identifier", 0.9, "rule")
    for t in reversed(toks):
        for lab in v.lookup_column.get(t, []):
            if lab == "identifier" and not is_pk:
                continue
            return Label(lab, 0.6, "lexicon")
    if shape in ("date", "datetime"):
        return Label("event_time", 0.4, "rule")
    return None


# ---------- 画像 ----------

def profile(catalog: dict[str, Any]) -> dict[str, Any]:
    """返回 {assets: {asset_id: {...标签、角色、外键...}}}；只用名字与统计，不碰任何值。"""
    assets = catalog["assets"]
    by_name: dict[str, dict[str, Any]] = {}
    for a in assets:
        by_name.setdefault(a["name"].lower(), a)
    out: dict[str, dict[str, Any]] = {}
    for a in assets:
        tl = label_table(a["name"])
        cols = {}
        for c in a["columns"]:
            cl = label_column(c["name"], tl.label if tl else None, c["name"] in a["primary_key"], c.get("shape"))
            cols[c["name"]] = {"label": cl.label if cl else None, "confidence": cl.confidence if cl else 0.0,
                               "source": cl.source if cl else None, "alt": cl.alt if cl else [],
                               "evidence_id": c["evidence_id"], "shape": c.get("shape"),
                               "date_span_days": c.get("date_span_days"), "sensitive": bool(c.get("sensitive_column"))}
        out[a["id"]] = {"name": a["name"], "source": a["source"], "evidence_id": a["evidence_id"],
                        "kind": a.get("kind", "table"),
                        "row_count": a.get("row_count"), "primary_key": a["primary_key"],
                        "label": tl.label if tl else None, "confidence": tl.confidence if tl else 0.0,
                        "label_source": tl.source if tl else None, "alt": tl.alt if tl else [],
                        "columns": cols, "fks": [], "referenced_by": 0}
    # 外键：声明的 1.0；推断的 ≤ INFERRED_FK_CAP，保持"推断"身份
    name_to_id = {a["name"].lower(): a["id"] for a in assets}
    pk_index: dict[str, str] = {}
    for a in assets:
        if len(a["primary_key"]) == 1:
            pk_index.setdefault(_norm(a["primary_key"][0]), a["id"])
    for a in assets:
        declared = set()
        for fk in a["foreign_keys"]:
            ref = name_to_id.get(fk["ref_table"].split(".")[-1].lower())
            if ref:
                out[a["id"]]["fks"].append({"columns": fk["columns"], "ref": ref, "declared": True, "score": 1.0,
                                            "evidence_id": fk["evidence_id"]})
                declared.update(c.lower() for c in fk["columns"])
        for c in a["columns"]:
            if c["name"].lower() in declared or c["name"] in a["primary_key"]:
                continue
            ref = None
            hit = pk_index.get(_norm(c["name"]))
            if hit and hit != a["id"]:
                ref = hit
            else:
                lab = out[a["id"]]["columns"][c["name"]]["label"] or ""
                if lab.endswith("_ref"):
                    ent = lab[:-4]
                    ref = next((aid for aid, p in out.items() if p["label"] == ent and aid != a["id"]), None)
            if ref:
                out[a["id"]]["fks"].append({"columns": [c["name"]], "ref": ref, "declared": False,
                                            "score": INFERRED_FK_CAP, "evidence_id": c["evidence_id"]})
    for aid, p in out.items():
        for fk in p["fks"]:
            out[fk["ref"]]["referenced_by"] += 1
    # 表角色
    rows = [p["row_count"] for p in out.values() if p["row_count"]]
    med = median(rows) if rows else 0
    for aid, p in out.items():
        n_cols = len(p["columns"])
        has_time = any(c["shape"] in ("date", "datetime") or (c["label"] or "").endswith(("_date", "_time"))
                       for c in p["columns"].values())
        refs_out = len({fk["ref"] for fk in p["fks"]})
        name = p["name"].lower()
        if p["kind"] == "view":
            role = "view"                       # 派生数据：可作证据，但对应概念时让位于底表
        elif any(k in name for k in ("log", "audit", "history", "rz")) and p["referenced_by"] == 0:
            role = "log"
        elif refs_out >= 2 and len(p["primary_key"]) > 1:
            role = "event" if has_time else "detail"
        elif has_time and refs_out >= 1 and (p["row_count"] or 0) >= med:
            role = "event"
        elif n_cols <= 4 and p["referenced_by"] >= 1 and (p["row_count"] or 0) <= 200:
            role = "dictionary"
        elif refs_out >= 2 and p["referenced_by"] == 0:
            role = "detail"
        else:
            role = "entity"                     # 主数据即使有两个外键（如商品 → 供应商、品类）也是实体
        p["role"] = role
    return {"assets": out}


ROLE_FACTOR = {"entity": 1.0, "event": 1.0, "detail": 0.9, "dictionary": 0.8, "log": 0.3, "view": 0.6}
