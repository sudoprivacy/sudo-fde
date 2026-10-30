import uuid
from datetime import timedelta

import httpx
from sqlalchemy import or_, select

from .models import Artifact, Audit, DocumentVersion, Job, Membership, QueueTicket, Tenant, Workspace, now
from .router import canonical
from .security import Context, Problem, audit, digest, require_workspace, service_token

CAPABILITIES = {
    "summarize",
    "interview",
    "recommend",
    "match",
    "report",
    "revise",
    "refine",
    "learn",
    "explain",
}


def enqueue(
    runtime, s, ctx, workspace_id, capability, params, call_id, model="", kind="agent", provider_id=None
):
    existing = s.scalar(select(Job).where(Job.tenant_id == ctx.tenant_id, Job.call_id == call_id))
    hashed = digest(
        canonical(
            {
                "workspace_id": workspace_id,
                "capability": capability,
                "params": params,
                "model": model,
                "provider_id": provider_id,
                "kind": kind,
            }
        )
    )
    if existing:
        if existing.input_hash != hashed or existing.user_id != ctx.user_id:
            raise Problem("IDEMPOTENCY_CONFLICT", "调用 ID 已用于其他请求", 409)
        return existing
    tenant = s.execute(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update()).scalar_one()
    ws = require_workspace(s, ctx, workspace_id, write=True) if workspace_id else None
    active = list(
        s.scalars(
            select(Job).where(
                Job.tenant_id == ctx.tenant_id, Job.status.in_(["queued", "running", "retry_wait"])
            )
        )
    )
    if len(active) >= runtime.cfg.max_concurrent_jobs:
        raise Problem("CONCURRENCY_LIMIT", "组织正在处理的任务过多", 429)
    if ws and kind == "agent" and any(j.workspace_id == ws.id and j.kind == "agent" for j in active):
        raise Problem("WORKSPACE_BUSY", "工作区已有正在执行的任务", 409)
    if kind == "agent" and capability not in CAPABILITIES:
        raise Problem("CAPABILITY_INVALID", "不支持的能力")
    from .versions import pipeline_revision
    from .providers import default_business_model, get_for_job, list_for_user, require_business_model

    needs_model = kind == "agent" and capability != "learn"
    if needs_model and not provider_id and not model:
        # Legacy clients may request automatic AI without a selector. Prefer
        # the configured organization route, otherwise use only this user's
        # active personal providers; never borrow another member's key.
        if tenant.router_account:
            model = default_business_model(tenant.allowed_models)
        if not model:
            for candidate in list_for_user(s, ctx.tenant_id, ctx.user_id):
                model = default_business_model(candidate.models, candidate.default_model)
                if model:
                    provider_id = candidate.provider_id
                    break
    if needs_model and provider_id:
        provider, _ = get_for_job(s, runtime.cfg, ctx.tenant_id, ctx.user_id, provider_id)
        if model and model not in provider.models:
            raise Problem("MODEL_NOT_ALLOWED", "模型不在个人提供商目录中", 403)
        model = model or default_business_model(provider.models, provider.default_model)
    elif needs_model:
        if not model:
            raise Problem("MODEL_NOT_CONFIGURED", "请在「我的模型」配置对话模型，或绑定组织模型账户", 409)
        if not tenant.router_account:
            raise Problem("ROUTER_NOT_BOUND", "组织模型账户未绑定，请选择个人提供商模型或完成组织绑定", 409)
        if model not in tenant.allowed_models:
            raise Problem("MODEL_NOT_ALLOWED", "模型不在组织允许的模型目录中", 403)
    if needs_model:
        require_business_model(model)
    job = Job(
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        user_id=ctx.user_id,
        call_id=call_id,
        kind=kind,
        capability=capability,
        payload={
            "params": params,
            "pipeline_revision": pipeline_revision(),
            "embedding_model": runtime.cfg.embedding_model,
            "provider_id": provider_id,
        },
        input_hash=hashed,
        model=model,
        budget_micros=min(tenant.budget_micros, runtime.cfg.max_budget_micros),
        base_version=ws.version if ws else 0,
    )
    s.add(job)
    s.flush()
    s.add(QueueTicket(tenant_id=ctx.tenant_id, job_id=job.id))
    audit(s, ctx, "job.queued", job.id, workspace_id, capability=capability)
    return job


def claim(runtime):
    with runtime.db.session() as s:
        q = (
            select(QueueTicket)
            .where(
                QueueTicket.available_at <= now(),
                or_(QueueTicket.lease_until.is_(None), QueueTicket.lease_until < now()),
            )
            .order_by(QueueTicket.created_at)
            .with_for_update(skip_locked=True)
        )
        ticket = s.scalar(q.limit(1))
        if not ticket:
            return None
        ticket.lease_until = now() + timedelta(seconds=runtime.cfg.job_lease_seconds)
        ticket.lease_token = uuid.uuid4().hex
        return ticket.id, ticket.tenant_id, ticket.job_id, ticket.lease_token


def run_once(runtime, dispatch=None):
    claimed = claim(runtime)
    if not claimed:
        return False
    ticket_id, tenant_id, job_id, lease_token = claimed
    try:
        with runtime.db.session(tenant_id) as s:
            job = s.get(Job, job_id)
            if job.status in {"succeeded", "failed", "cancelled"}:
                with runtime.db.session() as qs:
                    qs.delete(qs.get(QueueTicket, ticket_id))
                return True
            member = s.scalar(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == job.user_id,
                    Membership.active.is_(True),
                )
            )
            if not member:
                raise Problem("ACCESS_REVOKED", "任务发起者的权限已撤销", 403)
            ctx = Context(job.user_id, tenant_id, member.role)
            if job.workspace_id:
                require_workspace(s, ctx, job.workspace_id, write=True)
            from .versions import pipeline_revision

            if not job.result and (
                job.payload.get("pipeline_revision") != pipeline_revision()
                or job.payload.get("embedding_model") != runtime.cfg.embedding_model
            ):
                raise Problem("PIPELINE_CHANGED", "任务基于旧版流程或向量模型，请重新发起任务", 409)
            job.status, job.attempts = "running", job.attempts + 1
            job.lease_until = now() + timedelta(seconds=runtime.cfg.job_lease_seconds)
        if job.kind == "ingestion":
            from .knowledge import ingest

            # Version IDs are held in the job's server-owned parameters.
            job.payload = job.payload["params"]
            result = ingest(runtime, job)
        elif dispatch:
            result = dispatch(runtime, tenant_id, job_id)
        else:
            token = service_token(runtime.cfg, ctx, job.id, job.input_hash)
            try:
                with httpx.Client(
                    timeout=runtime.cfg.job_lease_seconds - 30, follow_redirects=False, trust_env=False
                ) as client:
                    res = client.post(
                        runtime.cfg.pattern_url + "/internal/jobs/" + job.id,
                        headers={"Authorization": "Bearer " + token},
                    )
                if res.status_code >= 300:
                    err = res.json().get("error", {})
                    raise Problem(
                        err.get("code", "PATTERN_UNAVAILABLE"),
                        err.get("message", "业务服务暂不可用"),
                        res.status_code,
                    )
                result = res.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise Problem("PATTERN_UNAVAILABLE", "业务服务暂不可用", 503) from exc
        with runtime.db.session(tenant_id) as s:
            job = s.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one()
            if job.status != "succeeded":
                if job.workspace_id:
                    ws = require_workspace(s, ctx, job.workspace_id, write=True)
                    if job.kind == "agent" and job.capability != "learn" and ws.version != job.base_version:
                        raise Problem("WORKSPACE_CHANGED", "工作区信息已变化，请重新发起生成", 409)
                    from .knowledge import authorize_sources

                    authorize_sources(s, ctx, job.workspace_id, job.payload.get("citations", []))
                if job.kind == "agent":
                    save_result(s, ctx, job, result)
                job.result, job.status, job.error = result, "succeeded", None
                audit(s, ctx, "job.succeeded", job.id, job.workspace_id, capability=job.capability)
        with runtime.db.session() as s:
            ticket = s.get(QueueTicket, ticket_id)
            if ticket and ticket.lease_token == lease_token:
                s.delete(ticket)
    except Exception as exc:
        code = exc.code if isinstance(exc, Problem) else "INTERNAL_ERROR"
        retryable = isinstance(exc, Problem) and exc.status in (502, 503, 504)
        with runtime.db.session(tenant_id) as s:
            job = s.get(Job, job_id)
            retry = retryable and job.attempts < 5
            job.status, job.error = ("retry_wait" if retry else "failed"), code
            if job.kind == "ingestion":
                version = s.get(DocumentVersion, job.payload["params"]["version_id"])
                version.status, version.error = ("queued" if retry else "failed"), code
            s.add(
                Audit(
                    tenant_id=tenant_id,
                    user_id=job.user_id,
                    workspace_id=job.workspace_id,
                    action="job.retry" if retry else "job.failed",
                    resource_id=job.id,
                    details={"code": code},
                )
            )
        with runtime.db.session() as s:
            ticket = s.get(QueueTicket, ticket_id)
            if ticket and ticket.lease_token == lease_token:
                if retry:
                    ticket.lease_until, ticket.lease_token = None, None
                    ticket.available_at = now() + timedelta(seconds=min(60, 2**job.attempts))
                else:
                    s.delete(ticket)
        if not isinstance(exc, Problem):
            import logging

            logging.getLogger("fde.worker").error(
                "job failed id=%s error_class=%s", job_id, type(exc).__name__
            )
    return True


def save_result(s, ctx, job, result):
    if job.capability == "learn":
        return
    body = result.get("result", result)
    if job.capability in {"summarize", "report", "match", "interview", "refine"}:
        for previous in s.scalars(
            select(Artifact).where(
                Artifact.tenant_id == ctx.tenant_id,
                Artifact.workspace_id == job.workspace_id,
                Artifact.kind == job.capability,
                Artifact.status.notin_(["superseded", "rejected"]),
            )
        ):
            previous.status = "superseded"
    citations = [
        {k: v for k, v in citation.items() if k != "text"} for citation in job.payload.get("citations", [])
    ]
    items = body.get("items", []) if job.capability in {"recommend", "match"} else []
    if job.capability == "report":
        items = body.get("ontology_items", [])
    s.add(
        Artifact(
            tenant_id=ctx.tenant_id,
            workspace_id=job.workspace_id,
            job_id=job.id,
            kind=job.capability,
            body=body,
            citations=citations,
            upstream_id=body.get("artifact_id"),
            status="proposed",
        )
    )
    for item in items:
        s.add(
            Artifact(
                tenant_id=ctx.tenant_id,
                workspace_id=job.workspace_id,
                job_id=job.id,
                kind=item.get("kind", "ontology"),
                body=item,
                upstream_id=item.get("artifact_id"),
                citations=citations,
            )
        )
    ws = s.get(Workspace, job.workspace_id)
    ws.version += 1
    ws.phase = {
        "summarize": "challenge",
        "interview": "interview",
        "match": "interview",
        "report": "report",
        "refine": "refine",
    }.get(job.capability, ws.phase)
    ws.next_action = {
        "summarize": "answer_challenge",
        "interview": "recommend",
        "match": "report",
        "report": "review_results",
        "refine": "export",
    }.get(job.capability, ws.next_action)
