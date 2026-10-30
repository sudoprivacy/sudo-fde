import copy
import hashlib
from contextlib import contextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from .models import Artifact, Catalog, Evidence, Job, Membership, Revision
from .router import RouterLLM, canonical
from .security import Context, Problem, decode_service, digest, require_workspace


@contextmanager
def model_lock(runtime, tenant_id, workspace_id):
    key = int.from_bytes(
        hashlib.sha256(f"{tenant_id}:{workspace_id}".encode()).digest()[:8], "big", signed=True
    )
    if runtime.db.engine.dialect.name == "postgresql":
        with runtime.db.engine.connect() as conn:
            if not conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                raise Problem("WORKSPACE_BUSY", "工作区正在执行任务", 503)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    else:
        import fcntl
        from pathlib import Path

        folder = Path(".data/locks")
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / str(key)).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Problem("WORKSPACE_BUSY", "工作区正在执行任务", 503) from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def current_context(runtime, tenant_id, job_id):
    with runtime.db.session(tenant_id) as s:
        job = s.get(Job, job_id)
        if not job or job.tenant_id != tenant_id:
            raise Problem("NOT_FOUND", status=404)
        m = s.scalar(
            select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == job.user_id,
                Membership.active.is_(True),
            )
        )
        if not m:
            raise Problem("ACCESS_REVOKED", status=403)
        ctx = Context(job.user_id, tenant_id, m.role)
        ws = require_workspace(s, ctx, job.workspace_id, write=True)
        if job.capability != "learn" and job.status not in {"succeeded"} and ws.version != job.base_version:
            raise Problem("WORKSPACE_CHANGED", "工作区信息已变化，请重新发起生成", 409)
        return ctx, job


def walk_targets(value, prefix):
    if isinstance(value, dict):
        for k, v in value.items():
            if k not in {"evidence", "artifact_id", "id", "version", "confidence", "origin", "status"}:
                yield from walk_targets(v, prefix + "/" + str(k).replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk_targets(v, prefix + "/" + str(i))
    elif isinstance(value, str) and value:
        yield prefix, value


def refinement_slots(artifacts):
    result = []
    for a in artifacts:
        if a.status != "confirmed":
            continue
        obj = a.body.get("object", {})
        parent = obj.get("name") or obj.get("text") or a.body.get("statement", "")
        if a.kind == "question":
            values, kind = obj.get("data_needs", []), "need"
        elif a.kind == "process":
            values, kind = obj.get("steps", []), "step"
        elif a.kind == "ontology":
            kind = "link" if obj.get("type") == "link" else "object"
            result.append(
                {
                    "id": a.id + ":object",
                    "kind": kind,
                    "label": parent[:200],
                    "parent": parent[:300],
                    "statement": a.body.get("statement", "")[:600],
                }
            )
            values, kind = obj.get("key_properties", []), "prop"
        else:
            continue
        for i, value in enumerate(values):
            result.append(
                {"id": f"{a.id}:{kind}:{i}", "kind": kind, "label": str(value)[:200], "parent": parent[:300]}
            )
    return result[:60]


def prepare(runtime, ctx, job):
    from .knowledge import retrieve, sources_visible
    from fdescan import to_wire

    with runtime.db.session(ctx.tenant_id, ctx.user_id) as s:
        require_workspace(s, ctx, job.workspace_id, write=True)
        evs = list(
            s.scalars(
                select(Evidence)
                .where(
                    Evidence.tenant_id == ctx.tenant_id,
                    Evidence.workspace_id == job.workspace_id,
                    Evidence.active.is_(True),
                )
                .order_by(Evidence.created_at)
            )
        )
        arts = list(
            s.scalars(
                select(Artifact)
                .where(
                    Artifact.tenant_id == ctx.tenant_id,
                    Artifact.workspace_id == job.workspace_id,
                    Artifact.status != "superseded",
                )
                .order_by(Artifact.created_at)
            )
        )
        arts = [a for a in arts if sources_visible(s, ctx, job.workspace_id, a.citations)]
        cats = list(
            s.scalars(
                select(Catalog)
                .where(Catalog.tenant_id == ctx.tenant_id, Catalog.workspace_id == job.workspace_id)
                .order_by(Catalog.created_at)
            )
        )
        corrections = list(
            s.scalars(
                select(Revision).where(
                    Revision.tenant_id == ctx.tenant_id,
                    Revision.workspace_id == job.workspace_id,
                    Revision.active.is_(True),
                )
            )
        )
    request = {"call_id": job.call_id, "workspace_id": job.workspace_id, "capability": job.capability}
    if job.capability == "learn":
        return request | job.payload["params"], []
    evidence = [
        {"id": e.id, "kind": e.kind, "text": e.text, "ref": e.ref, "sensitivity": e.sensitivity} for e in evs
    ]
    if any(e["sensitivity"] == "restricted" for e in evidence):
        raise Problem("INPUT_SENSITIVE", "受限资料不得发送给外部模型", 422)
    query = "\n".join(e.text for e in evs[-3:])[-3000:] or "业务流程 核心问题 数据系统"
    citations = (
        retrieve(runtime, ctx, job.workspace_id, query, job.capability, job.id)
        if job.capability not in {"match", "refine"}
        else []
    )
    evidence += [{k: c[k] for k in ("id", "kind", "text", "ref", "sensitivity")} for c in citations]
    context = {
        "evidence": evidence,
        "confirmed": [],
        "proposed": [],
        "rejected": [],
        "corrections": [
            {"evidence_id": r.evidence_id, "understood": r.instruction[:300]} for r in corrections
        ],
    }
    for a in arts:
        if a.kind in {"process", "system", "question", "ontology", "binding", "gap"}:
            context[
                "confirmed"
                if a.status == "confirmed"
                else "rejected"
                if a.status == "rejected"
                else "proposed"
            ].append(
                {
                    "id": a.upstream_id or a.id,
                    "version": a.version,
                    "kind": a.kind,
                    "statement": a.body.get("statement", ""),
                    "object": a.body.get("object"),
                    "status": a.status if a.status in {"confirmed", "rejected"} else "proposed",
                }
            )
    # Keep the latest observation of each source; identifiers are namespaced by the scan.
    latest = {c.body["catalog"]["source"]["id"]: c for c in cats}
    wires = [to_wire(c.body, "ev" + c.id[:8]) for c in latest.values()]
    if wires:
        snap = digest("|".join(w["snapshot_id"] for w in wires))[:32]
        context["catalog"] = {
            "snapshot_id": snap,
            "sources": [w["source"] for w in wires],
            "assets": [a for w in wires for a in w["assets"]],
        }
        context["scan_scope"] = {"snapshot_id": snap, "systems": [w["scope"] for w in wires]}
        for w in wires:
            context["evidence"] += [
                {
                    "id": e["id"],
                    "kind": "observation",
                    "ref": e["ref"],
                    "snapshot_id": w["snapshot_id"],
                    "text": e["text"],
                    "observation": {k: e[k] for k in ("target", "locator", "facts")},
                }
                for e in w["evidence"]
            ]
    request["context"] = context
    params = copy.deepcopy(job.payload["params"])
    if job.capability in {"interview", "recommend"}:
        from ps.db import Workspace as PW

        with runtime.pattern_db.session(ctx.tenant_id) as s:
            state = s.get(PW, job.workspace_id)
            request["expected_state_version"] = state.state_version if state else 0
    if job.capability == "revise":
        iid = params.get("instruction_evidence_id") or (evs[-1].id if evs else "")
        params = {"mode": "revise", "instruction_evidence_id": iid, "targets": []}
        for a in arts:
            if (
                a.kind in {"summarize", "report", "process", "system", "question", "ontology"}
                and a.status != "rejected"
            ):
                params["targets"] += [
                    {"path": path, "text": value[:2000], "version": a.version}
                    for path, value in walk_targets(a.body, "artifact/" + a.id)
                ]
        params["targets"] = params["targets"][:800]
    if job.capability == "refine":
        params = {
            "slots": refinement_slots(arts),
            "tables": [
                {
                    "name": a["name"],
                    "rows": a.get("row_count"),
                    "columns": [
                        {"name": c["name"], "type": c["type"][:40], "pk": c["name"] in a["primary_key"]}
                        for c in a["columns"]
                    ],
                }
                for w in wires
                for a in w["assets"]
            ],
        }
    # These pipelines consume evidence and assertion summaries, not the full
    # duplicate catalog or candidate-ranking objects produced by matching.
    # Keep source IDs and business statements intact for citation validation.
    if job.capability in {"summarize", "report", "revise", "refine", "explain"}:
        context.pop("catalog", None)
        context.pop("scan_scope", None)
        for category in ("confirmed", "proposed", "rejected"):
            for assertion in context[category]:
                assertion.pop("object", None)
        if job.capability != "report":
            context["evidence"] = [item for item in context["evidence"] if item["kind"] != "observation"]
    if job.capability not in {"summarize"}:
        request["params"] = params
    return request, citations


def execute(runtime, tenant_id, job_id):
    from ps import models as pm
    from ps.config import load_settings
    from ps.core.auth import Identity
    from ps.core.errors import ServiceError
    from ps.core.store import idem_begin, idem_release, input_hash, stable_identities
    from ps.models import Context as PatternContext
    from ps.pipelines import explain, interview, learn, match, recommend, refine, revise, understand
    from ps.pipelines.runtime import Run
    from pydantic import ValidationError

    ctx, job = current_context(runtime, tenant_id, job_id)
    from .versions import pipeline_revision

    if not job.result and job.payload.get("pipeline_revision") != pipeline_revision():
        raise Problem("PIPELINE_CHANGED", "任务基于旧版流程，请重新发起任务", 409)
    pairs = {
        "summarize": (pm.SummarizeRequest, understand.summarize),
        "interview": (pm.InterviewRequest, interview.run),
        "recommend": (pm.RecommendRequest, recommend.run),
        "match": (pm.MatchRequest, match.run),
        "report": (pm.ReportRequest, understand.report),
        "revise": (pm.ReviseRequest, revise.run),
        "refine": (pm.RefineRequest, refine.suggest),
        "learn": (pm.LearnRequest, learn.run_learn),
        "explain": (pm.ExplainRequest, explain.run),
    }
    if job.capability not in pairs:
        raise Problem("CAPABILITY_INVALID")
    with model_lock(runtime, tenant_id, job.workspace_id):
        ctx, job = current_context(runtime, tenant_id, job_id)
        if job.result:
            return job.result
        if "request" not in job.payload:
            payload, citations = prepare(runtime, ctx, job)
            with runtime.db.session(tenant_id) as s:
                stored = s.get(Job, job.id)
                stored.payload = {**stored.payload, "request": payload, "citations": citations}
        else:
            payload = job.payload["request"]
        cls, pipeline = pairs[job.capability]
        try:
            req = cls.model_validate(payload)
        except ValidationError as exc:
            raise Problem("INPUT_INVALID", "当前资料或参数不足以执行该能力", 422) from exc
        from ps.core.guard import scan, verdict

        if verdict(scan(payload)) != "clean":
            raise Problem("INPUT_SENSITIVE", "资料含敏感信息或未经允许的明细值", 422)
        if len(canonical(payload.get("context", {}))) > runtime.cfg.max_context_chars:
            raise Problem("CONTEXT_TOO_LARGE", "业务上下文超过限制，请减少输入资料", 413)
        settings = load_settings()
        settings.llm_backend = "sudorouter"
        settings.thresholds["recommend"]["samples"] = 3
        with runtime.pattern_db.session(tenant_id, ctx.user_id) as s:
            # Holding the workspace lock proves an old in-progress row has no active owner.
            from ps.db import Idempotency

            old = s.get(Idempotency, (tenant_id, job.capability, job.call_id))
            if old and old.status == "in_progress":
                idem_release(s, tenant_id, job.capability, job.call_id)
            cached = idem_begin(s, tenant_id, job.capability, job.call_id, input_hash(payload))
            if cached is not None:
                return cached
            provider_id = job.payload.get("provider_id")
            if provider_id:
                from .providers import UserProviderLLM

                llm = UserProviderLLM(runtime, tenant_id, ctx.user_id, provider_id, job.model)
            else:
                llm = RouterLLM(runtime, tenant_id, job.id, job.model)
            run = Run(
                settings=settings,
                llm=llm,
                session=s,
                ident=Identity(tenant_id, ctx.user_id),
                capability=job.capability,
                call_id=job.call_id,
                workspace_id=job.workspace_id,
                input_hash=input_hash(payload),
                corrections=[c.model_dump() for c in getattr(req, "context", PatternContext()).corrections],
            )
            try:
                with stable_identities(tenant_id + ":" + job.workspace_id + ":" + job.call_id):
                    result = pipeline(run, req)
            except ServiceError as exc:
                raise Problem(exc.code, str(exc), exc.status) from exc
        with runtime.db.session(tenant_id) as s:
            s.get(Job, job.id).result = result
        return result


def create_pattern_app(runtime=None):
    from .runtime import Runtime

    rt = runtime or Runtime()
    app = FastAPI(title="FDE internal Pattern Service", docs_url=None, redoc_url=None)
    app.state.runtime = rt

    @app.middleware("http")
    async def sanitized_errors(request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            import logging

            logging.getLogger("fde.pattern").error(
                "pattern request failed error_class=%s", type(exc).__name__
            )
            return JSONResponse(
                {"error": {"code": "INTERNAL_ERROR", "message": "业务服务暂时不可用"}}, status_code=500
            )

    @app.exception_handler(Problem)
    async def problem(_, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.get("/healthz")
    def health():
        return {"ok": True, "service": "pattern"}

    @app.post("/internal/jobs/{job_id}")
    def run(job_id: str, request: Request):
        claims = decode_service(rt.cfg, request.headers.get("authorization"))
        ctx, job = current_context(rt, claims["tenant"], job_id)
        if (
            claims["job"] != job_id
            or claims["sub"] != ctx.user_id
            or claims["payload_hash"] != job.input_hash
        ):
            raise Problem("SERVICE_IDENTITY_INVALID", status=403)
        return execute(rt, ctx.tenant_id, job_id)

    return app
