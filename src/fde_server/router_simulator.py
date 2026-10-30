"""Development-only Router contract fixture. Never a production balance or billing service."""
import hmac
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .router import canonical
from .security import Problem


def create_simulator(cfg, path=None):
    if cfg.production:
        raise RuntimeError("Router simulator is forbidden in production")
    path = Path(path or ".data/router-simulator.db")
    path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def database():
        with sqlite3.connect(path, timeout=30) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    with database() as db:
        db.execute("CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, tenant TEXT UNIQUE, balance INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, account TEXT NOT NULL, input_hash TEXT NOT NULL, budget INTEGER NOT NULL, reservation TEXT NOT NULL, status TEXT NOT NULL, output TEXT, receipt TEXT)")
    app = FastAPI(title="Development Router Simulator", docs_url=None)
    app.state.database = database
    app.state.fail_settlement = 0
    app.state.fail_execute_after_response = 0
    app.state.execution_count = 0
    app.state.settlement_count = 0
    app.state.settlement_attempts = 0
    app.state.fail_settlement_at = None

    @app.middleware("http")
    async def auth(request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer "+cfg.router_token):
            return JSONResponse({"error": {"code": "UNAUTHENTICATED"}}, status_code=401)
        return await call_next(request)

    @app.exception_handler(Problem)
    async def error(_, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    def operation(db, body):
        row = db.execute("SELECT * FROM operations WHERE id=?", (body["operation_id"],)).fetchone()
        if not row or body.get("reservation_id", row["reservation"]) != row["reservation"]:
            raise Problem("OPERATION_UNKNOWN", status=404)
        return row

    @app.get("/v1/fde/capabilities")
    def capabilities():
        return {"contract": cfg.router_contract, "simulation": True,
                "features": ["reserve", "execute", "settle", "release", "idempotency", "receipts", "operations", "chat", "structured_output", "embedding"]}

    @app.post("/v1/fde/bindings/exchange")
    def bind(body: dict):
        if body.get("authorization_code") != "demo-local":
            raise Problem("BINDING_INVALID", status=403)
        account = "account-"+body["tenant_id"]
        with database() as db:
            db.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?, ?)", (account, body["tenant_id"], 10_000_000))
        return {"account_id": account, "tenant_id": body["tenant_id"]}

    @app.get("/v1/fde/accounts/{account}")
    def account_get(account: str):
        with database() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account,)).fetchone()
            if not row:
                raise Problem("ACCOUNT_UNKNOWN", status=404)
            reserved = db.execute("SELECT coalesce(sum(budget),0) FROM operations WHERE account=? AND status IN ('reserved','executed')", (account,)).fetchone()[0]
            return {"account_id": account, "available_micros": row["balance"]-reserved,
                    "currency": "USD", "models": ["business-reasoner"], "embedding_models": [cfg.embedding_model], "simulation": True}

    @app.post("/v1/fde/reservations")
    def reserve(body: dict, request: Request):
        with database() as db:
            old = db.execute("SELECT * FROM operations WHERE id=?", (body["operation_id"],)).fetchone()
            if old:
                if old["account"] != body["account_id"] or old["input_hash"] != body["input_hash"] or old["budget"] != body["max_cost_micros"]:
                    raise Problem("IDEMPOTENCY_CONFLICT", status=409)
                return {"reservation_id": old["reservation"]}
            account = db.execute("SELECT * FROM accounts WHERE id=?", (body["account_id"],)).fetchone()
            if not account or account["tenant"] != body["attribution"]["tenant_id"]:
                raise Problem("ACCOUNT_UNKNOWN", status=403)
            reserved = db.execute("SELECT coalesce(sum(budget),0) FROM operations WHERE account=? AND status IN ('reserved','executed')", (account["id"],)).fetchone()[0]
            if body["max_cost_micros"] <= 0 or account["balance"]-reserved < body["max_cost_micros"]:
                raise Problem("QUOTA_EXHAUSTED", status=402)
            rid = "res-"+body["operation_id"]
            db.execute("INSERT INTO operations (id,account,input_hash,budget,reservation,status) VALUES (?,?,?,?,?,'reserved')",
                       (body["operation_id"], body["account_id"], body["input_hash"], body["max_cost_micros"], rid))
            return {"reservation_id": rid}

    @app.post("/v1/fde/executions")
    def execute(body: dict):
        with database() as db:
            row = operation(db, body)
            if row["output"]:
                return json.loads(row["output"])
            if row["status"] == "released":
                raise Problem("OPERATION_UNKNOWN", status=409)
            if body["kind"] == "embedding":
                from .knowledge import development_vector
                output = {"vectors": [development_vector(t) for t in body["request"]["input"]]}
            else:
                from ps.core.fake_llm import FakeLLM
                from ps.core.llm import LLMCall
                output, _ = FakeLLM().complete_json(LLMCall(**body["simulation"]))
            response = {"operation_id": row["id"], "request_id": "req-"+row["id"], "model": body["request"]["model"], "output": output}
            db.execute("UPDATE operations SET status='executed',output=? WHERE id=?", (canonical(response), row["id"]))
            app.state.execution_count += 1
        if app.state.fail_execute_after_response:
            app.state.fail_execute_after_response -= 1
            raise Problem("TEMPORARY_FAILURE", status=503)
        return response

    @app.post("/v1/fde/settlements")
    def settle(body: dict):
        app.state.settlement_attempts += 1
        if app.state.settlement_attempts == app.state.fail_settlement_at:
            raise Problem("TEMPORARY_FAILURE", status=503)
        if app.state.fail_settlement:
            app.state.fail_settlement -= 1
            raise Problem("TEMPORARY_FAILURE", status=503)
        with database() as db:
            row = operation(db, body)
            if row["receipt"]:
                return json.loads(row["receipt"])
            if not row["output"] or body["request_id"] != json.loads(row["output"])["request_id"]:
                raise Problem("OPERATION_UNKNOWN", status=409)
            amount = min(100, row["budget"])
            receipt = {"operation_id": row["id"], "account_id": row["account"], "request_id": body["request_id"],
                       "model": json.loads(row["output"])["model"],
                       "status": "settled", "tokens_in": 100, "tokens_out": 50, "amount_micros": amount, "currency": "USD"}
            db.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount, row["account"]))
            db.execute("UPDATE operations SET receipt=?,status='settled' WHERE id=?", (canonical(receipt), row["id"]))
            app.state.settlement_count += 1
            return receipt

    @app.post("/v1/fde/releases")
    def release(body: dict):
        with database() as db:
            row = operation(db, body)
            if row["status"] in {"executed", "settled"}:
                raise Problem("ALREADY_EXECUTED", status=409)
            db.execute("UPDATE operations SET status='released' WHERE id=?", (row["id"],))
        return {"ok": True}

    @app.get("/v1/fde/receipts/{operation_id}")
    def receipt(operation_id: str):
        with database() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row or not row["receipt"]:
                raise Problem("OPERATION_UNKNOWN", status=404)
            return json.loads(row["receipt"])

    @app.get("/v1/fde/operations/{operation_id}")
    def get_operation(operation_id: str):
        with database() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise Problem("OPERATION_UNKNOWN", status=404)
            return {"operation_id": operation_id, "status": row["status"], "reservation_id": row["reservation"],
                    "response": json.loads(row["output"]) if row["output"] else None}
    return app
