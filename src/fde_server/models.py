import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Scoped(Record):
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class User(Record, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Tenant(Record, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(150))
    router_account: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allowed_models: Mapped[list] = mapped_column(JSON, default=list)
    budget_micros: Mapped[int] = mapped_column(Integer, default=200000)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (Index("uq_tenants_name_normalized", func.lower(func.trim(name)), unique=True),)

    @validates("name")
    def normalize_name(self, key, value):
        from .tenant_names import normalize_tenant_name

        return normalize_tenant_name(value)


class TenantDirectory(Base):
    """Content-free dispatch directory, used only by background maintenance."""

    __tablename__ = "tenant_directory"
    id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)


class Membership(Scoped, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(Record, Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Invitation(Scoped, Base):
    __tablename__ = "invitations"
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    invited_by: Mapped[str] = mapped_column(String(64))


class PasswordReset(Record, Base):
    __tablename__ = "password_resets"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Workspace(Scoped, Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    name: Mapped[str] = mapped_column(String(150))
    owner_id: Mapped[str] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(24), default="intro")
    version: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    objective: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(String(40), default="collect_material")


class ModelProvider(Scoped, Base):
    __tablename__ = "model_providers"
    __table_args__ = (UniqueConstraint("tenant_id", "owner_id", "provider_id"),)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_id: Mapped[str] = mapped_column(String(80))
    display_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(24), default="sudorouter")
    api_base: Mapped[str] = mapped_column(String(300))
    protocol: Mapped[str] = mapped_column(String(24), default="openai")
    encrypted_api_key: Mapped[str] = mapped_column(Text)
    models: Mapped[list] = mapped_column(JSON, default=list)
    default_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class WorkflowRun(Scoped, Base):
    __tablename__ = "workflow_runs"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="active")
    started_by: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class WorkspaceMember(Scoped, Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workspace_id", "user_id"),
        ForeignKeyConstraint(["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"]),
    )
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(24))


class Evidence(Scoped, Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="user_quote")
    text: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(String(400), default="conversation")
    sensitivity: Mapped[str] = mapped_column(String(16), default="internal")
    metadata_: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Catalog(Scoped, Base):
    __tablename__ = "catalogs"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(160))
    body: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class Artifact(Scoped, Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    body: Mapped[dict] = mapped_column(JSON)
    upstream_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    citations: Mapped[list] = mapped_column(JSON, default=list)


class Decision(Scoped, Base):
    __tablename__ = "decisions"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(24))
    body: Mapped[dict] = mapped_column(JSON, default=dict)


class Revision(Scoped, Base):
    __tablename__ = "revisions"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(64))
    before: Mapped[list] = mapped_column(JSON)
    after: Mapped[list] = mapped_column(JSON)
    instruction: Mapped[str] = mapped_column(Text)
    evidence_id: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Refinement(Scoped, Base):
    __tablename__ = "refinements"
    __table_args__ = (UniqueConstraint("tenant_id", "workspace_id", "slot_id"),)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    slot_id: Mapped[str] = mapped_column(String(120))
    body: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)


class RefinementVersion(Scoped, Base):
    __tablename__ = "refinement_versions"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    slot_id: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("tenant_id", "workspace_id", "slot_id", "version"),)


class KnowledgeBase(Scoped, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class KnowledgeGrant(Scoped, Base):
    __tablename__ = "knowledge_grants"
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("tenant_id", "kb_id", "subject_type", "subject_id"),)


class Document(Scoped, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(500), default="upload")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    classification: Mapped[str] = mapped_column(String(16), default="internal")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentVersion(Scoped, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "version"),
        UniqueConstraint("tenant_id", "id"),
    )
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(500))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Chunk(Scoped, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index("ix_chunks_scope", "tenant_id", "kb_id", "document_id"),
    )
    kb_id: Mapped[str] = mapped_column(String(64), index=True)
    document_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list] = mapped_column(JSON)
    embedding_model: Mapped[str] = mapped_column(String(160))


class Retrieval(Scoped, Base):
    __tablename__ = "retrieval_events"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    query_hash: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(24))
    chunk_ids: Mapped[list] = mapped_column(JSON)


class Job(Scoped, Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("tenant_id", "call_id"), UniqueConstraint("tenant_id", "id"))
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    call_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    capability: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    budget_micros: Mapped[int] = mapped_column(Integer, default=200000)
    model: Mapped[str] = mapped_column(String(128), default="")
    base_version: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QueueTicket(Record, Base):
    """Dispatcher metadata contains no customer content; workers enter tenant scope before reading jobs."""

    __tablename__ = "queue_tickets"
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RouterCall(Scoped, Base):
    __tablename__ = "router_calls"
    __table_args__ = (UniqueConstraint("tenant_id", "operation_id"),)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str] = mapped_column(String(128))
    account_id: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    capability: Mapped[str] = mapped_column(String(32))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="created")
    budget_micros: Mapped[int] = mapped_column(Integer)
    reservation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    router_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    amount_micros: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)


class Export(Scoped, Base):
    __tablename__ = "exports"
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    object_key: Mapped[str] = mapped_column(String(500))
    digest: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)


class Audit(Scoped, Base):
    __tablename__ = "audit_events"
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class SecurityEvent(Record, Base):
    __tablename__ = "security_events"
    action: Mapped[str] = mapped_column(String(80))
    subject_hash: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


# Composite foreign keys enforce tenant ownership independently of application code.
for table in Base.metadata.tables.values():
    if "tenant_id" in table.c and table.name != "queue_tickets":
        table.append_constraint(ForeignKeyConstraint(["tenant_id"], ["tenants.id"]))
    if "workspace_id" in table.c and table.name not in {"workspace_members", "audit_events"}:
        table.append_constraint(
            ForeignKeyConstraint(["tenant_id", "workspace_id"], ["workspaces.tenant_id", "workspaces.id"])
        )
    for col, parent in [
        ("kb_id", "knowledge_bases"),
        ("document_id", "documents"),
        ("version_id", "document_versions"),
        ("artifact_id", "artifacts"),
    ]:
        if col in table.c:
            table.append_constraint(
                ForeignKeyConstraint(["tenant_id", col], [f"{parent}.tenant_id", f"{parent}.id"])
            )
