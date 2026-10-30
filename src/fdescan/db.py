"""只读扫描关系数据库（设计 §3.1–§3.3）。

只发目录查询与聚合统计；为算形态特征读取的少量值只在内存中，不写入目录。
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import hashlib

from sqlalchemy import column, create_engine, func, inspect, select, table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url

from fdescan import features, finalize, slug

DEFAULTS = {"sample_n": 200, "big_table_rows": 10_000_000, "low_cardinality": 20}


def readonly_engine(url: str) -> Engine:
    u = make_url(url)
    if u.get_backend_name() == "sqlite":
        db = u.database or ""
        # SQLite 以只读 URI 打开，写语句在驱动层直接失败
        return create_engine(f"sqlite:///file:{db}?mode=ro&uri=true")
    if u.get_backend_name() == "postgresql":
        return create_engine(url, execution_options={"postgresql_readonly": True})
    return create_engine(url)


def _begin_readonly(conn: Connection) -> None:
    name = conn.dialect.name
    if name == "mysql":
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
    elif name == "mssql":
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")


def _tbl(name: str, schema: str | None, cols: list[str]):
    t = table(name, *[column(c) for c in cols])
    t.schema = schema
    return t


def _column_stats(conn: Connection, t, col: str, rows: int | None, opts: dict[str, Any],
                  allow_enum: bool) -> dict[str, Any]:
    c = column(col)
    src = t
    method = "exact"
    if rows is not None and rows > opts["big_table_rows"]:
        src = select(c).select_from(t).limit(opts["big_table_rows"] // 10).subquery()
        c = src.c[col]
        method = "sampled"
    nn, distinct = conn.execute(select(func.count(c), func.count(func.distinct(c))).select_from(src)).one()
    total = conn.execute(select(func.count()).select_from(src)).scalar() or 0
    values = [r[0] for r in conn.execute(select(c).select_from(src).where(c.is_not(None))
                                         .limit(opts["sample_n"]))]
    out = {"null_rate": round(1 - nn / total, 4) if total else None, "distinct": int(distinct),
           "_values": values, "_method": method}
    if allow_enum and distinct <= opts["low_cardinality"]:
        out["_enum"] = sorted({str(v)[:40] for v in conn.execute(
            select(func.distinct(c)).select_from(src).where(c.is_not(None)).limit(opts["low_cardinality"])).scalars()})
    return out


def scan(url: str, system_hint: str | None = None, name: str | None = None, allow_enum: bool = False,
         schemas: list[str] | None = None, on_connect=None, **opts: Any) -> dict[str, Any]:
    """on_connect：测试钩子，可在原生连接上挂 SQLite authorizer 等。"""
    o = {**DEFAULTS, **opts}
    engine = readonly_engine(url)
    if on_connect is not None:
        from sqlalchemy import event
        event.listen(engine, "connect", lambda dbapi_conn, _: on_connect(dbapi_conn))
    insp = inspect(engine)
    dialect = engine.dialect.name
    perm: list[dict[str, str]] = []
    try:
        schema_names = schemas or [s for s in insp.get_schema_names()
                                   if s not in ("information_schema", "pg_catalog", "sys", "mysql",
                                                "performance_schema")]
    except Exception as e:  # noqa: BLE001
        perm.append({"object": "(schemas)", "error": str(e)[:300]})
        schema_names = [None]
    if dialect == "sqlite":
        schema_names = [None]
    assets: list[dict[str, Any]] = []
    stats_ok = True
    with engine.connect() as conn:
        _begin_readonly(conn)
        for sch in schema_names:
            try:
                names = [(n, "table") for n in insp.get_table_names(schema=sch)] + \
                        [(n, "view") for n in insp.get_view_names(schema=sch)]
            except Exception as e:  # noqa: BLE001
                perm.append({"object": f"{sch}.*", "error": str(e)[:300]})
                continue
            for tname, kind in names:
                if tname.lower().startswith("sqlite_"):        # 数据库内部表，不是业务数据
                    continue
                full = f"{sch}.{tname}" if sch else tname
                try:
                    cols = insp.get_columns(tname, schema=sch)
                except Exception as e:  # noqa: BLE001
                    perm.append({"object": full, "error": str(e)[:300]})
                    continue
                pk = (insp.get_pk_constraint(tname, schema=sch) or {}).get("constrained_columns") or [] if kind == "table" else []
                fks = insp.get_foreign_keys(tname, schema=sch) if kind == "table" else []
                try:
                    tcomment = (insp.get_table_comment(tname, schema=sch) or {}).get("text")
                except NotImplementedError:
                    tcomment = None
                t = _tbl(tname, sch, [c["name"] for c in cols])
                rows: int | None
                if kind == "view" and not o.get("view_stats"):
                    # 视图只列结构：统计要跑底层连接查询，慢且与底表重复
                    rows = None
                else:
                    try:
                        rows = int(conn.execute(select(func.count()).select_from(t)).scalar() or 0)
                    except Exception as e:  # noqa: BLE001
                        perm.append({"object": full, "error": str(e).splitlines()[0][:300]})
                        rows, stats_ok = None, False
                method = "exact" if rows is not None else "none"
                out_cols = []
                for c in cols:
                    ctype = str(c["type"])
                    st: dict[str, Any] = {}
                    if rows is not None:
                        try:
                            st = _column_stats(conn, t, c["name"], rows, o, allow_enum)
                            method = "sampled" if st["_method"] == "sampled" else method
                        except Exception as e:  # noqa: BLE001
                            perm.append({"object": f"{full}.{c['name']}", "error": str(e).splitlines()[0][:300]})
                            stats_ok = False
                    values = st.get("_values", [])
                    shape = features.shape_of(values, ctype)
                    span = None
                    if shape in ("date", "datetime") and rows:
                        try:
                            lo, hi = conn.execute(select(func.min(column(c["name"])), func.max(column(c["name"])))
                                                  .select_from(t)).one()
                            span = features.span_days(lo, hi)
                        except Exception:  # noqa: BLE001
                            span = None
                    ll = features.looks_like(values)
                    col_out = {
                        "name": c["name"], "type": ctype[:80], "nullable": c.get("nullable"),
                        "has_default": c.get("default") is not None, "comment": c.get("comment"),
                        "null_rate": st.get("null_rate"), "distinct": st.get("distinct"),
                        "shape": shape, "looks_like": ll,
                        "low_cardinality": (st["distinct"] <= o["low_cardinality"]) if "distinct" in st else None,
                        "monotonic": features.monotonic(values) if values else None,
                        "date_span_days": span,
                        "sensitive_column": ll in ("phone", "id_card", "email") or features.sensitive_name(c["name"]),
                    }
                    if "_enum" in st and not col_out["sensitive_column"]:
                        from fdescan import guard_hits
                        col_out["enum_values"] = [v for v in st["_enum"] if not guard_hits(v)]
                    out_cols.append(col_out)
                    del values, st          # 取值只在内存中停留
                assets.append({
                    "id": f"a_{slug(full, 60)}", "kind": kind, "schema": sch, "name": tname, "comment": tcomment,
                    "row_count": rows, "primary_key": list(pk), "columns": out_cols,
                    "foreign_keys": [{"columns": fk["constrained_columns"], "ref_table": fk["referred_table"],
                                      "ref_columns": fk["referred_columns"], "declared": True} for fk in fks],
                    "referenced_by": 0, "stats_method": method})
    parsed_url = make_url(url)
    display_name = (name or Path(parsed_url.database or dialect).name)[:120]
    address = (dialect, parsed_url.host, parsed_url.port, parsed_url.database)
    default_id = slug(display_name) + "_" + hashlib.sha256(repr(address).encode()).hexdigest()[:8]
    source = {"id": "src_" + (slug(system_hint or name) if system_hint or name else default_id), "kind": "database",
              "dialect": dialect, "system_hint": system_hint, "name": display_name}
    scope = {"schemas_listed": [s for s in schema_names if s], "tables_listed": len(assets),
             "tables_total": None if perm else len(assets), "permission_errors": perm,
             "stats_available": stats_ok and not perm}
    return finalize(source, scope, assets, "db", allow_enum)
