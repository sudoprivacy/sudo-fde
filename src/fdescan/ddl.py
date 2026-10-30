"""DDL 导入（设计 §3.1 M2）：CREATE TABLE / COMMENT ON / ALTER TABLE ADD FOREIGN KEY → 同格式目录，统计全空。"""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from fdescan import finalize, slug


def _ident(node) -> str:
    return node.name if hasattr(node, "name") else str(node)


def _ref(reference: exp.Reference) -> tuple[str, list[str]]:
    sch = reference.this
    tbl = sch.this if isinstance(sch, exp.Schema) else sch
    cols = [_ident(e) for e in sch.expressions] if isinstance(sch, exp.Schema) else []
    return tbl.name, cols


def _shape_from_type(t: str) -> str | None:
    from fdescan.features import shape_of
    return shape_of([], t)


def parse(sql: str, dialect: str | None = None, system_hint: str | None = None,
          name: str | None = None) -> dict[str, Any]:
    stmts = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    tables: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for st in stmts:
        if isinstance(st, exp.Create) and isinstance(st.this, exp.Schema) and (st.args.get("kind") or "").upper() in ("TABLE", ""):
            tnode = st.this.this
            key = tnode.name.lower()
            t = {"id": f"a_{slug(tnode.name, 60)}", "kind": "table", "schema": tnode.db or None, "name": tnode.name,
                 "comment": None, "row_count": None, "primary_key": [], "columns": [], "foreign_keys": [],
                 "referenced_by": 0, "stats_method": "none"}
            for e in st.this.expressions:
                if isinstance(e, exp.ColumnDef):
                    ctype = e.args["kind"].sql(dialect=dialect) if e.args.get("kind") else "unknown"
                    col = {"name": e.name, "type": ctype[:80], "nullable": True, "has_default": False,
                           "comment": None, "null_rate": None, "distinct": None, "shape": _shape_from_type(ctype),
                           "looks_like": None, "low_cardinality": None, "monotonic": None, "date_span_days": None,
                           "sensitive_column": __import__("fdescan.features", fromlist=["x"]).sensitive_name(e.name)}
                    for cc in e.args.get("constraints") or []:
                        k = cc.args.get("kind")
                        if isinstance(k, exp.NotNullColumnConstraint):
                            col["nullable"] = bool(k.args.get("allow_null"))
                        elif isinstance(k, exp.PrimaryKeyColumnConstraint):
                            t["primary_key"].append(e.name)
                            col["nullable"] = False
                        elif isinstance(k, exp.DefaultColumnConstraint):
                            col["has_default"] = True
                        elif isinstance(k, exp.CommentColumnConstraint):
                            col["comment"] = k.this.name if hasattr(k.this, "name") else str(k.this)
                        elif isinstance(k, exp.Reference):
                            rt, rc = _ref(k)
                            t["foreign_keys"].append({"columns": [e.name], "ref_table": rt, "ref_columns": rc,
                                                      "declared": True})
                    t["columns"].append(col)
                elif isinstance(e, exp.PrimaryKey):
                    t["primary_key"] += [_ident(x) for x in e.expressions]
                elif isinstance(e, (exp.ForeignKey, exp.Constraint)):
                    for fk in ([e] if isinstance(e, exp.ForeignKey) else e.find_all(exp.ForeignKey)):
                        rt, rc = _ref(fk.args["reference"])
                        t["foreign_keys"].append({"columns": [_ident(x) for x in fk.expressions], "ref_table": rt,
                                                  "ref_columns": rc, "declared": True})
                    if isinstance(e, exp.Constraint):
                        for pk in e.find_all(exp.PrimaryKey):
                            t["primary_key"] += [_ident(x) for x in pk.expressions]
            for prop in (st.args.get("properties") or exp.Properties()).expressions:
                if isinstance(prop, exp.SchemaCommentProperty):
                    t["comment"] = prop.this.name
            tables[key] = t
            order.append(key)
        elif isinstance(st, exp.Comment) and st.this is not None:
            kind = (st.args.get("kind") or "").upper()
            text = st.expression.name if st.expression is not None else None
            if kind == "TABLE":
                t = tables.get(st.this.name.lower())
                if t:
                    t["comment"] = text
            elif kind == "COLUMN":
                col_node = st.this
                t = tables.get((col_node.table or "").lower())
                if t:
                    for c in t["columns"]:
                        if c["name"].lower() == col_node.name.lower():
                            c["comment"] = text
        elif isinstance(st, exp.Alter) and st.this is not None:
            t = tables.get(st.this.name.lower())
            if t:
                for fk in st.find_all(exp.ForeignKey):
                    rt, rc = _ref(fk.args["reference"])
                    t["foreign_keys"].append({"columns": [_ident(x) for x in fk.expressions], "ref_table": rt,
                                              "ref_columns": rc, "declared": True})
    assets = [tables[k] for k in order]
    for a in assets:   # 去重（行内 REFERENCES 与表级 FOREIGN KEY 可能重复声明）
        seen, uniq = set(), []
        for fk in a["foreign_keys"]:
            key = (tuple(fk["columns"]), fk["ref_table"].lower())
            if key not in seen:
                seen.add(key)
                uniq.append(fk)
        a["foreign_keys"] = uniq
        a["primary_key"] = list(dict.fromkeys(a["primary_key"]))
    src_name = name or system_hint or "ddl"
    source = {"id": f"src_{slug(src_name)}", "kind": "ddl", "dialect": dialect, "system_hint": system_hint,
              "name": src_name}
    scope = {"schemas_listed": sorted({a["schema"] for a in assets if a["schema"]}), "tables_listed": len(assets),
             "tables_total": len(assets), "permission_errors": [], "stats_available": False}
    return finalize(source, scope, assets, "ddl")
