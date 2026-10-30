"""数据库直连：按类型拼连接串、检查驱动、隐藏密码。

密码只用于本次扫描：拼进 URL 交给 SQLAlchemy，**不写进目录、不写日志、不回显**；
对外展示与存档一律用 describe()（不含密码）。
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import URL


@dataclass(frozen=True)
class DbType:
    key: str
    label: str
    drivername: str
    module: str | None        # 需要的驱动模块；None 表示内置
    extra: str | None         # pip 安装提示：fde-scan[extra]
    default_port: int | None


DB_TYPES: dict[str, DbType] = {t.key: t for t in [
    DbType("postgresql", "PostgreSQL", "postgresql+psycopg", "psycopg", "postgres", 5432),
    DbType("mysql", "MySQL / MariaDB", "mysql+pymysql", "pymysql", "mysql", 3306),
    DbType("mssql", "SQL Server", "mssql+pyodbc", "pyodbc", "mssql", 1433),
    DbType("oracle", "Oracle", "oracle+oracledb", "oracledb", "oracle", 1521),
    DbType("sqlite", "SQLite 文件", "sqlite", None, None, None),
]}


class ConnectionInvalid(ValueError):
    pass


def driver_available(key: str) -> bool:
    t = DB_TYPES[key]
    return t.module is None or importlib.util.find_spec(t.module) is not None


def driver_hint(key: str) -> str | None:
    t = DB_TYPES[key]
    if driver_available(key):
        return None
    return f"本机未安装 {t.label} 驱动：pip install \"fde-scan[{t.extra}]\""


def build_url(key: str, host: str | None = None, port: int | str | None = None, database: str | None = None,
              username: str | None = None, password: str | None = None,
              query: dict[str, str] | None = None) -> URL:
    if key not in DB_TYPES:
        raise ConnectionInvalid(f"不支持的数据库类型：{key}")
    t = DB_TYPES[key]
    if key == "sqlite":
        if not database:
            raise ConnectionInvalid("请填写 SQLite 文件路径")
        return URL.create("sqlite", database=database)
    if not host or not database:
        raise ConnectionInvalid("请填写主机和库名")
    try:
        p = int(port) if port not in (None, "") else t.default_port
    except ValueError as e:
        raise ConnectionInvalid("端口必须是数字") from e
    q = dict(query or {})
    if key == "mssql":
        q.setdefault("driver", "ODBC Driver 18 for SQL Server")
        q.setdefault("TrustServerCertificate", "yes")
    return URL.create(t.drivername, username=username or None, password=password or None, host=host.strip(),
                      port=p, database=database.strip(), query=q)


def describe(key: str, host: str | None = None, port: int | str | None = None, database: str | None = None,
             username: str | None = None, **_: Any) -> dict[str, Any]:
    """可以存档、可以展示的连接描述：不含密码。"""
    t = DB_TYPES.get(key)
    return {"type": key, "label": t.label if t else key, "host": host or None,
            "port": int(port) if str(port or "").isdigit() else (t.default_port if t else None),
            "database": database or None, "username": username or None}


def display(d: dict[str, Any]) -> str:
    if d.get("type") == "sqlite":
        return f"SQLite · {d.get('database')}"
    who = f"{d['username']}@" if d.get("username") else ""
    return f"{d.get('label')} · {who}{d.get('host')}:{d.get('port')}/{d.get('database')}"
