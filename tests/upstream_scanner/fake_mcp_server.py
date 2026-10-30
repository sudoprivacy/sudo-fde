"""测试用的最小 MCP 服务器（stdio）：仿照常见的 SQLite MCP Server，暴露 list_tables / describe_table /
read_query / create_order 四个工具。每次 tools/call 都把工具名追加到 CALL_LOG 文件，测试据此确认扫描器从不调用查询工具。

用法：python fake_mcp_server.py <sqlite 文件> <调用日志文件>
"""

import json
import sqlite3
import sys

db, log = sys.argv[1], sys.argv[2]
conn = sqlite3.connect(db)
TOOLS = [
    {"name": "list_tables", "description": "List all tables in the SQLite database", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "describe_table", "description": "Get the schema information for a specific table",
     "inputSchema": {"type": "object", "properties": {"table_name": {"type": "string"}}, "required": ["table_name"]}},
    {"name": "read_query", "description": "Execute a SELECT query on the SQLite database",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "create_order", "description": "Create a sales order for a customer",
     "inputSchema": {"type": "object", "properties": {"customer_id": {"type": "string"}, "items": {"type": "array"}}}},
]


def reply(mid, result):
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)


print("server starting (log line on stdout that is not JSON)", flush=True)
for line in sys.stdin:
    m = json.loads(line)
    method, mid = m.get("method"), m.get("id")
    if method == "initialize":
        reply(mid, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "sqlite-demo", "version": "1"}})
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = m["params"]["name"], m["params"].get("arguments") or {}
        open(log, "a").write(name + "\n")
        if name == "list_tables":
            rows = conn.execute("select name from sqlite_master where type='table'").fetchall()
            text = str([{"name": r[0]} for r in rows])        # 与参考实现一样返回 Python repr
        elif name == "describe_table":
            rows = conn.execute(f"pragma table_info('{args['table_name']}')").fetchall()
            text = str([{"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt_value": r[4], "pk": r[5]} for r in rows])
        else:
            text = "SHOULD NOT BE CALLED"
        reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
    elif mid is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not found"}}), flush=True)
