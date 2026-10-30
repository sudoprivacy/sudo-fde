"""通过 MCP Server 读取系统的结构（阶段二扩展，joy 2026-10-26）。

只做两件事，**不读任何业务数据**（K49）：
1. 结构发现：只调用"列出表 / 描述表结构"一类工具，或读取声明为表结构的资源；
   查询、读取、写入类工具（read_query、execute、insert…）一律不调用——白名单之外的工具都不碰。
2. 能力清单：记录服务器提供了哪些工具（名称、说明、参数名），作为"这个系统能做什么"的观察。

传输：stdio（启动本机命令）与 Streamable HTTP（POST JSON-RPC，响应可以是 JSON 或 SSE）。
客户端是最小实现，只用到 initialize、tools/list、tools/call、resources/list、resources/read。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import threading
from typing import Any

import httpx

from fdescan import features, finalize, guard_hits, slug

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "fde-scan", "version": "0.1.0"}
MAX_TABLES = 300

# 结构发现的白名单（工具名，大小写不敏感）
_LIST_TABLES = re.compile(r"^(list|get|show)[_\-]?(all[_\-]?)?tables?$", re.I)
_DESCRIBE = re.compile(r"^(describe[_\-]?table|get[_\-]?table[_\-]?(schema|info|columns|structure)|"
                       r"table[_\-]?(schema|info|columns)|describe|get[_\-]?columns|list[_\-]?columns)$", re.I)
_SCHEMA_RESOURCE = re.compile(r"(/schema$|schema://|table[_\-]?schema)", re.I)


class McpError(RuntimeError):
    pass


# ---------------------------------------------------------------- 传输

class _Stdio:
    def __init__(self, command: str | list[str], env: dict[str, str] | None = None, timeout: float = 30.0):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise McpError("MCP 命令为空")
        self.timeout = timeout
        try:
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env={**os.environ, **(env or {})}, text=True, encoding="utf-8", bufsize=1)
        except OSError as e:
            raise McpError(f"无法启动 MCP 命令：{e.__class__.__name__}") from e

    def send(self, msg: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def recv(self, want_id: int) -> dict[str, Any]:
        assert self.p.stdout is not None
        out: dict[str, Any] = {}

        def read() -> None:
            while True:
                line = self.p.stdout.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except ValueError:
                    continue            # 服务器打到 stdout 的日志，忽略
                if m.get("id") == want_id and ("result" in m or "error" in m):
                    out["m"] = m
                    return

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(self.timeout)
        if "m" not in out:
            raise McpError("MCP 服务器无响应（超时或已退出）")
        return out["m"]

    def request(self, rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.recv(rid)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        try:
            if self.p.stdin:
                self.p.stdin.close()
            self.p.terminate()
            self.p.wait(timeout=3)
        except Exception:  # noqa: BLE001
            self.p.kill()


class _Http:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30.0,
                 client: httpx.Client | None = None):
        if not re.match(r"^https?://", url or ""):
            raise McpError("MCP 地址必须以 http:// 或 https:// 开头")
        self.url, self.session = url, None
        self.http = client or httpx.Client(timeout=timeout)
        self.headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                        **(headers or {})}

    def _post(self, msg: dict[str, Any]) -> httpx.Response:
        h = dict(self.headers)
        if self.session:
            h["Mcp-Session-Id"] = self.session
        try:
            r = self.http.post(self.url, json=msg, headers=h)
        except httpx.HTTPError as e:
            raise McpError(f"连不上 MCP 服务器：{e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise McpError("MCP 服务器拒绝访问（认证失败）")
        if r.status_code >= 400:
            raise McpError(f"MCP 服务器返回 HTTP {r.status_code}")
        self.session = r.headers.get("mcp-session-id") or self.session
        return r

    def request(self, rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        r = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if m.get("id") == rid:
                        return m
            raise McpError("MCP 服务器的事件流里没有响应")
        return r.json()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def close(self) -> None:
        if self.session:
            try:
                self.http.delete(self.url, headers={"Mcp-Session-Id": self.session})
            except httpx.HTTPError:
                pass


class McpClient:
    def __init__(self, transport):
        self.t, self._id = transport, 0
        self.server: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.calls: list[str] = []          # 调用过的工具名，供审计与测试

    def _req(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        m = self.t.request(self._id, method, params or {})
        if "error" in m:
            raise McpError(f"{method} 失败：{str(m['error'].get('message', ''))[:120]}")
        return m.get("result") or {}

    def initialize(self) -> None:
        r = self._req("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO})
        self.server, self.capabilities = r.get("serverInfo") or {}, r.get("capabilities") or {}
        self.t.notify("notifications/initialized")

    def _paged(self, method: str, key: str) -> list[dict[str, Any]]:
        items, cursor = [], None
        for _ in range(20):
            r = self._req(method, {"cursor": cursor} if cursor else {})
            items += r.get(key) or []
            cursor = r.get("nextCursor")
            if not cursor:
                break
        return items

    def tools(self) -> list[dict[str, Any]]:
        return self._paged("tools/list", "tools") if "tools" in self.capabilities or not self.capabilities else []

    def resources(self) -> list[dict[str, Any]]:
        if "resources" not in self.capabilities:
            return []
        try:
            return self._paged("resources/list", "resources")
        except McpError:
            return []

    def call(self, name: str, args: dict[str, Any]) -> str:
        if not (_LIST_TABLES.match(name) or _DESCRIBE.match(name)):
            raise McpError(f"拒绝调用非结构类工具：{name}")      # 第二道闸：白名单之外的工具永不调用
        self.calls.append(name)
        r = self._req("tools/call", {"name": name, "arguments": args})
        if r.get("isError"):
            raise McpError(f"{name} 返回错误")
        return "\n".join(c.get("text", "") for c in r.get("content") or [] if c.get("type") == "text")

    def read(self, uri: str) -> str:
        r = self._req("resources/read", {"uri": uri})
        return "\n".join(c.get("text", "") for c in r.get("contents") or [] if "text" in c)

    def close(self) -> None:
        self.t.close()


# ---------------------------------------------------------------- 解析

def _loads(text: str) -> Any:
    text = (text or "").strip()
    for fn in (json.loads, ast.literal_eval):
        try:
            return fn(text)
        except (ValueError, SyntaxError, TypeError, MemoryError):
            continue
    return None


def _table_names(text: str) -> list[str]:
    data = _loads(text)
    names: list[str] = []
    if isinstance(data, dict):
        data = data.get("tables") or data.get("result") or list(data.values())[0] if data else []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str):
                names.append(x)
            elif isinstance(x, dict):
                v = x.get("name") or x.get("table_name") or x.get("TABLE_NAME") or x.get("table") or \
                    (list(x.values())[0] if len(x) == 1 else None)
                if isinstance(v, str):
                    names.append(v)
    else:                                   # 纯文本：一行一个表名
        names = [ln.strip(" -*•\t") for ln in (text or "").splitlines() if re.match(r"^[\s\-*•]*[\w.$]+\s*$", ln)]
    return list(dict.fromkeys(n for n in names if n and not n.lower().startswith("sqlite_")))[:MAX_TABLES]


_NAME_KEYS = ("name", "column_name", "COLUMN_NAME", "Field", "field", "column")
_TYPE_KEYS = ("type", "data_type", "DATA_TYPE", "Type", "column_type", "datatype")


def _columns(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """把描述表结构的返回解析成列；只取列名、类型、可空、主键，**默认值等任何取值都丢弃**。"""
    data = _loads(text)
    if isinstance(data, dict):
        data = data.get("columns") or data.get("result") or []
    cols, pk = [], []
    if isinstance(data, list):
        for x in data:
            if not isinstance(x, dict):
                continue
            name = next((x[k] for k in _NAME_KEYS if isinstance(x.get(k), str)), None)
            if not name:
                continue
            typ = next((str(x[k]) for k in _TYPE_KEYS if x.get(k) is not None), "unknown")
            nullable = x.get("nullable", x.get("is_nullable", x.get("Null")))
            if "notnull" in x:
                nullable = not bool(x["notnull"])
            if isinstance(nullable, str):
                nullable = nullable.upper() in ("YES", "Y", "TRUE")
            if x.get("pk") or x.get("primary_key") or str(x.get("Key", "")).upper() == "PRI":
                pk.append(name)
            cols.append({"name": name, "type": typ[:80], "nullable": nullable if isinstance(nullable, bool) else None})
    return cols, pk


def _arg_for(tool: dict[str, Any], table: str) -> dict[str, Any]:
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    req = (tool.get("inputSchema") or {}).get("required") or []
    for k in [*req, *props]:
        if re.search(r"table|name", k, re.I):
            return {k: table}
    return {(req or list(props) or ["table_name"])[0]: table}


def _asset(table: str, cols: list[dict[str, Any]], pk: list[str]) -> dict[str, Any]:
    sch, _, name = table.rpartition(".")
    return {"id": f"a_{slug(table, 60)}", "kind": "table", "schema": sch or None, "name": name, "comment": None,
            "row_count": None, "primary_key": pk, "referenced_by": 0, "stats_method": "none", "foreign_keys": [],
            "columns": [{"name": c["name"], "type": c["type"], "nullable": c["nullable"], "has_default": False,
                         "comment": None, "null_rate": None, "distinct": None, "shape": features.shape_of([], c["type"]),
                         "looks_like": None, "low_cardinality": None, "monotonic": None, "date_span_days": None,
                         "sensitive_column": features.sensitive_name(c["name"])} for c in cols]}


def _operation(tool: dict[str, Any], role: str) -> dict[str, Any]:
    desc = re.sub(r"\s+", " ", (tool.get("description") or "")).strip()[:300]
    if guard_hits(desc):
        desc = "[已屏蔽]"
    params = list(((tool.get("inputSchema") or {}).get("properties") or {}).keys())[:20]
    return {"name": str(tool.get("name"))[:120], "description": desc or None, "params": params, "role": role}


def _role(name: str) -> str:
    if _LIST_TABLES.match(name) or _DESCRIBE.match(name):
        return "schema"
    if re.search(r"(query|select|read|search|fetch|get|list|find|export)", name, re.I):
        return "read"
    if re.search(r"(create|insert|update|delete|write|execute|exec|run|set|add|remove|send)", name, re.I):
        return "write"
    return "other"


# ---------------------------------------------------------------- 扫描

def scan_mcp(*, command: str | list[str] | None = None, url: str | None = None, env: dict[str, str] | None = None,
             headers: dict[str, str] | None = None, system_hint: str | None = None, name: str | None = None,
             timeout: float = 30.0, http_client: httpx.Client | None = None,
             audit: list[str] | None = None) -> dict[str, Any]:
    """audit：传入一个列表，会追加本次调用过的工具名（审计与测试用，不写入目录）。"""
    if bool(command) == bool(url):
        raise McpError("command 与 url 二选一")
    transport = _Stdio(command, env, timeout) if command else _Http(url, headers, timeout, http_client)
    client = McpClient(transport)
    perm: list[dict[str, str]] = []
    assets: list[dict[str, Any]] = []
    try:
        client.initialize()
        tools = client.tools()
        resources = client.resources()
        # 1) 结构资源（如 postgres://…/表名/schema）
        for r in resources:
            uri = r.get("uri") or ""
            if not _SCHEMA_RESOURCE.search(uri) or len(assets) >= MAX_TABLES:
                continue
            table = r.get("name") or uri.rstrip("/").rsplit("/", 2)[-2]
            table = re.sub(r"\s*(database\s+)?schema$", "", str(table), flags=re.I).strip().strip('"')
            try:
                cols, pk = _columns(client.read(uri))
            except McpError as e:
                perm.append({"object": uri[:200], "error": str(e)[:300]})
                continue
            if cols:
                assets.append(_asset(table, cols, pk))
        # 2) 列表工具 ＋ 描述工具
        lister = next((t for t in tools if _LIST_TABLES.match(t.get("name", ""))), None)
        describer = next((t for t in tools if _DESCRIBE.match(t.get("name", ""))), None)
        if lister and not assets:
            try:
                tables = _table_names(client.call(lister["name"], {}))
            except McpError as e:
                perm.append({"object": lister["name"], "error": str(e)[:300]})
                tables = []
            for tb in tables:
                cols, pk = [], []
                if describer:
                    try:
                        cols, pk = _columns(client.call(describer["name"], _arg_for(describer, tb)))
                    except McpError as e:
                        perm.append({"object": tb[:200], "error": str(e)[:300]})
                assets.append(_asset(tb, cols, pk))
        operations = [_operation(t, _role(t.get("name", ""))) for t in tools][:200]
        server = client.server
    finally:
        client.close()
    sname = name or server.get("name") or "mcp"
    source = {"id": f"src_{slug(system_hint or sname)}", "kind": "mcp", "dialect": f"mcp:{slug(server.get('name') or 'server', 24)}",
              "system_hint": system_hint, "name": sname[:120]}
    scope = {"schemas_listed": sorted({a["schema"] for a in assets if a["schema"]}), "tables_listed": len(assets),
             "tables_total": None if perm or not assets else len(assets), "permission_errors": perm,
             "stats_available": False}
    doc = finalize(source, scope, assets, "mcp", extra={"operations": operations,
                                                       "transport": "stdio" if command else "http"})
    if audit is not None:
        audit.extend(client.calls)
    return doc
