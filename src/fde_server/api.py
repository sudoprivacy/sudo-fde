import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from . import mail, schemas as q
from .audit_display import present_audits
from .db import set_scope
from .jobs import enqueue
from .knowledge import authorize_sources, require_kb, retrieve, sources_visible
from .models import (
    Artifact,
    Audit,
    Catalog,
    Chunk,
    Decision,
    Document,
    DocumentVersion,
    Evidence,
    Export,
    Invitation,
    Job,
    KnowledgeBase,
    KnowledgeGrant,
    LoginSession,
    Membership,
    ModelProvider,
    PasswordReset,
    QueueTicket,
    Refinement,
    RefinementVersion,
    RouterCall,
    Tenant,
    TenantDirectory,
    User,
    Workspace,
    WorkspaceMember,
    now,
    uid,
)
from .router import reconcile
from .security import (
    ADMINS,
    DUMMY_HASH,
    PASSWORDS,
    Context,
    Problem,
    audit,
    authenticate,
    check_password,
    digest,
    rate_limit,
    require_admin,
    require_platform_admin,
    require_workspace,
    security_event,
    session_digest,
    utc,
    visible_workspaces,
)
from .providers import business_models, fetch_model_catalog, row_view, encrypt, validate_base, test_provider


def view(row, exclude=()):
    blocked = {"password_hash", "token_hash", "csrf_hash", "object_key", "payload"} | set(exclude)
    out = {}
    for col in row.__table__.columns:
        key = col.key
        if key in blocked:
            continue
        value = getattr(row, "metadata_" if key == "metadata" else key)
        out[key] = utc(value).isoformat() if hasattr(value, "isoformat") else value
    return out


def validation_field_errors(errors):
    """Describe invalid fields without echoing submitted values or credentials."""
    names = {
        "provider_id": "Provider ID", "display_name": "显示名称", "api_base": "API 地址",
        "protocol": "API 协议", "api_key": "API 密钥", "models": "模型目录",
        "default_model": "默认模型", "email": "邮箱", "password": "密码", "name": "名称",
    }
    details = []
    for error in errors:
        path = [part for part in error["loc"] if part not in {"body", "query", "path"}]
        field = str(path[0]) if path else "request"
        title = names.get(field, field)
        if field == "models" and len(path) > 1 and isinstance(path[1], int):
            title = f"模型目录第 {path[1] + 1} 个模型 ID"
        context, code = error.get("ctx", {}), error["type"]
        if code == "missing":
            reason = "不能为空"
        elif code == "string_too_short":
            reason = f"至少需要 {context['min_length']} 个字符"
        elif code == "string_too_long":
            reason = f"不能超过 {context['max_length']} 个字符"
        elif code == "too_short":
            reason = f"至少需要 {context['min_length']} 项"
        elif code == "too_long":
            reason = f"不能超过 {context['max_length']} 项"
        elif code == "string_pattern_mismatch" and field == "provider_id":
            reason = "须以小写字母开头，仅包含小写字母、数字、下划线或连字符"
        elif code == "extra_forbidden":
            reason = "是不支持的字段"
        else:
            reason = "格式不正确，请检查后重试"
        details.append({"field": ".".join(map(str, path)), "message": title + reason})
    return details


def protect_last_owner(s, member, role, active):
    if not member.active or member.role != "tenant_owner" or (role == "tenant_owner" and active):
        return
    s.get(Tenant, member.tenant_id, with_for_update=True)
    others = s.scalar(
        select(func.count()).select_from(Membership).join(User, User.id == Membership.user_id).where(
            Membership.tenant_id == member.tenant_id, Membership.user_id != member.user_id,
            Membership.role == "tenant_owner", Membership.active.is_(True), User.active.is_(True),
        )
    )
    if not others:
        raise Problem("OWNER_PROTECTED", "租户至少需要保留一名有效的组织所有者", 409)


def login_response(rt, user, tenant_id):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with rt.db.session() as s:
        s.add(
            LoginSession(
                token_hash=session_digest(rt.cfg.session_secret, token),
                csrf_hash=session_digest(rt.cfg.session_secret, csrf),
                user_id=user.id,
                tenant_id=tenant_id,
                expires_at=now() + timedelta(hours=rt.cfg.session_hours),
            )
        )
    response = JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "name": user.name},
            "tenant_id": tenant_id,
            "csrf": csrf,
        }
    )
    response.set_cookie(
        "fde_session",
        token,
        httponly=True,
        secure=rt.cfg.production,
        samesite="lax",
        max_age=rt.cfg.session_hours * 3600,
        path="/",
    )
    response.set_cookie(
        "fde_csrf",
        csrf,
        httponly=False,
        secure=rt.cfg.production,
        samesite="strict",
        max_age=rt.cfg.session_hours * 3600,
        path="/",
    )
    return response


def create_app(runtime=None):
    from .runtime import Runtime

    rt = runtime or Runtime()
    app = FastAPI(
        title="Sudo FDE SaaS",
        version="1.0.0",
        docs_url=None if rt.cfg.production else "/docs",
        redoc_url=None,
        openapi_url=None if rt.cfg.production else "/openapi.json",
    )
    app.state.runtime = rt
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        request_id = uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception as exc:
            import logging

            logging.getLogger("fde.api").error(
                "unhandled request id=%s error_class=%s", request_id, type(exc).__name__
            )
            response = JSONResponse(
                {"error": {"code": "INTERNAL_ERROR", "message": "服务暂时不可用", "request_id": request_id}},
                status_code=500,
            )
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            }
        )
        if rt.cfg.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(Problem)
    async def problem(request, exc):
        if exc.status in (401, 403, 404) or exc.code == "INPUT_SENSITIVE":
            security_event(
                rt.db,
                "request.denied",
                request.client.host if request.client else "",
                code=exc.code,
                route=request.scope.get("route").path if request.scope.get("route") else "unknown",
            )
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc):
        details = validation_field_errors(exc.errors())
        return JSONResponse(
            {
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "；".join(item["message"] for item in details[:5]),
                    "fields": [".".join(map(str, e["loc"])) for e in exc.errors()],
                    "field_errors": details,
                }
            },
            status_code=422,
        )

    @app.exception_handler(IntegrityError)
    async def conflict(_, exc):
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
        if constraint == "uq_tenants_name_normalized" or "uq_tenants_name_normalized" in str(exc.orig):
            return JSONResponse(
                {"error": {"code": "TENANT_NAME_EXISTS", "message": "组织名称已存在，请使用其他名称"}},
                status_code=409,
            )
        return JSONResponse(
            {"error": {"code": "CONFLICT", "message": "记录已存在或版本冲突，请刷新后重试"}}, status_code=409
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/admin", response_class=HTMLResponse)
    @app.get("/invite", response_class=HTMLResponse)
    @app.get("/reset-password", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "templates/index.html").read_text()

    @app.get("/healthz")
    def health():
        return {"ok": True, "service": "fde-bff"}

    @app.get("/readyz")
    def ready():
        from sqlalchemy import text

        with rt.db.session() as s:
            s.execute(text("SELECT 1"))
        rt.router.capabilities()
        return {"ok": True}

    @app.get("/api/admin/session")
    def admin_session(ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        return {"platform_admin": True, "user_id": ctx.user_id, "tenant_id": ctx.tenant_id}

    @app.get("/api/admin/overview")
    def admin_overview(ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant_count = s.scalar(select(func.count()).select_from(Tenant))
            user_count = s.scalar(select(func.count()).select_from(User))
            workspace_count = s.scalar(select(func.count()).select_from(Workspace))
            active_jobs = s.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status.in_(["queued", "running", "retry_wait"]))
            )
            failed_jobs = s.scalar(select(func.count()).select_from(Job).where(Job.status == "failed"))
            calls = s.scalar(select(func.count()).select_from(RouterCall))
            from ps.db import CallLog

            with rt.pattern_db.session(platform_admin=True) as ps:
                personal_calls = ps.scalar(select(func.sum(CallLog.llm_calls)).where(CallLog.model_alias.like("provider:%"))) or 0
            return {
                "tenants": tenant_count,
                "users": user_count,
                "workspaces": workspace_count,
                "active_jobs": active_jobs,
                "failed_jobs": failed_jobs,
                "router_calls": calls,
                "personal_calls": personal_calls,
                "model_calls": calls + personal_calls,
            }

    @app.get("/api/admin/tenants")
    def admin_tenants(ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            rows = []
            for tenant in s.scalars(select(Tenant).order_by(Tenant.created_at.desc())):
                rows.append(
                    {
                        **view(tenant),
                        "members": s.scalar(
                            select(func.count())
                            .select_from(Membership)
                            .where(Membership.tenant_id == tenant.id)
                        ),
                        "workspaces": s.scalar(
                            select(func.count())
                            .select_from(Workspace)
                            .where(Workspace.tenant_id == tenant.id)
                        ),
                        "active_jobs": s.scalar(
                            select(func.count())
                            .select_from(Job)
                            .where(
                                Job.tenant_id == tenant.id,
                                Job.status.in_(["queued", "running", "retry_wait"]),
                            )
                        ),
                    }
                )
            return rows

    @app.post("/api/admin/tenants")
    def admin_tenant_create(data: q.TenantCreate, ctx=Depends(authenticate)):
        """Create a tenant from the platform control plane.

        A tenant may be created before its first user is attached. This lets
        platform operations provision the space and then associate an
        existing user from the user directory in a separate step.
        """
        require_platform_admin(ctx)
        tenant_id = uid()
        with rt.db.session(platform_admin=True) as s:
            if data.owner_user_id:
                owner = s.get(User, data.owner_user_id)
                if not owner or not owner.active:
                    raise Problem("USER_NOT_FOUND", "指定的用户不存在或已停用", 404)
            tenant = Tenant(
                id=tenant_id,
                name=data.name,
                budget_micros=data.budget_micros,
                allowed_models=data.allowed_models,
            )
            s.add(tenant)
            s.flush()
            s.add(TenantDirectory(id=tenant_id))
            if data.owner_user_id:
                s.add(Membership(tenant_id=tenant_id, user_id=data.owner_user_id, role="tenant_owner"))
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.tenant_created",
                tenant_id,
                owner_user_id=data.owner_user_id or "",
            )
            return view(tenant)

    @app.get("/api/admin/tenants/{tenant_id}")
    def admin_tenant(tenant_id: str, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise Problem("NOT_FOUND", status=404)
            members = [
                {**view(m), "user": view(s.get(User, m.user_id))}
                for m in s.scalars(select(Membership).where(Membership.tenant_id == tenant_id))
            ]
            workspaces = [
                view(w) for w in s.scalars(select(Workspace).where(Workspace.tenant_id == tenant_id))
            ]
            return {"tenant": view(tenant), "members": members, "workspaces": workspaces}

    @app.patch("/api/admin/tenants/{tenant_id}")
    def admin_tenant_patch(tenant_id: str, data: q.TenantPatch, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise Problem("NOT_FOUND", status=404)
            changes = data.model_dump(exclude_none=True)
            if not changes:
                raise Problem("NO_CHANGES", "至少提交一项需要修改的内容")
            for key, value in changes.items():
                setattr(tenant, key, value)
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.tenant_updated",
                tenant_id,
                changes=changes,
            )
            return view(tenant)

    @app.delete("/api/admin/tenants/{tenant_id}")
    def admin_tenant_delete(tenant_id: str, ctx=Depends(authenticate)):
        """Retire a tenant while preserving its data and audit history."""
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise Problem("NOT_FOUND", status=404)
            tenant.active = False
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.tenant_deleted",
                tenant_id,
            )
        return {"ok": True, "deleted": True}

    @app.post("/api/admin/tenants/{tenant_id}/suspend")
    def admin_tenant_suspend(tenant_id: str, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise Problem("NOT_FOUND", status=404)
            tenant.active = False
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.tenant_suspended",
                tenant_id,
            )
        return {"ok": True}

    @app.post("/api/admin/tenants/{tenant_id}/restore")
    def admin_tenant_restore(tenant_id: str, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise Problem("NOT_FOUND", status=404)
            tenant.active = True
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.tenant_restored",
                tenant_id,
            )
        return {"ok": True}

    @app.get("/api/admin/users")
    def admin_users(ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            return [
                {
                    **view(u),
                    "memberships": [
                        {
                            **view(m),
                            "tenant_name": (s.get(Tenant, m.tenant_id).name if s.get(Tenant, m.tenant_id) else ""),
                        }
                        for m in s.scalars(select(Membership).where(Membership.user_id == u.id))
                    ],
                }
                for u in s.scalars(select(User).order_by(User.created_at.desc()))
            ]

    @app.patch("/api/admin/users/{user_id}/status")
    def admin_user_status(user_id: str, data: q.MemberEdit, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            user = s.get(User, user_id)
            if not user:
                raise Problem("NOT_FOUND", status=404)
            if not data.active and user_id == ctx.user_id:
                raise Problem("SELF_PROTECTED", "不能停用当前登录的管理员", 409)
            if not data.active:
                for member in s.scalars(select(Membership).where(Membership.user_id == user_id)):
                    protect_last_owner(s, member, member.role, False)
            user.active = data.active
            audit(
                s,
                Context(ctx.user_id, ctx.tenant_id, "platform_admin", ctx.session_id, True),
                "platform.user_status_changed",
                user_id,
                active=data.active,
            )
            return view(user)

    @app.post("/api/admin/users/{user_id}/memberships")
    def admin_user_membership_create(
        user_id: str, data: q.TenantMembershipCreate, ctx=Depends(authenticate)
    ):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            user = s.get(User, user_id)
            tenant = s.get(Tenant, data.tenant_id)
            if not user or not user.active:
                raise Problem("USER_NOT_FOUND", "用户不存在或已停用", 404)
            if not tenant or not tenant.active:
                raise Problem("TENANT_NOT_FOUND", "租户不存在或已停用", 404)
            member = s.scalar(
                select(Membership)
                .where(Membership.user_id == user_id, Membership.tenant_id == data.tenant_id)
                .with_for_update()
            )
            if member:
                protect_last_owner(s, member, data.role, True)
                member.role = data.role
                member.active = True
            else:
                member = Membership(tenant_id=data.tenant_id, user_id=user_id, role=data.role)
                s.add(member)
                s.flush()
            audit(
                s,
                Context(ctx.user_id, data.tenant_id, "platform_admin", ctx.session_id, True),
                "platform.membership_assigned",
                user_id,
                tenant_id=data.tenant_id,
                role=data.role,
            )
            return {**view(member), "tenant_name": tenant.name, "user": view(user)}

    @app.patch("/api/admin/users/{user_id}/memberships/{tenant_id}")
    def admin_user_membership_patch(
        user_id: str,
        tenant_id: str,
        data: q.TenantMembershipPatch,
        ctx=Depends(authenticate),
    ):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            member = s.scalar(
                select(Membership)
                .where(Membership.user_id == user_id, Membership.tenant_id == tenant_id)
                .with_for_update()
            )
            if not member:
                raise Problem("MEMBERSHIP_NOT_FOUND", "用户尚未关联到该租户", 404)
            changes = data.model_dump(exclude_none=True)
            if not changes:
                raise Problem("NO_CHANGES", "至少提交一项需要修改的内容")
            protect_last_owner(s, member, changes.get("role", member.role), changes.get("active", member.active))
            for key, value in changes.items():
                setattr(member, key, value)
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.membership_updated",
                user_id,
                tenant_id=tenant_id,
                changes=changes,
            )
            return view(member)

    @app.delete("/api/admin/users/{user_id}/memberships/{tenant_id}")
    def admin_user_membership_delete(user_id: str, tenant_id: str, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            member = s.scalar(
                select(Membership)
                .where(Membership.user_id == user_id, Membership.tenant_id == tenant_id)
                .with_for_update()
            )
            if not member:
                raise Problem("MEMBERSHIP_NOT_FOUND", "用户尚未关联到该租户", 404)
            protect_last_owner(s, member, member.role, False)
            member.active = False
            audit(
                s,
                Context(ctx.user_id, tenant_id, "platform_admin", ctx.session_id, True),
                "platform.membership_removed",
                user_id,
                tenant_id=tenant_id,
            )
        return {"ok": True}

    @app.post("/api/admin/users/{user_id}/platform-admin")
    def admin_user_promote(user_id: str, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            user = s.get(User, user_id)
            if not user:
                raise Problem("NOT_FOUND", status=404)
            user.platform_admin = True
            audit(
                s,
                Context(ctx.user_id, ctx.tenant_id, "platform_admin", ctx.session_id, True),
                "platform.admin_granted",
                user_id,
            )
        return {"ok": True}

    @app.get("/api/admin/audit")
    def admin_audit(offset: int = 0, exclude_reads: bool = False, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            return present_audits(s, s.scalars(
                    select(Audit).where(Audit.action != "workspace.viewed" if exclude_reads else True)
                    .order_by(Audit.created_at.desc(), Audit.id.desc()).offset(max(0, offset)).limit(200)
            ))

    @app.get("/api/admin/jobs")
    def admin_jobs(ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        with rt.db.session(platform_admin=True) as s:
            return [view(j) for j in s.scalars(select(Job).order_by(Job.created_at.desc()).limit(200))]

    def origin_guard(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != rt.cfg.public_url.rstrip("/"):
            raise Problem("ORIGIN_DENIED", status=403)

    @app.post("/api/auth/register", include_in_schema=False)
    def registration_closed():
        # Keep a clear response for old clients; organization creation is admin-only.
        raise Problem("REGISTRATION_DISABLED", "公开注册已关闭，请联系管理员邀请加入组织", 403)

    @app.post("/api/auth/login")
    def login(data: q.Login, request: Request):
        origin_guard(request)
        email = str(data.email).lower()
        rate_limit(rt.db, "login-ip:" + request.client.host, limit=30)
        rate_limit(rt.db, "login-user:" + email, limit=10)
        with rt.db.session() as s:
            user = s.scalar(select(User).where(User.email == email))
            try:
                PASSWORDS.verify(user.password_hash if user else DUMMY_HASH, data.password)
                valid = bool(user and user.active)
            except Exception:
                valid = False
            if not valid:
                security_event(rt.db, "auth.failed", email)
                raise Problem("INVALID_CREDENTIALS", "邮箱或密码错误", 401)
            set_scope(s, user_id=user.id)
            member = s.scalar(
                select(Membership).join(Tenant, Membership.tenant_id == Tenant.id)
                .where(Membership.user_id == user.id, Membership.active.is_(True),
                       Tenant.active.is_(True) if not user.platform_admin else True)
                .order_by(Membership.created_at)
            )
            if not member:
                raise Problem("NO_MEMBERSHIP", "账号没有有效的组织成员资格", 403)
            tenant_id = member.tenant_id
        with rt.db.session(tenant_id, user.id) as s:
            audit(s, Context(user.id, tenant_id, member.role), "auth.login")
        return login_response(rt, user, tenant_id)

    @app.post("/api/auth/logout")
    def logout(ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            s.get(LoginSession, ctx.session_id).revoked = True
            audit(s, ctx, "auth.logout")
        res = JSONResponse({"ok": True})
        res.delete_cookie("fde_session")
        res.delete_cookie("fde_csrf")
        return res

    @app.post("/api/auth/password-reset")
    def reset_request(data: q.ResetRequest, request: Request):
        origin_guard(request)
        rate_limit(rt.db, "reset:" + request.client.host, limit=5)
        with rt.db.session() as s:
            user = s.scalar(select(User).where(User.email == str(data.email).lower(), User.active.is_(True)))
            if user:
                token = secrets.token_urlsafe(32)
                s.add(
                    PasswordReset(
                        user_id=user.id, token_hash=digest(token), expires_at=now() + timedelta(minutes=30)
                    )
                )
                mail.send(
                    rt.cfg, user.email, "重置 FDE 密码", rt.cfg.public_url + "/reset-password#token=" + token
                )
        return {"ok": True, "message": "若账号存在，重置邮件已发送"}

    @app.post("/api/auth/password-reset/confirm")
    def reset_confirm(data: q.ResetConfirm, request: Request):
        origin_guard(request)
        rate_limit(rt.db, "reset-confirm:" + request.client.host, limit=10)
        check_password(data.password)
        with rt.db.session() as s:
            reset = s.scalar(
                select(PasswordReset).where(PasswordReset.token_hash == digest(data.token)).with_for_update()
            )
            if not reset or reset.used or utc(reset.expires_at) <= now():
                raise Problem("RESET_INVALID", "重置链接已过期或失效")
            s.get(User, reset.user_id).password_hash = PASSWORDS.hash(data.password)
            reset.used = True
            for session in s.scalars(select(LoginSession).where(LoginSession.user_id == reset.user_id)):
                session.revoked = True
        security_event(rt.db, "auth.password_reset", reset.user_id)
        return {"ok": True}

    @app.get("/api/session")
    def session(ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            user = s.get(User, ctx.user_id)
            memberships = list(
                s.scalars(
                    select(Membership).where(Membership.user_id == ctx.user_id, Membership.active.is_(True))
                )
            )
            tenants = [
                {"id": m.tenant_id, "name": tenant.name, "role": m.role}
                for m in memberships
                if (tenant := s.get(Tenant, m.tenant_id)) and tenant.active
            ]
            current = s.get(Tenant, ctx.tenant_id)
            providers = [
                row_view(p)
                for p in list(
                    s.scalars(
                        select(ModelProvider).where(
                            ModelProvider.tenant_id == ctx.tenant_id,
                            ModelProvider.owner_id == ctx.user_id,
                            ModelProvider.active.is_(True),
                        )
                    )
                )
            ]
            return {
                "user": {"id": user.id, "name": user.name, "email": user.email},
                "tenants": tenants,
                "tenant_id": ctx.tenant_id,
                "role": ctx.role,
                "platform_admin": ctx.platform_admin,
                "models": current.allowed_models,
                "chat_models": business_models(current.allowed_models),
                "providers": providers,
                "router_bound": bool(current.router_account),
            }

    @app.get("/api/models/providers")
    def providers(ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            return [
                row_view(row)
                for row in list(
                    s.scalars(
                        select(ModelProvider)
                        .where(
                            ModelProvider.tenant_id == ctx.tenant_id,
                            ModelProvider.owner_id == ctx.user_id,
                            ModelProvider.active.is_(True),
                        )
                        .order_by(ModelProvider.created_at)
                    )
                )
            ]

    @app.post("/api/models/providers/catalog")
    def provider_catalog(data: q.ModelCatalogInput, ctx=Depends(authenticate)):
        """Return models from an unsaved provider configuration.

        The submitted API key is used for this request only and is never
        written to the database or returned to the browser.
        """
        models = fetch_model_catalog(rt.cfg, data.api_base, data.protocol, data.api_key)
        return {"models": models}

    @app.post("/api/models/providers/{provider_id}/catalog")
    def saved_provider_catalog(
        provider_id: str, data: q.ModelCatalogSavedInput, ctx=Depends(authenticate)
    ):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = s.scalar(
                select(ModelProvider).where(
                    ModelProvider.tenant_id == ctx.tenant_id,
                    ModelProvider.owner_id == ctx.user_id,
                    ModelProvider.provider_id == provider_id,
                    ModelProvider.active.is_(True),
                )
            )
            if not row:
                raise Problem("PROVIDER_NOT_FOUND", status=404)
            from .providers import decrypt

            api_base = data.api_base or row.api_base
            protocol = data.protocol or row.protocol
            api_key = decrypt(rt.cfg, row.encrypted_api_key)
        return {"models": fetch_model_catalog(rt.cfg, api_base, protocol, api_key)}

    @app.post("/api/models/providers")
    def provider_create(data: q.ModelProviderInput, ctx=Depends(authenticate)):
        base = validate_base(rt.cfg, data.api_base)
        if data.default_model and data.default_model not in data.models:
            raise Problem("MODEL_INVALID", "默认模型必须在模型目录中")
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = ModelProvider(
                tenant_id=ctx.tenant_id,
                owner_id=ctx.user_id,
                provider_id=data.provider_id,
                display_name=data.display_name,
                kind=data.kind,
                api_base=base,
                protocol=data.protocol,
                encrypted_api_key=encrypt(rt.cfg, data.api_key),
                models=data.models,
                default_model=data.default_model,
            )
            s.add(row)
            s.flush()
            audit(s, ctx, "model_provider.created", row.id, **{"provider_id": row.provider_id})
            return row_view(row)

    @app.patch("/api/models/providers/{provider_id}")
    def provider_patch(provider_id: str, data: q.ModelProviderPatch, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = s.scalar(
                select(ModelProvider)
                .where(
                    ModelProvider.tenant_id == ctx.tenant_id,
                    ModelProvider.owner_id == ctx.user_id,
                    ModelProvider.provider_id == provider_id,
                    ModelProvider.active.is_(True),
                )
                .with_for_update()
            )
            if not row:
                raise Problem("PROVIDER_NOT_FOUND", status=404)
            changes = data.model_dump(exclude_none=True)
            if "api_base" in changes:
                changes["api_base"] = validate_base(rt.cfg, changes["api_base"])
            models = changes.get("models", row.models)
            default_model = changes.get("default_model", row.default_model)
            if default_model and default_model not in models:
                raise Problem("MODEL_INVALID", "默认模型必须在模型目录中")
            if "api_key" in changes:
                row.encrypted_api_key = encrypt(rt.cfg, changes.pop("api_key"))
                row.verified_at = None
            for key, value in changes.items():
                setattr(row, key, value)
            audit(s, ctx, "model_provider.updated", row.id, **{"provider_id": provider_id})
            return row_view(row)

    @app.post("/api/models/providers/{provider_id}/test")
    def provider_test(provider_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = s.scalar(
                select(ModelProvider)
                .where(
                    ModelProvider.tenant_id == ctx.tenant_id,
                    ModelProvider.owner_id == ctx.user_id,
                    ModelProvider.provider_id == provider_id,
                    ModelProvider.active.is_(True),
                )
                .with_for_update()
            )
            if not row:
                raise Problem("PROVIDER_NOT_FOUND", status=404)
            from .providers import decrypt

            models = test_provider(rt.cfg, row, decrypt(rt.cfg, row.encrypted_api_key))
            row.models = sorted(set(row.models) | set(models))
            row.verified_at = datetime.now(UTC)
            row.last_error = None
            audit(s, ctx, "model_provider.verified", row.id, **{"provider_id": provider_id})
            return row_view(row)

    @app.delete("/api/models/providers/{provider_id}")
    def provider_delete(provider_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = s.scalar(
                select(ModelProvider)
                .where(
                    ModelProvider.tenant_id == ctx.tenant_id,
                    ModelProvider.owner_id == ctx.user_id,
                    ModelProvider.provider_id == provider_id,
                )
                .with_for_update()
            )
            if not row:
                raise Problem("PROVIDER_NOT_FOUND", status=404)
            row.active = False
            row.encrypted_api_key = encrypt(rt.cfg, secrets.token_urlsafe(32))
            audit(s, ctx, "model_provider.deleted", row.id, **{"provider_id": provider_id})
        return {"ok": True}

    @app.post("/api/tenants/{tenant_id}/switch")
    def switch(tenant_id: str, ctx=Depends(authenticate)):
        with rt.db.session(tenant_id, ctx.user_id) as s:
            m = s.scalar(
                select(Membership).where(
                    Membership.tenant_id == tenant_id,
                    Membership.user_id == ctx.user_id,
                    Membership.active.is_(True),
                )
            )
            tenant = s.get(Tenant, tenant_id)
            if not m or not tenant or not tenant.active:
                raise Problem("NOT_FOUND", status=404)
            s.get(LoginSession, ctx.session_id).tenant_id = tenant_id
        return {"tenant_id": tenant_id}

    @app.post("/api/tenants")
    def create_tenant(data: q.TenantName, ctx=Depends(authenticate)):
        require_platform_admin(ctx)
        tenant_id = uid()
        rate_limit(rt.db, "tenant-create:" + ctx.user_id, limit=5, seconds=86400)
        with rt.db.session(tenant_id, ctx.user_id) as s:
            s.add(Tenant(id=tenant_id, name=data.name, budget_micros=rt.cfg.max_budget_micros))
            s.flush()
            s.add(TenantDirectory(id=tenant_id))
            s.add(Membership(tenant_id=tenant_id, user_id=ctx.user_id, role="tenant_owner"))
            audit(s, Context(ctx.user_id, tenant_id, "tenant_owner"), "organization.created", tenant_id)
        return {"id": tenant_id}

    @app.get("/api/tenant")
    def current_tenant(ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            tenant = s.get(Tenant, ctx.tenant_id)
            return {"id": tenant.id, "name": tenant.name}

    @app.patch("/api/tenant")
    def update_current_tenant(data: q.TenantName, ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            tenant = s.get(Tenant, ctx.tenant_id)
            previous = tenant.name
            tenant.name = data.name
            audit(s, ctx, "organization.renamed", tenant.id, previous_name=previous, name=tenant.name)
            s.flush()
            return {"id": tenant.id, "name": tenant.name}

    @app.get("/api/members")
    def members(ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            rows = s.execute(
                select(Membership, User)
                .join(User, Membership.user_id == User.id)
                .where(Membership.tenant_id == ctx.tenant_id)
            ).all()
            return [{**view(m), "name": u.name, "email": u.email} for m, u in rows]

    @app.post("/api/invitations")
    def invite(data: q.Invite, ctx=Depends(authenticate)):
        require_admin(ctx)
        if data.role == "tenant_admin":
            require_admin(ctx, owner=True)
        rate_limit(rt.db, "invite:" + ctx.tenant_id + ":" + ctx.user_id, limit=20, seconds=3600)
        token = secrets.token_urlsafe(32)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            inv = Invitation(
                tenant_id=ctx.tenant_id,
                email=str(data.email).lower(),
                role=data.role,
                token_hash=digest(token),
                expires_at=now() + timedelta(days=3),
                invited_by=ctx.user_id,
            )
            s.add(inv)
            s.flush()
            link = rt.cfg.public_url + "/invite#tenant=" + ctx.tenant_id + "&token=" + token
            sent = mail.send(rt.cfg, inv.email, "邀请加入 FDE 组织", link)
            audit(s, ctx, "member.invited", inv.id, role=data.role)
            return {
                "id": inv.id,
                "email_sent": sent,
                **({"development_link": link} if not rt.cfg.production else {}),
            }

    @app.post("/api/invitations/accept")
    def accept(data: q.AcceptInvite, request: Request):
        origin_guard(request)
        rate_limit(rt.db, "accept:" + request.client.host, limit=10)
        with rt.db.session(data.tenant_id) as s:
            inv = s.scalar(
                select(Invitation)
                .where(Invitation.tenant_id == data.tenant_id, Invitation.token_hash == digest(data.token))
                .with_for_update()
            )
            if not inv or inv.accepted or utc(inv.expires_at) <= now():
                raise Problem("INVITATION_INVALID", "邀请已失效")
            issuer = s.scalar(
                select(Membership).where(
                    Membership.tenant_id == data.tenant_id,
                    Membership.user_id == inv.invited_by,
                    Membership.active.is_(True),
                )
            )
            tenant = s.get(Tenant, data.tenant_id)
            if not issuer or issuer.role not in ADMINS or not tenant or not tenant.active:
                raise Problem("INVITATION_INVALID", "邀请者的组织管理权限已失效")
            if inv.role == "tenant_admin" and issuer.role != "tenant_owner":
                raise Problem("INVITATION_INVALID", "管理员邀请已失效")
            user = s.scalar(select(User).where(User.email == inv.email))
            if user:
                try:
                    PASSWORDS.verify(user.password_hash, data.password)
                except Exception as exc:
                    raise Problem("INVALID_CREDENTIALS", "已有账号请使用原密码接受邀请", 401) from exc
                if not user.active:
                    raise Problem("INVALID_CREDENTIALS", status=401)
            else:
                check_password(data.password)
                user = User(email=inv.email, name=data.name, password_hash=PASSWORDS.hash(data.password))
                s.add(user)
                s.flush()
            member = s.scalar(
                select(Membership).where(
                    Membership.tenant_id == data.tenant_id, Membership.user_id == user.id
                )
            )
            if member:
                if member.role != "tenant_owner":
                    member.role = inv.role
                member.active = True
            else:
                s.add(Membership(tenant_id=data.tenant_id, user_id=user.id, role=inv.role))
            inv.accepted = True
            audit(s, Context(user.id, data.tenant_id, inv.role), "member.joined", user.id)
        return login_response(rt, user, data.tenant_id)

    @app.patch("/api/members/{user_id}")
    def member_edit(user_id: str, data: q.MemberEdit, ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            member = s.scalar(
                select(Membership)
                .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
                .with_for_update()
            )
            if not member:
                raise Problem("NOT_FOUND", status=404)
            if member.role == "tenant_owner" or user_id == ctx.user_id:
                raise Problem("OWNER_PROTECTED", "不能通过此入口停用自己或修改组织所有者", 409)
            if member.role in ADMINS or data.role in ADMINS:
                require_admin(ctx, owner=True)
            member.role, member.active = data.role, data.active
            audit(s, ctx, "member.changed", user_id, role=data.role, active=data.active)
        return {"ok": True}

    @app.get("/api/workspaces")
    def workspaces(deleted: bool = False, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            return [view(w) for w in visible_workspaces(s, ctx, deleted)]

    @app.post("/api/workspaces")
    def workspace_create(data: q.WorkspaceCreate, ctx=Depends(authenticate)):
        if ctx.role == "billing_viewer":
            raise Problem("FORBIDDEN", status=403)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = Workspace(tenant_id=ctx.tenant_id, name=data.name, objective=data.objective,
                           next_action="collect_material", owner_id=ctx.user_id)
            s.add(ws)
            s.flush()
            audit(s, ctx, "workspace.created", ws.id, ws.id)
            return view(ws)

    @app.get("/api/workspaces/{workspace_id}")
    def workspace_read(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id)
            audit(s, ctx, "workspace.viewed", ws.id, ws.id)

            def rows(cls):
                values = []
                for x in s.scalars(
                    select(cls)
                    .where(cls.tenant_id == ctx.tenant_id, cls.workspace_id == ws.id)
                    .order_by(cls.created_at)
                ):
                    if cls is Artifact and not sources_visible(s, ctx, ws.id, x.citations):
                        values.append(
                            {
                                "id": x.id,
                                "kind": x.kind,
                                "version": x.version,
                                "status": x.status,
                                "body": {"说明": "引用资料已撤销、更新或未授权，请重新生成"},
                                "citations": [],
                                "source_unavailable": True,
                            }
                        )
                    elif cls is Job and not sources_visible(s, ctx, ws.id, x.payload.get("citations", [])):
                        values.append(view(x, {"result"}))
                    else:
                        values.append(view(x))
                return values

            from .workflow import state as workflow_state

            return {
                "workspace": view(ws),
                "workflow": workflow_state(s, ws, ctx.user_id),
                "evidence": rows(Evidence),
                "artifacts": rows(Artifact),
                "jobs": rows(Job),
                "catalogs": rows(Catalog),
                "refinements": rows(Refinement),
                "exports": rows(Export),
                "editable": ws.owner_id == ctx.user_id
                or ctx.role in ADMINS
                or bool(
                    s.scalar(
                        select(WorkspaceMember).where(
                            WorkspaceMember.workspace_id == ws.id,
                            WorkspaceMember.tenant_id == ctx.tenant_id,
                            WorkspaceMember.user_id == ctx.user_id,
                            WorkspaceMember.role == "workspace_editor",
                        )
                    )
                ),
            }

    @app.patch("/api/workspaces/{workspace_id}/objective")
    def objective(workspace_id: str, data: q.Name, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            ws.objective = data.name
            ws.next_action = "summarize"
            audit(s, ctx, "workflow.objective_set", ws.id, workspace_id)
            return {"objective": ws.objective}

    @app.get("/api/workspaces/{workspace_id}/workflow")
    def workflow(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id)
            from .workflow import state as workflow_state

            return workflow_state(s, ws, ctx.user_id)

    @app.post("/api/workspaces/{workspace_id}/workflow/start")
    def workflow_start(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            from .workflow import start_run, state as workflow_state

            start_run(s, ws, ctx.user_id, ws.phase)
            return workflow_state(s, ws, ctx.user_id)

    @app.patch("/api/workspaces/{workspace_id}")
    def rename(workspace_id: str, data: q.Name, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            ws.name = data.name
            audit(s, ctx, "workspace.renamed", ws.id, ws.id)
        return {"ok": True}

    def manage_workspace(s, ctx, workspace_id, deleted=False):
        ws = require_workspace(s, ctx, workspace_id, write=True, deleted=deleted)
        if ws.owner_id != ctx.user_id and ctx.role not in ADMINS:
            raise Problem("FORBIDDEN", "仅创建者或组织管理员可管理共享、归档和恢复", 403)
        return ws

    @app.delete("/api/workspaces/{workspace_id}")
    def ws_delete(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = manage_workspace(s, ctx, workspace_id)
            ws.deleted = True
            audit(s, ctx, "workspace.deleted", ws.id, ws.id)
        return {"ok": True}

    @app.post("/api/workspaces/{workspace_id}/restore")
    def restore(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = manage_workspace(s, ctx, workspace_id, deleted=True)
            ws.deleted = False
            audit(s, ctx, "workspace.restored", ws.id, ws.id)
        return {"ok": True}

    @app.get("/api/workspaces/{workspace_id}/members")
    def ws_members(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_workspace(s, ctx, workspace_id)
            return [
                view(m)
                for m in s.scalars(
                    select(WorkspaceMember).where(
                        WorkspaceMember.tenant_id == ctx.tenant_id,
                        WorkspaceMember.workspace_id == workspace_id,
                    )
                )
            ]

    @app.get("/api/workspaces/{workspace_id}/collaborators")
    def collaborators(workspace_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            manage_workspace(s, ctx, workspace_id)
            rows = s.execute(
                select(User.id, User.name, User.email)
                .join(Membership, Membership.user_id == User.id)
                .where(
                    Membership.tenant_id == ctx.tenant_id,
                    Membership.active.is_(True),
                    Membership.role != "billing_viewer",
                    User.active.is_(True),
                )
            ).all()
            return [{"id": r.id, "name": r.name, "email": r.email} for r in rows]

    @app.post("/api/workspaces/{workspace_id}/members")
    def ws_invite(workspace_id: str, data: q.WorkspaceInvite, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            manage_workspace(s, ctx, workspace_id)
            m = s.scalar(
                select(Membership).where(
                    Membership.tenant_id == ctx.tenant_id,
                    Membership.user_id == data.user_id,
                    Membership.active.is_(True),
                )
            )
            if not m or m.role == "billing_viewer":
                raise Problem("MEMBER_INVALID", "用户不是组织中的有效业务成员")
            wm = s.scalar(
                select(WorkspaceMember).where(
                    WorkspaceMember.tenant_id == ctx.tenant_id,
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == data.user_id,
                )
            )
            if wm:
                wm.role = data.role
            else:
                s.add(
                    WorkspaceMember(
                        tenant_id=ctx.tenant_id,
                        workspace_id=workspace_id,
                        user_id=data.user_id,
                        role=data.role,
                    )
                )
            audit(s, ctx, "workspace.member_changed", data.user_id, workspace_id, role=data.role)
        return {"ok": True}

    @app.delete("/api/workspaces/{workspace_id}/members/{user_id}")
    def ws_revoke(workspace_id: str, user_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            manage_workspace(s, ctx, workspace_id)
            s.execute(
                delete(WorkspaceMember).where(
                    WorkspaceMember.tenant_id == ctx.tenant_id,
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == user_id,
                )
            )
            audit(s, ctx, "workspace.member_revoked", user_id, workspace_id)
        return {"ok": True}

    @app.post("/api/workspaces/{workspace_id}/evidence")
    def evidence_add(workspace_id: str, data: q.EvidenceInput, ctx=Depends(authenticate)):
        from ps.core.guard import scan_text

        if data.sensitivity == "restricted" or scan_text(data.text):
            raise Problem("INPUT_SENSITIVE", "内容含受限或敏感信息，请脱敏后提交", 422)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            row = Evidence(
                tenant_id=ctx.tenant_id,
                workspace_id=workspace_id,
                text=data.text,
                sensitivity=data.sensitivity,
            )
            s.add(row)
            ws.version += 1
            s.flush()
            audit(s, ctx, "evidence.added", row.id, workspace_id)
            next_job = None
            ai_error = None
            if (
                data.start_ai
                and s.scalar(
                    select(func.count())
                    .select_from(Evidence)
                    .where(Evidence.tenant_id == ctx.tenant_id, Evidence.workspace_id == workspace_id)
                )
                == 1
            ):
                from .jobs import enqueue

                try:
                    # An optional AI job must not roll back the user's material.
                    with s.begin_nested():
                        next_job = enqueue(rt, s, ctx, workspace_id, "summarize", {}, uuid.uuid4().hex,
                                           model=data.model, provider_id=data.provider_id)
                        ws.next_action = "summarize"
                except Problem as exc:
                    ai_error = {"code": exc.code, "message": exc.message}
                    audit(s, ctx, "job.start_rejected", row.id, workspace_id, code=exc.code)
            return {"id": row.id, "kind": row.kind, "text": row.text, "evidence": view(row),
                    "next_job": view(next_job) if next_job else None, "ai_error": ai_error}

    @app.post("/api/workspaces/{workspace_id}/catalogs")
    async def upload_catalog(workspace_id: str, file: UploadFile = File(), ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_workspace(s, ctx, workspace_id, write=True)
        data = await file.read(rt.cfg.max_upload_bytes + 1)
        if len(data) > rt.cfg.max_upload_bytes:
            raise Problem("FILE_TOO_LARGE", status=413)
        from fdescan import CatalogInvalid, validate
        from fdescan.ddl import parse

        try:
            catalog = (
                await run_in_threadpool(parse, data.decode(), name=file.filename)
                if file.filename.endswith(".sql")
                else json.loads(data)
            )
            await run_in_threadpool(validate, catalog)
        except (ValueError, CatalogInvalid) as exc:
            raise Problem("CATALOG_INVALID", "仅接受合法的 DDL 或 fde-catalog/1 元数据文件", 422) from exc
        # Enums are values; SaaS deliberately refuses them even if a local scanner opted in.
        if any(col.get("enum_values") for a in catalog["catalog"]["assets"] for col in a["columns"]):
            raise Problem("CATALOG_VALUES_FORBIDDEN", "SaaS 目录不可包含枚举取值", 422)
        if len(catalog["catalog"]["assets"]) > 2000:
            raise Problem("CATALOG_TOO_LARGE", status=413)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            row = Catalog(
                tenant_id=ctx.tenant_id,
                workspace_id=workspace_id,
                name=file.filename[:160],
                body=catalog,
                digest=digest(data),
            )
            s.add(row)
            ws.version += 1
            s.flush()
            audit(s, ctx, "catalog.uploaded", row.id, workspace_id)
            return view(row)

    @app.post("/api/workspaces/{workspace_id}/agent/{capability}")
    def agent(workspace_id: str, capability: str, data: q.AgentInput, ctx=Depends(authenticate)):
        rate_limit(rt.db, "agent:" + ctx.tenant_id + ":" + ctx.user_id, limit=30, seconds=60)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_workspace(s, ctx, workspace_id, write=True)
            if capability == "learn":
                raise Problem("USE_DECISION_ENDPOINT", "反馈须从人工裁决入口产生", 403)
            return view(
                enqueue(
                    rt,
                    s,
                    ctx,
                    workspace_id,
                    capability,
                    data.params,
                    data.call_id,
                    data.model,
                    provider_id=data.provider_id,
                )
            )

    def require_artifact(s, ctx, artifact_id, write=False):
        row = s.scalar(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
        )
        if not row:
            raise Problem("NOT_FOUND", status=404)
        require_workspace(s, ctx, row.workspace_id, write=write)
        authorize_sources(s, ctx, row.workspace_id, row.citations)
        return row

    @app.post("/api/artifacts/{artifact_id}/decisions")
    def decide(artifact_id: str, data: q.DecisionInput, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = require_artifact(s, ctx, artifact_id, write=True)
            if row.version != data.version:
                raise Problem("VERSION_CONFLICT", "结果已更新，请刷新", 409)
            if data.action == "edit" and not data.body:
                raise Problem("EDIT_BODY_REQUIRED")
            before = row.body
            s.add(
                Decision(
                    tenant_id=ctx.tenant_id,
                    workspace_id=row.workspace_id,
                    artifact_id=row.id,
                    actor_id=ctx.user_id,
                    action=data.action,
                    body={
                        "before": before,
                        "after": data.body,
                        "reason": data.reason,
                        "version": row.version,
                    },
                )
            )
            event = {
                "artifact_id": row.upstream_id,
                "artifact_version": row.version,
                "decision": {"accept": "accepted", "edit": "edited", "reject": "rejected"}[data.action],
            }
            if data.action == "edit":
                from ps.core.guard import scan

                if scan(data.body):
                    raise Problem("INPUT_SENSITIVE", status=422)
                row.body = data.body
                row.version += 1
                event["edited_body"] = data.body
            if data.action == "reject":
                event.update(reason_code="other", reason_text=data.reason or "人工驳回")
            row.status = "rejected" if data.action == "reject" else "confirmed"
            s.get(Workspace, row.workspace_id).version += 1
            if row.upstream_id:
                enqueue(rt, s, ctx, row.workspace_id, "learn", {"events": [event]}, uuid.uuid4().hex)
            audit(s, ctx, "artifact.decided", row.id, row.workspace_id, decision=data.action)
            return view(row)

    @app.get("/api/jobs/{job_id}")
    def job_read(job_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            job = s.scalar(select(Job).where(Job.id == job_id, Job.tenant_id == ctx.tenant_id))
            if not job:
                raise Problem("NOT_FOUND", status=404)
            if job.workspace_id:
                require_workspace(s, ctx, job.workspace_id)
            elif job.kind == "ingestion":
                version = s.get(DocumentVersion, job.payload["params"]["version_id"])
                require_kb(s, ctx, s.get(Document, version.document_id).kb_id)
            elif job.user_id != ctx.user_id:
                raise Problem("NOT_FOUND", status=404)
            if job.workspace_id:
                authorize_sources(s, ctx, job.workspace_id, job.payload.get("citations", []))
            return view(job)

    @app.post("/api/jobs/{job_id}/retry")
    def job_retry(job_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            job = s.scalar(
                select(Job).where(Job.id == job_id, Job.tenant_id == ctx.tenant_id).with_for_update()
            )
            if not job or job.user_id != ctx.user_id:
                raise Problem("NOT_FOUND", status=404)
            if job.workspace_id:
                require_workspace(s, ctx, job.workspace_id, write=True)
            if job.status != "failed":
                raise Problem("JOB_NOT_FAILED", status=409)
            job.status, job.error, job.attempts = "queued", None, 0
            s.add(QueueTicket(tenant_id=ctx.tenant_id, job_id=job.id))
            audit(s, ctx, "job.requeued", job.id, job.workspace_id)
        return {"ok": True}

    # The remaining feature routes share the same authorization helpers.
    register_knowledge(app, rt)
    register_billing(app, rt)
    register_outputs(app, rt, require_artifact)
    return app


def register_knowledge(app, rt):
    @app.delete("/api/knowledge-bases/{kb_id}")
    def revoke_kb(kb_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            kb = require_kb(s, ctx, kb_id, write=True)
            kb.active = False
            audit(s, ctx, "knowledge.revoked", kb.id, kb.workspace_id)
        return {"ok": True}

    @app.get("/api/knowledge-bases")
    def list_kbs(workspace_id: str | None = None, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            result = []
            for kb in s.scalars(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.active.is_(True)
                )
            ):
                try:
                    require_kb(s, ctx, kb.id, workspace_id=workspace_id)
                    result.append(view(kb))
                except Problem:
                    continue
            return result

    @app.post("/api/knowledge-bases")
    def create_kb(data: q.KBInput, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            if data.workspace_id:
                require_workspace(s, ctx, data.workspace_id, write=True)
            else:
                require_admin(ctx)
            kb = KnowledgeBase(
                tenant_id=ctx.tenant_id,
                workspace_id=data.workspace_id,
                scope="workspace" if data.workspace_id else "tenant",
                name=data.name,
                description=data.description,
                owner_id=ctx.user_id,
            )
            s.add(kb)
            s.flush()
            audit(s, ctx, "knowledge.created", kb.id, kb.workspace_id)
            return view(kb)

    @app.get("/api/knowledge-bases/{kb_id}")
    def read_kb(kb_id: str, workspace_id: str | None = None, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            kb = require_kb(s, ctx, kb_id, workspace_id=workspace_id)
            docs = []
            for doc in s.scalars(
                select(Document).where(
                    Document.tenant_id == ctx.tenant_id, Document.kb_id == kb.id, Document.active.is_(True)
                )
            ):
                versions = list(
                    s.scalars(
                        select(DocumentVersion)
                        .where(
                            DocumentVersion.tenant_id == ctx.tenant_id, DocumentVersion.document_id == doc.id
                        )
                        .order_by(DocumentVersion.version.desc())
                    )
                )
                docs.append({**view(doc), "versions": [view(v) for v in versions]})
            grants = (
                [
                    view(g)
                    for g in s.scalars(
                        select(KnowledgeGrant).where(
                            KnowledgeGrant.tenant_id == ctx.tenant_id, KnowledgeGrant.kb_id == kb.id
                        )
                    )
                ]
                if ctx.role in ADMINS
                else []
            )
            return {"knowledge_base": view(kb), "documents": docs, "grants": grants}

    @app.post("/api/knowledge-bases/{kb_id}/grants")
    def grant(kb_id: str, data: q.GrantInput, ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            kb = require_kb(s, ctx, kb_id, write=True)
            if kb.scope != "tenant":
                raise Problem("GRANT_SCOPE", "工作区知识库继承工作区权限")
            if data.subject_type == "user":
                m = s.scalar(
                    select(Membership).where(
                        Membership.tenant_id == ctx.tenant_id,
                        Membership.user_id == data.subject_id,
                        Membership.active.is_(True),
                    )
                )
                if not m or m.role == "billing_viewer":
                    raise Problem("MEMBER_INVALID")
            elif data.subject_type == "workspace":
                require_workspace(s, ctx, data.subject_id)
            elif data.subject_id not in {"tenant_owner", "tenant_admin", "fde_member"}:
                raise Problem("ROLE_INVALID")
            row = KnowledgeGrant(tenant_id=ctx.tenant_id, kb_id=kb.id, **data.model_dump())
            s.add(row)
            s.flush()
            audit(
                s,
                ctx,
                "knowledge.granted",
                row.id,
                subject_type=data.subject_type,
                subject_id=data.subject_id,
            )
            return view(row)

    @app.delete("/api/knowledge-bases/{kb_id}/grants/{grant_id}")
    def revoke(kb_id: str, grant_id: str, ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_kb(s, ctx, kb_id, write=True)
            row = s.scalar(
                select(KnowledgeGrant).where(
                    KnowledgeGrant.tenant_id == ctx.tenant_id,
                    KnowledgeGrant.kb_id == kb_id,
                    KnowledgeGrant.id == grant_id,
                )
            )
            if not row:
                raise Problem("NOT_FOUND", status=404)
            s.delete(row)
            audit(s, ctx, "knowledge.grant_revoked", grant_id)
        return {"ok": True}

    @app.post("/api/knowledge-bases/{kb_id}/documents")
    async def upload(
        kb_id: str,
        file: UploadFile = File(),
        classification: str = Form("internal"),
        source: str = Form("upload"),
        tags: str = Form(""),
        document_id: str | None = Form(None),
        retention_days: int = Form(365),
        ctx=Depends(authenticate),
    ):
        from .storage import scan_file

        if classification not in {"public", "internal"}:
            raise Problem("INPUT_SENSITIVE", "SaaS 不接收 restricted 资料", 422)
        if not 1 <= retention_days <= 3650 or len(source) > 500 or len(tags) > 500:
            raise Problem("METADATA_INVALID")
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            kb = require_kb(s, ctx, kb_id, write=True)
        data = await file.read(rt.cfg.max_upload_bytes + 1)
        filename = Path(file.filename or "upload.txt").name[:200]
        await run_in_threadpool(scan_file, rt.cfg, data, filename)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            kb = require_kb(s, ctx, kb_id, write=True)
            if document_id:
                doc = s.scalar(
                    select(Document)
                    .where(
                        Document.id == document_id,
                        Document.tenant_id == ctx.tenant_id,
                        Document.kb_id == kb.id,
                        Document.active.is_(True),
                    )
                    .with_for_update()
                )
                if not doc:
                    raise Problem("NOT_FOUND", status=404)
                doc.current_version += 1
                doc.name, doc.classification = filename, classification
            else:
                doc = Document(
                    tenant_id=ctx.tenant_id,
                    kb_id=kb.id,
                    workspace_id=kb.workspace_id,
                    name=filename,
                    source=source,
                    tags=[x.strip() for x in tags.split(",") if x.strip()],
                    classification=classification,
                    expires_at=now() + timedelta(days=retention_days),
                )
                s.add(doc)
                s.flush()
            key = f"{ctx.tenant_id}/{kb.workspace_id or 'knowledge'}/{kb.id}/{doc.id}/{doc.current_version}/{uid()}"
            await run_in_threadpool(rt.objects.put, key, data, ctx.tenant_id)
            version = DocumentVersion(
                tenant_id=ctx.tenant_id,
                document_id=doc.id,
                version=doc.current_version,
                object_key=key,
                digest=digest(data),
            )
            s.add(version)
            s.flush()
            job = enqueue(
                rt,
                s,
                ctx,
                kb.workspace_id,
                "ingest",
                {"version_id": version.id},
                uuid.uuid4().hex,
                kind="ingestion",
            )
            audit(s, ctx, "document.uploaded", doc.id, kb.workspace_id, version=doc.current_version)
            return {"document": view(doc), "job": view(job)}

    def document_access(s, ctx, doc_id, write=False, workspace_id=None):
        doc = s.scalar(
            select(Document).where(
                Document.id == doc_id, Document.tenant_id == ctx.tenant_id, Document.active.is_(True)
            )
        )
        if not doc or (doc.expires_at and utc(doc.expires_at) <= now()):
            raise Problem("NOT_FOUND", status=404)
        require_kb(s, ctx, doc.kb_id, write=write, workspace_id=workspace_id)
        return doc

    @app.get("/api/documents/{doc_id}/download")
    def download(doc_id: str, workspace_id: str | None = None, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            doc = document_access(s, ctx, doc_id, workspace_id=workspace_id)
            version = s.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.tenant_id == ctx.tenant_id,
                    DocumentVersion.document_id == doc.id,
                    DocumentVersion.version == doc.current_version,
                )
            )
            audit(s, ctx, "document.downloaded", doc.id, doc.workspace_id)
            data = rt.objects.get(version.object_key, ctx.tenant_id)
        from urllib.parse import quote

        return Response(
            data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(doc.name, safe="")},
        )

    @app.get("/api/chunks/{chunk_id}")
    def chunk_read(chunk_id: str, workspace_id: str | None = None, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            chunk = s.scalar(select(Chunk).where(Chunk.id == chunk_id, Chunk.tenant_id == ctx.tenant_id))
            if not chunk:
                raise Problem("NOT_FOUND", status=404)
            doc = document_access(s, ctx, chunk.document_id, workspace_id=workspace_id)
            version = s.get(DocumentVersion, chunk.version_id)
            if version.version != doc.current_version or version.status != "ready":
                raise Problem("SOURCE_REVOKED", "引用的文档版本已撤销或更新", 410)
            return {
                "title": doc.name,
                "page": chunk.page,
                "paragraph": chunk.ordinal + 1,
                "text": chunk.text,
                "version": version.version,
            }

    @app.delete("/api/documents/{doc_id}")
    def doc_revoke(doc_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            doc = document_access(s, ctx, doc_id, write=True)
            doc.active = False
            audit(s, ctx, "document.revoked", doc.id, doc.workspace_id)
        return {"ok": True}

    @app.post("/api/knowledge/search")
    def search(data: q.SearchInput, ctx=Depends(authenticate)):
        from ps.core.guard import scan_text

        if scan_text(data.query):
            raise Problem("INPUT_SENSITIVE", status=422)
        return {"hits": retrieve(rt, ctx, data.workspace_id, data.query)}


def register_billing(app, rt):
    @app.post("/api/billing/binding")
    def bind(data: q.BindRouter, ctx=Depends(authenticate)):
        require_admin(ctx, owner=True)
        rt.router.capabilities()
        receipt = rt.router.request(
            "POST",
            "bindings/exchange",
            {
                "authorization_code": data.authorization_code,
                "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id,
            },
            digest(ctx.tenant_id + data.authorization_code),
        )
        if receipt.get("tenant_id") != ctx.tenant_id:
            raise Problem("ROUTER_BINDING_INVALID", status=502)
        account = rt.router.account(receipt["account_id"])
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            tenant = s.execute(
                select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update()
            ).scalar_one()
            pending = s.scalar(
                select(Job).where(
                    Job.tenant_id == ctx.tenant_id, Job.status.in_(["queued", "running", "retry_wait"])
                )
            )
            unsettled = s.scalar(
                select(RouterCall).where(
                    RouterCall.tenant_id == ctx.tenant_id,
                    RouterCall.reservation_id.is_not(None),
                    RouterCall.status.notin_(["settled", "released"]),
                )
            )
            if pending or unsettled:
                raise Problem("BILLING_BUSY", "仍有任务执行中，暂不能更换计费绑定", 409)
            tenant.router_account, tenant.allowed_models = receipt["account_id"], account["models"][:20]
            audit(s, ctx, "router.bound", tenant.id)
        return {"ok": True, "models": account["models"]}

    @app.get("/api/billing")
    def billing(ctx=Depends(authenticate)):
        if ctx.role not in ADMINS | {"billing_viewer"}:
            raise Problem("FORBIDDEN", status=403)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            tenant = s.get(Tenant, ctx.tenant_id)
            rows = list(
                s.scalars(
                    select(RouterCall)
                    .where(RouterCall.tenant_id == ctx.tenant_id)
                    .order_by(RouterCall.created_at.desc())
                    .limit(200)
                )
            )
        account = rt.router.account(tenant.router_account) if tenant.router_account else None
        from ps.db import CallLog

        with rt.pattern_db.session(ctx.tenant_id, ctx.user_id) as s:
            personal_logs = list(s.scalars(select(CallLog).where(
                CallLog.tenant_id == ctx.tenant_id, CallLog.model_alias.like("provider:%"),
            ).order_by(CallLog.created_at.desc()).limit(200)))
        personal_calls = [
            {"call_id": log.call_id, "capability": log.capability,
             "provider_id": log.model_alias.split(":", 2)[1], "model": log.model_alias.split(":", 2)[2],
             "calls": log.llm_calls, "tokens_in": log.tokens_in, "tokens_out": log.tokens_out,
             "latency_ms": log.latency_ms, "status": log.status,
             "created_at": utc(log.created_at).isoformat()}
            for log in personal_logs
        ]
        return {
            "account": {
                k: account.get(k)
                for k in ("available_micros", "currency", "models", "account_id", "recharge_url")
            }
            if account
            else None,
            "policy": {"models": tenant.allowed_models, "budget_micros": tenant.budget_micros},
            "calls": [view(r, {"response", "receipt", "input_hash"}) for r in rows],
            "personal_calls": personal_calls,
            "authority": "SudoRouter",
        }

    @app.patch("/api/billing/policy")
    def policy(data: q.Policy, ctx=Depends(authenticate)):
        require_admin(ctx, owner=True)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            tenant = s.get(Tenant, ctx.tenant_id)
            account = rt.router.account(tenant.router_account)
            if (
                not set(data.models).issubset(account["models"])
                or data.budget_micros > rt.cfg.max_budget_micros
            ):
                raise Problem("POLICY_INVALID", "策略超过平台或 Router 允许范围")
            tenant.allowed_models, tenant.budget_micros = data.models, data.budget_micros
            audit(s, ctx, "router.policy_changed", tenant.id)
        return {"ok": True}

    @app.post("/api/billing/reconcile")
    def run_reconcile(ctx=Depends(authenticate)):
        require_admin(ctx)
        return reconcile(rt, ctx.tenant_id)

    @app.get("/api/audit")
    def audit_list(offset: int = 0, exclude_reads: bool = False, ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            return present_audits(s, s.scalars(
                    select(Audit)
                    .where(Audit.tenant_id == ctx.tenant_id,
                           Audit.action != "workspace.viewed" if exclude_reads else True)
                    .order_by(Audit.created_at.desc(), Audit.id.desc())
                    .offset(max(0, offset))
                    .limit(200)
            ))

    @app.get("/api/metrics")
    def metrics(ctx=Depends(authenticate)):
        require_admin(ctx)
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            counts = {
                cls.__tablename__: s.scalar(
                    select(func.count()).select_from(cls).where(cls.tenant_id == ctx.tenant_id)
                )
                for cls in (Membership, Workspace, Document, Chunk, Job, RouterCall)
            }
            failures = s.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.tenant_id == ctx.tenant_id, Job.status == "failed")
            )
            usage = s.execute(
                select(
                    func.sum(RouterCall.tokens_in),
                    func.sum(RouterCall.tokens_out),
                    func.sum(RouterCall.amount_micros),
                ).where(RouterCall.tenant_id == ctx.tenant_id, RouterCall.status == "settled")
            ).one()
            amounts = dict(
                s.execute(
                    select(RouterCall.currency, func.sum(RouterCall.amount_micros))
                    .where(RouterCall.tenant_id == ctx.tenant_id, RouterCall.status == "settled")
                    .group_by(RouterCall.currency)
                ).all()
            )
            from ps.db import CallLog

            with rt.pattern_db.session(ctx.tenant_id, ctx.user_id) as ps:
                personal = ps.execute(select(
                    func.sum(CallLog.tokens_in), func.sum(CallLog.tokens_out), func.sum(CallLog.llm_calls),
                ).where(CallLog.tenant_id == ctx.tenant_id, CallLog.model_alias.like("provider:%"))).one()
            counts["personal_model_calls"] = personal[2] or 0
            return {
                "counts": counts,
                "failed_jobs": failures,
                "tokens_in": (usage[0] or 0) + (personal[0] or 0),
                "tokens_out": (usage[1] or 0) + (personal[1] or 0),
                "amount_by_currency": amounts,
            }


def register_outputs(app, rt, require_artifact):
    @app.post("/api/artifacts/{artifact_id}/revision/apply")
    def apply_revision(artifact_id: str, data: q.ApplyRevision, ctx=Depends(authenticate)):
        from .outputs import apply_changes

        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            artifact = require_artifact(s, ctx, artifact_id, write=True)
            return apply_changes(s, ctx, artifact, data.change_ids)

    @app.post("/api/revisions/{revision_id}/undo")
    def undo_revision(revision_id: str, ctx=Depends(authenticate)):
        from .outputs import undo_changes

        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            return undo_changes(s, ctx, revision_id)

    @app.get("/api/workspaces/{workspace_id}/refinement")
    def refinement(workspace_id: str, ctx=Depends(authenticate)):
        from .pattern import refinement_slots

        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_workspace(s, ctx, workspace_id)
            arts = list(
                s.scalars(
                    select(Artifact).where(
                        Artifact.tenant_id == ctx.tenant_id, Artifact.workspace_id == workspace_id
                    )
                )
            )
            arts = [a for a in arts if sources_visible(s, ctx, workspace_id, a.citations)]
            bindings = list(
                s.scalars(
                    select(Refinement).where(
                        Refinement.tenant_id == ctx.tenant_id, Refinement.workspace_id == workspace_id
                    )
                )
            )
            slots = refinement_slots(arts)
            slot_ids = {x["id"] for x in slots}
            return {"slots": slots, "bindings": [view(x) for x in bindings if x.slot_id in slot_ids]}

    @app.post("/api/workspaces/{workspace_id}/refinement")
    def refine_bind(workspace_id: str, data: q.RefinementInput, ctx=Depends(authenticate)):
        from .pattern import refinement_slots

        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            require_workspace(s, ctx, workspace_id, write=True)
            arts = list(
                s.scalars(
                    select(Artifact).where(
                        Artifact.tenant_id == ctx.tenant_id, Artifact.workspace_id == workspace_id
                    )
                )
            )
            arts = [a for a in arts if sources_visible(s, ctx, workspace_id, a.citations)]
            slots = {x["id"]: x for x in refinement_slots(arts)}
            if data.slot_id not in slots:
                raise Problem("SLOT_INVALID")
            catalog = list(
                s.scalars(
                    select(Catalog).where(
                        Catalog.tenant_id == ctx.tenant_id, Catalog.workspace_id == workspace_id
                    )
                )
            )
            fields = {
                f"{a['name']}.{c['name']}"
                for cat in catalog
                for a in cat.body["catalog"]["assets"]
                for c in a["columns"]
            }
            tables = {a["name"] + ".*" for cat in catalog for a in cat.body["catalog"]["assets"]}
            allowed = fields | (tables if slots[data.slot_id]["kind"] == "object" else set())
            if (
                data.action == "bind" and (not data.fields or not set(data.fields).issubset(allowed))
            ) or not set(data.grain).issubset(data.fields):
                raise Problem("BINDING_INVALID", "字段或粒度不在授权目录中")
            if data.action == "offline" and slots[data.slot_id]["kind"] != "step":
                raise Problem("OFFLINE_INVALID", "仅流程步骤可标记为线下")
            if data.action != "bind" and (data.fields or data.grain):
                raise Problem("BINDING_INVALID")
            row = s.scalar(
                select(Refinement)
                .where(
                    Refinement.tenant_id == ctx.tenant_id,
                    Refinement.workspace_id == workspace_id,
                    Refinement.slot_id == data.slot_id,
                )
                .with_for_update()
            )
            if (row.version if row else 0) != data.version:
                raise Problem("VERSION_CONFLICT", status=409)
            if row:
                row.body, row.version = data.model_dump(exclude={"version"}), row.version + 1
            else:
                row = Refinement(
                    tenant_id=ctx.tenant_id,
                    workspace_id=workspace_id,
                    slot_id=data.slot_id,
                    body=data.model_dump(exclude={"version"}),
                )
                s.add(row)
            workspace = s.get(Workspace, workspace_id)
            workspace.phase, workspace.version = "refine", workspace.version + 1
            audit(s, ctx, "refinement.changed", data.slot_id, workspace_id)
            s.flush()
            s.add(
                RefinementVersion(
                    tenant_id=ctx.tenant_id,
                    workspace_id=workspace_id,
                    slot_id=row.slot_id,
                    version=row.version,
                    actor_id=ctx.user_id,
                    body=row.body,
                )
            )
            return view(row)

    @app.post("/api/workspaces/{workspace_id}/exports")
    def export_create(workspace_id: str, ctx=Depends(authenticate)):
        from .outputs import bundle

        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            ws = require_workspace(s, ctx, workspace_id, write=True)
            source_refs = []
            for art in s.scalars(
                select(Artifact).where(
                    Artifact.tenant_id == ctx.tenant_id,
                    Artifact.workspace_id == ws.id,
                    Artifact.status.notin_(["rejected", "superseded"]),
                )
            ):
                if sources_visible(s, ctx, ws.id, art.citations):
                    source_refs.extend(art.citations)
            data = bundle(s, ctx, ws)
            row = Export(
                tenant_id=ctx.tenant_id,
                workspace_id=ws.id,
                user_id=ctx.user_id,
                name=f"business-v{ws.version}.zip",
                object_key=f"{ctx.tenant_id}/{ws.id}/exports/{uid()}.zip",
                digest=digest(data),
                version=ws.version,
                source_refs=source_refs,
            )
            rt.objects.put(row.object_key, data, ctx.tenant_id)
            s.add(row)
            s.flush()
            audit(s, ctx, "export.created", row.id, ws.id)
            return view(row)

    @app.get("/api/exports/{export_id}/download")
    def export_download(export_id: str, ctx=Depends(authenticate)):
        with rt.db.session(ctx.tenant_id, ctx.user_id) as s:
            row = s.scalar(select(Export).where(Export.id == export_id, Export.tenant_id == ctx.tenant_id))
            if not row:
                raise Problem("NOT_FOUND", status=404)
            require_workspace(s, ctx, row.workspace_id)
            authorize_sources(s, ctx, row.workspace_id, row.source_refs)
            audit(s, ctx, "export.downloaded", row.id, row.workspace_id)
            data = rt.objects.get(row.object_key, ctx.tenant_id)
            return Response(
                data,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{row.name}"'},
            )
