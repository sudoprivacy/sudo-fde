import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from fastapi import Request
from sqlalchemy import or_, select

from .models import (
    Audit,
    LoginSession,
    Membership,
    RateBucket,
    SecurityEvent,
    Tenant,
    User,
    Workspace,
    WorkspaceMember,
    now,
)

PASSWORDS = PasswordHasher()
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(24))
TENANT_ROLES = {"tenant_owner", "tenant_admin", "fde_member", "billing_viewer"}
ADMINS = {"tenant_owner", "tenant_admin"}


class Problem(Exception):
    def __init__(self, code, message=None, status=400):
        self.code, self.message, self.status = code, message or code, status
        super().__init__(self.message)


def digest(value):
    if not isinstance(value, bytes):
        value = str(value).encode()
    return hashlib.sha256(value).hexdigest()


def session_digest(secret, value):
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def check_password(value):
    if len(value) < 12 or len(value) > 128:
        raise Problem("PASSWORD_POLICY", "密码须为 12–128 个字符")


def audit(s, ctx, action, resource_id=None, workspace_id=None, **details):
    s.add(
        Audit(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            action=action,
            resource_id=resource_id,
            workspace_id=workspace_id,
            details=details,
        )
    )


def rate_limit(db, key, limit=10, seconds=600):
    bucket_key = digest(key)
    with db.session() as s:
        # Upsert creates the locking row atomically on both database backends.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sq_insert

        insert = pg_insert if db.engine.dialect.name == "postgresql" else sq_insert
        s.execute(
            insert(RateBucket)
            .values(key=bucket_key, count=0, expires_at=now() + timedelta(seconds=seconds))
            .on_conflict_do_nothing(index_elements=["key"])
        )
        row = s.execute(select(RateBucket).where(RateBucket.key == bucket_key).with_for_update()).scalar_one()
        if utc(row.expires_at) < now():
            row.count, row.expires_at = 0, now() + timedelta(seconds=seconds)
        blocked = row.count >= limit
        row.count += 1
    if blocked:
        raise Problem("RATE_LIMITED", "请求过于频繁，请稍后重试", 429)


@dataclass(frozen=True)
class Context:
    user_id: str
    tenant_id: str
    role: str
    session_id: str = ""
    platform_admin: bool = False


def authenticate(request: Request):
    rt = request.app.state.runtime
    token = request.cookies.get("fde_session", "")
    with rt.db.session() as s:
        session = (
            s.scalar(
                select(LoginSession).where(
                    LoginSession.token_hash == session_digest(rt.cfg.session_secret, token)
                )
            )
            if token
            else None
        )
        if (
            not session
            or session.revoked
            or utc(session.expires_at) <= now()
            or utc(session.last_seen) < now() - timedelta(minutes=rt.cfg.idle_minutes)
        ):
            raise Problem("UNAUTHENTICATED", "请先登录", 401)
        user = s.get(User, session.user_id)
        if not user or not user.active:
            raise Problem("UNAUTHENTICATED", "账号已停用", 401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-csrf-token", "")
            origin = request.headers.get("origin")
            if (
                not csrf
                or not hmac.compare_digest(session_digest(rt.cfg.session_secret, csrf), session.csrf_hash)
                or (origin and origin.rstrip("/") != rt.cfg.public_url.rstrip("/"))
            ):
                raise Problem("CSRF_INVALID", "会话校验失败，请刷新页面", 403)
        session.last_seen = now()
        uid, tid, sid = user.id, session.tenant_id, session.id
    if not tid:
        raise Problem("TENANT_REQUIRED", "请先选择组织", 403)
    with rt.db.session(tid, uid) as s:
        membership = s.scalar(
            select(Membership).where(
                Membership.tenant_id == tid, Membership.user_id == uid, Membership.active.is_(True)
            )
        )
        tenant = s.get(Tenant, tid)
        if not membership or not tenant or (not tenant.active and not user.platform_admin):
            raise Problem("TENANT_ACCESS_DENIED", "组织成员资格已失效", 403)
        return Context(uid, tid, membership.role, sid, bool(user.platform_admin))


def require_admin(ctx, owner=False):
    if ctx.role not in ({"tenant_owner"} if owner else ADMINS):
        raise Problem("FORBIDDEN", "没有管理权限", 403)


def require_platform_admin(ctx):
    if not ctx.platform_admin:
        raise Problem("FORBIDDEN", "没有平台管理权限", 403)


def require_workspace(s, ctx, workspace_id, write=False, deleted=False):
    member = s.scalar(
        select(Membership).where(
            Membership.tenant_id == ctx.tenant_id,
            Membership.user_id == ctx.user_id,
            Membership.active.is_(True),
        )
    )
    user, tenant = s.get(User, ctx.user_id), s.get(Tenant, ctx.tenant_id)
    if not member or not user or not user.active or not tenant or not tenant.active:
        raise Problem("NOT_FOUND", "工作区不存在或无权访问", 404)
    ws = s.scalar(select(Workspace).where(Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id))
    if not ws or (ws.deleted and not deleted) or member.role == "billing_viewer":
        raise Problem("NOT_FOUND", "工作区不存在或无权访问", 404)
    wm = s.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.tenant_id == ctx.tenant_id,
            WorkspaceMember.user_id == ctx.user_id,
        )
    )
    full = ws.owner_id == ctx.user_id or member.role in ADMINS
    if not full and (not wm or (write and wm.role != "workspace_editor")):
        raise Problem(
            "FORBIDDEN" if wm else "NOT_FOUND",
            "没有工作区编辑权限" if wm else "工作区不存在或无权访问",
            403 if wm else 404,
        )
    return ws


def visible_workspaces(s, ctx, deleted=False):
    q = select(Workspace).where(Workspace.tenant_id == ctx.tenant_id, Workspace.deleted.is_(deleted))
    if ctx.role == "billing_viewer":
        return []
    if ctx.role not in ADMINS:
        if deleted:
            return list(
                s.scalars(q.where(Workspace.owner_id == ctx.user_id).order_by(Workspace.created_at.desc()))
            )
        member_ids = select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.tenant_id == ctx.tenant_id, WorkspaceMember.user_id == ctx.user_id
        )
        q = q.where(or_(Workspace.owner_id == ctx.user_id, Workspace.id.in_(member_ids)))
    return list(s.scalars(q.order_by(Workspace.created_at.desc())))


def service_token(cfg, ctx, job_id, payload_hash):
    stamp = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": ctx.user_id,
            "tenant": ctx.tenant_id,
            "job": job_id,
            "payload_hash": payload_hash,
            "iss": "fde-bff",
            "aud": "fde-pattern",
            "iat": stamp,
            "exp": stamp + timedelta(seconds=60),
            "jti": secrets.token_hex(16),
        },
        cfg.service_secret,
        algorithm="HS256",
    )


def decode_service(cfg, authorization):
    try:
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError()
        return jwt.decode(
            authorization[7:],
            cfg.service_secret,
            algorithms=["HS256"],
            audience="fde-pattern",
            issuer="fde-bff",
            options={"require": ["exp", "iat", "jti", "sub", "tenant", "job", "payload_hash"]},
        )
    except (jwt.PyJWTError, ValueError):
        raise Problem("SERVICE_IDENTITY_INVALID", "服务身份无效", 401)


def security_event(db, action, subject, **details):
    with db.session() as s:
        s.add(SecurityEvent(action=action, subject_hash=digest(subject), details=details))
