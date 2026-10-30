"""fde-scan：本地只读扫描器（阶段二详细设计 §3，K58 E-1–E-3）。

产出 fde-catalog/1 目录文件：表名、字段名、类型、统计画像与形态类别。**任何取值都不写入目录、不落盘。**
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

__version__ = "0.1.0"
HERE = Path(__file__).parent


class CatalogInvalid(ValueError):
    pass


@lru_cache
def _schema() -> dict[str, Any]:
    return json.loads((HERE / "catalog_schema.json").read_text(encoding="utf-8"))


@lru_cache
def _guard_rules() -> list[tuple[str, re.Pattern[str]]]:
    data = yaml.safe_load((HERE / "guard_rules.yaml").read_text(encoding="utf-8"))
    return [(r["id"], re.compile(r["pattern"])) for level in ("sensitive", "uncertain") for r in data.get(level, [])]


def guard_hits(text: str | None) -> list[str]:
    if not text:
        return []
    return [rid for rid, rx in _guard_rules() if rx.search(text)]


def validate(doc: dict[str, Any]) -> None:
    """按 JSON Schema 校验；任何多出来的字段（例如手工加的 sample_values）都会失败。"""
    try:
        jsonschema.validate(doc, _schema())
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path)
        raise CatalogInvalid(f"{where or '(根)'}: {e.message}") from e
    ids = [a["id"] for a in doc["catalog"]["assets"]]
    if len(ids) != len(set(ids)):
        raise CatalogInvalid("资产 id 重复")


def slug(text: str, n: int = 24) -> str:
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", text or "").strip("_").lower()
    return (s or hashlib.sha1((text or "x").encode()).hexdigest()[:8])[:n]


def finalize(source: dict[str, Any], scope: dict[str, Any], assets: list[dict[str, Any]], method: str,
             allow_enum: bool = False, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """extra：MCP 扫描的附加信息 {"operations", "transport"}，写入 catalog.mcp。"""
    """补 referenced_by、屏蔽敏感注释、生成快照 ID，并做最终校验。"""
    by_name = {a["name"].lower(): a for a in assets}
    for a in assets:
        a["referenced_by"] = 0
    for a in assets:
        for fk in a["foreign_keys"]:
            ref = by_name.get(fk["ref_table"].split(".")[-1].lower())
            if ref is not None:
                ref["referenced_by"] += 1
    redactions = []
    for i, a in enumerate(assets):
        if guard_hits(a.get("comment")):
            redactions.append({"path": f"assets[{i}].comment", "rule": guard_hits(a["comment"])[0]})
            a["comment"] = "[已屏蔽]"
        for j, c in enumerate(a["columns"]):
            if guard_hits(c.get("comment")):
                redactions.append({"path": f"assets[{i}].columns[{j}].comment", "rule": guard_hits(c["comment"])[0]})
                c["comment"] = "[已屏蔽]"
    body = {"format": "fde-catalog/1", "source": source, "scope": scope, "assets": assets}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
    stamp = datetime.now(timezone.utc)
    doc = {"catalog": {
        "format": "fde-catalog/1",
        "snapshot_id": f"snap_{stamp.strftime('%Y%m%d')}_{slug(source.get('system_hint') or source['name'], 16)}_{digest}",
        "scanned_at": stamp.isoformat(timespec="seconds"),
        "scanner": {"version": __version__, "method": method, "allow_enum": allow_enum},
        "source": source, "scope": scope, "assets": assets, "redactions": redactions}}
    if extra is not None:
        doc["catalog"]["mcp"] = extra
    validate(doc)
    return doc


def summarize(doc: dict[str, Any]) -> str:
    """inspect：列出将会离开本机的全部内容。"""
    c = doc["catalog"]
    cols = sum(len(a["columns"]) for a in c["assets"])
    lines = [f"目录 {c['snapshot_id']}（{c['scanner']['method']}，{c['scanned_at']}）",
             f"数据源：{c['source']['name']}　系统提示：{c['source']['system_hint'] or '—'}",
             f"表 / 视图 {len(c['assets'])} 个，列 {cols} 个；权限错误 {len(c['scope']['permission_errors'])} 处；"
             f"屏蔽注释 {len(c['redactions'])} 处；统计可用：{c['scope']['stats_available']}",
             "将会离开本机的内容：表名、列名、类型、注释（已过敏感规则）、行数、空值率、去重数、形态类别、日期跨度天数"
             + ("、低基数枚举值" if c["scanner"].get("allow_enum") else "")
             + ("、MCP 工具的名称 · 说明 · 参数名" if c.get("mcp") else "") + "。不含任何行数据或取值。", ""]
    for a in c["assets"]:
        lines.append(f"- {a['schema'] + '.' if a['schema'] else ''}{a['name']}（{a['row_count'] if a['row_count'] is not None else '?'} 行）："
                     + "、".join(f"{x['name']}:{x['shape'] or x['type']}" for x in a["columns"]))
    for op in (c.get("mcp") or {}).get("operations", []):
        lines.append(f"- 工具 {op['name']}（{op['role']}）：{op['description'] or '—'}；参数 {'、'.join(op['params']) or '—'}")
    return "\n".join(lines)


def to_wire(doc: dict[str, Any], prefix: str) -> dict[str, Any]:
    """目录文件 → 经验服务的 CatalogSummary 片段 ＋ 扫描范围 ＋ 观察证据清单（规格 v0.4 §3.5）。

    证据 ID 由 prefix ＋ 快照 ＋ 对象路径确定性生成，同一文件重复导入得到同一批 ID。
    返回 {"source", "assets", "scope", "evidence": [{id, ref, target, locator, facts, text}]}。
    """
    c = doc["catalog"]
    snap = c["snapshot_id"]
    src = c["source"]
    h = lambda *parts: f"{prefix}_{hashlib.sha1('|'.join([snap, *parts]).encode()).hexdigest()[:16]}"
    sname = src["name"]
    evidence = []

    def ev(eid: str, ref: str, target: str, locator: dict[str, Any], facts: dict[str, Any], text: str) -> str:
        evidence.append({"id": eid, "ref": ref, "target": target, "locator": locator, "facts": facts, "text": text})
        return eid

    src_ev = ev(h("source"), src["id"], "source", {"source_id": src["id"]},
                {"dialect": src["dialect"], "kind": src["kind"]},
                f"数据源 {sname}（{src['dialect'] or src['kind']}），系统提示：{src['system_hint'] or '—'}")
    sc = c["scope"]
    scope_ev = ev(h("scope"), f"{src['id']}#scope", "source", {"source_id": src["id"]},
                  {"tables_listed": sc["tables_listed"], "tables_total": sc["tables_total"],
                   "stats_available": sc["stats_available"], "permission_errors": len(sc["permission_errors"])},
                  f"扫描范围：列出 {sc['tables_listed']} 张表"
                  + ("（完整）" if sc["tables_total"] is not None else "（不完整）")
                  + (f"，{len(sc['permission_errors'])} 处权限错误" if sc["permission_errors"] else ""))
    assets = []
    for a in c["assets"]:
        full = f"{a['schema']}.{a['name']}" if a["schema"] else a["name"]
        spans = [(c["name"], c["date_span_days"]) for c in a["columns"] if c.get("date_span_days")]
        col_names = [c["name"] for c in a["columns"] if not c.get("sensitive_column")]
        a_ev = ev(h("t", full), f"{src['id']}/{full}", "asset", {"source_id": src["id"], "asset": full},
                  {"row_count": a["row_count"], "columns": len(a["columns"]), "primary_key": ",".join(a["primary_key"])},
                  f"{'视图' if a['kind'] == 'view' else '表'} {full}："
                  + (f"约 {a['row_count']} 行，" if a["row_count"] is not None else "")
                  + f"列 {'、'.join(col_names[:14])}{' 等' if len(col_names) > 14 else ''}"
                  + (f"；外键指向 {'、'.join(sorted({f['ref_table'] for f in a['foreign_keys']}))}" if a["foreign_keys"] else "")
                  + (f"；{spans[0][0]} 跨度 {spans[0][1]} 天" if spans else "")
                  + (f"；注释「{a['comment']}」" if a["comment"] else ""))
        cols = []
        for col in a["columns"]:
            c_ev = ev(h("c", full, col["name"]), f"{src['id']}/{full}.{col['name']}", "column",
                      {"source_id": src["id"], "asset": full, "column": col["name"]},
                      {k: col[k] for k in ("type", "null_rate", "distinct", "shape", "date_span_days")},
                      f"表 {full} 的列 {col['name']}：{col['shape'] or col['type']}"
                      + (f"，空值率 {col['null_rate']}" if col["null_rate"] is not None else "")
                      + (f"，{col['distinct']} 个不同取值" if col["distinct"] is not None else "")
                      + (f"，跨度 {col['date_span_days']} 天" if col["date_span_days"] is not None else ""))
            cols.append({**{k: v for k, v in col.items()}, "evidence_id": c_ev})
        fks = []
        for fk in a["foreign_keys"]:
            f_ev = ev(h("f", full, ",".join(fk["columns"]), fk["ref_table"]),
                      f"{src['id']}/{full}.fk({','.join(fk['columns'])}→{fk['ref_table']})", "foreign_key",
                      {"source_id": src["id"], "asset": full, "fk": f"{','.join(fk['columns'])}→{fk['ref_table']}"},
                      {"declared": fk["declared"]},
                      f"外键 {full}.{','.join(fk['columns'])} → {fk['ref_table']}（{'声明' if fk['declared'] else '推断'}）")
            fks.append({**fk, "evidence_id": f_ev})
        assets.append({"id": f"{src['id']}:{a['id']}", "evidence_id": a_ev, "source": src["id"], "kind": a["kind"],
                       "schema": a["schema"], "name": a["name"], "comment": a["comment"], "row_count": a["row_count"],
                       "stats_method": a["stats_method"], "primary_key": a["primary_key"], "columns": cols,
                       "foreign_keys": fks, "referenced_by": a["referenced_by"]})
    # MCP：每个工具一条"系统能做什么"的观察（target=asset，只进追问与报告，不进匹配目录）
    role_label = {"schema": "结构", "read": "查询", "write": "写入 / 操作", "other": "其他"}
    for op in (c.get("mcp") or {}).get("operations", []):
        ev(h("op", op["name"]), f"{src['id']}#tool/{op['name']}", "asset",
           {"source_id": src["id"], "asset": f"tool:{op['name']}"}, {"params": len(op["params"]), "role": op["role"]},
           f"系统 {sname} 提供接口 {op['name']}（{role_label.get(op['role'], op['role'])}）"
           + (f"：{op['description']}" if op["description"] else "")
           + (f"；参数 {'、'.join(op['params'][:8])}" if op["params"] else ""))
    status = "scanned" if sc["tables_total"] is not None and not sc["permission_errors"] else \
             ("permission_denied" if sc["permission_errors"] else "partial")
    scope = {"source_id": src["id"], "status": status,
             "coverage": {"assets_listed": sc["tables_listed"], "assets_total": sc["tables_total"]},
             "stats_available": sc["stats_available"],
             "permission_errors": [e["object"] for e in sc["permission_errors"]], "evidence_id": scope_ev}
    source = {"id": src["id"], "kind": src["kind"], "dialect": src["dialect"], "name": sname,
              "system_hint": src["system_hint"], "evidence_id": src_ev}
    return {"snapshot_id": snap, "source": source, "assets": assets, "scope": scope, "evidence": evidence}
