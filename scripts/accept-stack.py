"""Acceptance against the dedicated local Compose fixture. No external/production target allowed."""
import io
import json
import os
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from rdflib import Graph

base_url = "http://127.0.0.1:18800"
admin_email = os.environ.get("FDE_ACCEPTANCE_ADMIN_EMAIL", "admin@example.com")
admin_password = os.environ.get("FDE_ACCEPTANCE_ADMIN_PASSWORD", "")
credentials_file = os.environ.get("FDE_ACCEPTANCE_CREDENTIALS_FILE")
if credentials_file:
    credentials = dict(line.split(": ", 1) for line in Path(credentials_file).read_text().splitlines() if ": " in line)
    admin_email, admin_password = credentials.get("Email", admin_email), credentials.get("Password", "")
if not admin_password:
    raise SystemExit("Supply the dedicated acceptance stack's admin credentials via FDE_ACCEPTANCE_CREDENTIALS_FILE or FDE_ACCEPTANCE_ADMIN_PASSWORD")
client = httpx.Client(base_url=base_url, timeout=30)
for attempt in range(50):
    try:
        if client.get("/readyz").status_code == 200:
            break
    except httpx.HTTPError:
        pass
    time.sleep(0.4)
else:
    raise SystemExit("Dedicated Compose acceptance stack did not become ready")


def call(method, path, **kwargs):
    response = client.request(method, "/api"+path, **kwargs)
    if response.status_code >= 300:
        raise AssertionError((path, response.status_code, response.text[:600]))
    return response.json()


def wait(job_id):
    for _ in range(150):
        job = call("GET", "/jobs/"+job_id)
        if job["status"] == "succeeded":
            return job
        if job["status"] == "failed":
            raise AssertionError((job_id, job["error"]))
        time.sleep(0.2)
    raise AssertionError("Task did not finish within acceptance window")


signed_in = call("POST", "/auth/login", json={"email": admin_email, "password": admin_password})
client.headers["X-CSRF-Token"] = signed_in["csrf"]
call("GET", "/admin/session")
tenant = call("POST", "/admin/tenants", json={"name": "Container acceptance " + uuid.uuid4().hex[:10],
              "owner_user_id": signed_in["user"]["id"]})
call("POST", "/tenants/" + tenant["id"] + "/switch")
call("POST", "/billing/binding", json={"authorization_code": "demo-local"})
workspace = call("POST", "/workspaces", json={"name": "食品业务容器验收"})["id"]
call("POST", f"/workspaces/{workspace}/evidence", json={"text": "我们做食品批发，客户是餐厅，靠进销差价赚钱。采购从供应商进货，仓库管理库存，第三方物流发货。ERP 记订单，Excel 管补货，经常断货和发货延迟。"})
kb = call("POST", "/knowledge-bases", json={"name": "采购知识", "workspace_id": workspace})["id"]
doc = call("POST", f"/knowledge-bases/{kb}/documents", files={"file": ("procurement.md", "库存采购补货应结合安全库存与供应商交期，关注食品缺货。".encode(), "text/markdown")})
wait(doc["job"]["id"])
calls = []
for capability in ("summarize", "interview", "recommend", "report"):
    call_id = uuid.uuid4().hex
    job = call("POST", f"/workspaces/{workspace}/agent/{capability}", json={"call_id": call_id})
    wait(job["id"])
    replay = call("POST", f"/workspaces/{workspace}/agent/{capability}", json={"call_id": call_id})
    assert replay["id"] == job["id"]
    calls.append(capability)
result = call("GET", "/workspaces/"+workspace)
assert result["artifacts"] and any(a["citations"] for a in result["artifacts"])
export = call("POST", f"/workspaces/{workspace}/exports")
download = client.get("/api/exports/"+export["id"]+"/download")
download.raise_for_status()
with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
    assert "business.yaml" in archive.namelist()
    Graph().parse(data=archive.read("ontology.ttl"), format="turtle")
reconciled = call("POST", "/billing/reconcile")
assert reconciled["mismatched"] == 0
metrics = call("GET", "/metrics")
assert metrics["counts"]["jobs"] >= 5
receipt = {"target": base_url, "database": "PostgreSQL 16 / runtime RLS role",
           "transport": "BFF -> worker -> signed HTTP Pattern -> HTTP Router simulator",
           "capabilities": calls, "ingestion": "passed", "rag_citations": "passed",
           "idempotent_http_replay": "passed", "export": "passed", "reconciliation": reconciled,
           "external_model_or_charge": False}
Path("test-results").mkdir(exist_ok=True)
Path("test-results/compose-acceptance.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+"\n")
print(json.dumps(receipt, ensure_ascii=False))
