"""fde-scan db / ddl / inspect。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fdescan import CatalogInvalid, summarize, validate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fde-scan", description="本地只读扫描器：产出只含元数据的目录文件")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("db", help="只读扫描数据库")
    d.add_argument("--url", required=True, help="SQLAlchemy URL，建议使用只读账号")
    d.add_argument("--system-hint", help="这个库属于哪个业务系统，例如 ERP / 采购系统")
    d.add_argument("--name")
    d.add_argument("--schema", action="append", help="只扫描指定 schema，可重复")
    d.add_argument("--allow-enum", action="store_true", help="输出低基数列的取值（默认不输出，K58 E-2）")
    d.add_argument("--out", required=True)
    g = sub.add_parser("ddl", help="从 DDL 文件生成目录（无统计）")
    g.add_argument("file")
    g.add_argument("--dialect", help="mysql / postgres / tsql / oracle / sqlite …")
    g.add_argument("--system-hint")
    g.add_argument("--name")
    g.add_argument("--out", required=True)
    m = sub.add_parser("mcp", help="通过 MCP Server 读取结构（只调列表 / 描述类工具）")
    mg = m.add_mutually_exclusive_group(required=True)
    mg.add_argument("--stdio", help="启动 MCP Server 的命令，例如 \"uvx mcp-server-sqlite --db-path a.db\"")
    mg.add_argument("--url", help="Streamable HTTP 地址，例如 http://localhost:8000/mcp")
    m.add_argument("--header", action="append", default=[], help="HTTP 头，形如 \"Authorization: Bearer …\"，可重复")
    m.add_argument("--system-hint")
    m.add_argument("--name")
    m.add_argument("--out", required=True)
    i = sub.add_parser("inspect", help="查看目录里会离开本机的全部内容")
    i.add_argument("file")
    a = ap.parse_args(argv)

    if a.cmd == "inspect":
        doc = json.loads(Path(a.file).read_text(encoding="utf-8"))
        try:
            validate(doc)
        except CatalogInvalid as e:
            print(f"目录不合格：{e}", file=sys.stderr)
            return 2
        print(summarize(doc))
        return 0
    if a.cmd == "db":
        from fdescan.db import scan
        doc = scan(a.url, system_hint=a.system_hint, name=a.name, allow_enum=a.allow_enum, schemas=a.schema)
    elif a.cmd == "mcp":
        from fdescan.mcp import scan_mcp
        headers = dict(h.split(":", 1) for h in a.header)
        doc = scan_mcp(command=a.stdio, url=a.url, headers={k.strip(): v.strip() for k, v in headers.items()},
                       system_hint=a.system_hint, name=a.name)
    else:
        from fdescan.ddl import parse
        doc = parse(Path(a.file).read_text(encoding="utf-8"), dialect=a.dialect, system_hint=a.system_hint,
                    name=a.name or Path(a.file).stem)
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summarize(doc))
    print(f"\n已写入 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
