"""数据库直连（连接串拼装、密码不外泄）与 MCP 扫描（只调结构类工具）。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from fdescan import to_wire, validate
from fdescan.conn import ConnectionInvalid, build_url, describe, display, driver_hint
from fdescan.db import scan
from fdescan.mcp import McpClient, McpError, _Http, scan_mcp

HERE = Path(__file__).parent


def _db(tmp_path) -> Path:
    p = tmp_path / "shop.db"
    c = sqlite3.connect(p)
    c.executescript("""
      CREATE TABLE Customers (CustomerID TEXT PRIMARY KEY, CompanyName TEXT NOT NULL, Phone TEXT);
      CREATE TABLE Orders (OrderID INTEGER PRIMARY KEY, CustomerID TEXT REFERENCES Customers(CustomerID), OrderDate DATE DEFAULT '1996-07-04');
      INSERT INTO Customers VALUES ('ALFKI', 'Alfreds Futterkiste', '030-0074321');
      INSERT INTO Orders VALUES (1, 'ALFKI', '1996-07-04');
    """)
    c.commit()
    return p


def test_build_url_and_describe_never_expose_password():
    u = build_url("postgresql", host="db.local", port="", database="erp", username="ro", password="p@ss:w/rd")
    assert u.drivername == "postgresql+psycopg" and u.port == 5432
    assert "p@ss" not in str(u) and u.password == "p@ss:w/rd"        # str(URL) 默认隐藏密码
    d = describe("postgresql", host="db.local", port="", database="erp", username="ro", password="p@ss:w/rd")
    assert "password" not in d and "p@ss" not in json.dumps(d)
    assert display(d) == "PostgreSQL · ro@db.local:5432/erp"
    with pytest.raises(ConnectionInvalid):
        build_url("postgresql", host="", database="erp")
    with pytest.raises(ConnectionInvalid):
        build_url("db2", host="x", database="y")


def test_driver_hint_points_to_extra():
    assert driver_hint("sqlite") is None
    hint = driver_hint("oracle")
    assert hint is None or 'fde-scan[oracle]' in hint


def test_direct_url_scan_sqlite(tmp_path):
    doc = scan(build_url("sqlite", database=str(_db(tmp_path))), system_hint="CRM", name="shop")
    names = {a["name"] for a in doc["catalog"]["assets"]}
    assert names == {"Customers", "Orders"}
    assert "Alfreds" not in json.dumps(doc) and "1996-07-04" not in json.dumps(doc)


def _stdio_cmd(tmp_path):
    log = tmp_path / "calls.log"
    return [sys.executable, str(HERE / "fake_mcp_server.py"), str(_db(tmp_path)), str(log)], log


def test_mcp_stdio_scan_reads_structure_only(tmp_path):
    cmd, log = _stdio_cmd(tmp_path)
    audit: list[str] = []
    doc = scan_mcp(command=cmd, system_hint="订单系统", audit=audit)
    validate(doc)
    c = doc["catalog"]
    assert c["scanner"]["method"] == "mcp" and c["source"]["kind"] == "mcp" and c["source"]["dialect"] == "mcp:sqlite-demo"
    tables = {a["name"]: a for a in c["assets"]}
    assert set(tables) == {"Customers", "Orders"}
    assert [x["name"] for x in tables["Customers"]["columns"]] == ["CustomerID", "CompanyName", "Phone"]
    assert tables["Customers"]["primary_key"] == ["CustomerID"]
    assert tables["Customers"]["columns"][2]["sensitive_column"] is True
    # 查询 / 写入工具从不被调用；默认值（1996-07-04）、任何行数据都不进目录
    called = set(log.read_text().split())
    assert called == {"list_tables", "describe_table"} and set(audit) == called
    raw = json.dumps(doc, ensure_ascii=False)
    assert "Alfreds" not in raw and "1996-07-04" not in raw and "SHOULD NOT" not in raw
    ops = {o["name"]: o["role"] for o in c["mcp"]["operations"]}
    assert ops == {"list_tables": "schema", "describe_table": "schema", "read_query": "read", "create_order": "write"}
    # 工具成为"系统能做什么"的观察证据
    w = to_wire(doc, "ev")
    tool_ev = [e for e in w["evidence"] if e["locator"].get("asset", "").startswith("tool:")]
    assert len(tool_ev) == 4 and any("create_order" in e["text"] and "写入" in e["text"] for e in tool_ev)
    assert {a["name"] for a in w["assets"]} == {"Customers", "Orders"}


def test_client_refuses_non_schema_tools(tmp_path):
    cmd, log = _stdio_cmd(tmp_path)
    from fdescan.mcp import _Stdio
    cl = McpClient(_Stdio(cmd))
    try:
        cl.initialize()
        with pytest.raises(McpError):
            cl.call("read_query", {"query": "select * from Customers"})
    finally:
        cl.close()
    assert not log.exists() or "read_query" not in log.read_text()


def test_mcp_http_with_sse_and_session(tmp_path):
    tables = {"inventory": [{"column_name": "sku", "data_type": "text"}, {"column_name": "qty", "data_type": "integer"}]}
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            return httpx.Response(200)
        m = json.loads(req.content)
        seen.append((m.get("method"), req.headers.get("mcp-session-id"), req.headers.get("authorization")))
        if "id" not in m:
            return httpx.Response(202)
        if m["method"] == "initialize":
            res = {"protocolVersion": "2025-03-26", "capabilities": {"resources": {}, "tools": {}}, "serverInfo": {"name": "pg"}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": m["id"], "result": res}, headers={"mcp-session-id": "s1"})
        if m["method"] == "tools/list":
            res = {"tools": [{"name": "query", "description": "Run a read-only SQL query", "inputSchema": {"properties": {"sql": {}}}}]}
        elif m["method"] == "resources/list":
            res = {"resources": [{"uri": "postgres://h/inventory/schema", "name": '"inventory" database schema', "mimeType": "application/json"}]}
        elif m["method"] == "resources/read":
            res = {"contents": [{"uri": m["params"]["uri"], "text": json.dumps(tables["inventory"])}]}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": m["id"], "error": {"message": "no"}})
        body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': res})}\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    doc = scan_mcp(url="http://mcp.local/mcp", headers={"Authorization": "Bearer t0k"}, system_hint="库存",
                   http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    c = doc["catalog"]
    assert [a["name"] for a in c["assets"]] == ["inventory"] and c["mcp"]["transport"] == "http"
    assert [x["name"] for x in c["assets"][0]["columns"]] == ["sku", "qty"]
    assert all(s == "s1" for meth, s, _ in seen if meth not in ("initialize",))
    assert "t0k" not in json.dumps(doc)
    assert not any(meth == "tools/call" for meth, _, _ in seen)     # 只有查询工具：一个都不调


def test_http_url_must_be_http():
    with pytest.raises(McpError):
        _Http("file:///etc/passwd")
