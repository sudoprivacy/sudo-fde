"""存储（规格 §4）。SQLAlchemy 2.0，SQLite 与 Postgres 通用。

租户隔离：所有私有表带 tenant_id，查询一律经 repo 函数按租户过滤（§3.1）。
Postgres 部署时另加 RLS 策略作为数据库层兜底——SQLite 不支持 RLS，本地开发只有应用层过滤。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "pattern_workspace"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0)   # CAS 版本（§3.6）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Idempotency(Base):
    __tablename__ = "pattern_idempotency"
    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    capability: Mapped[str] = mapped_column(String(32), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))          # in_progress | done
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkspaceState(Base):
    """BusinessHypothesis 版本链。"""

    __tablename__ = "pattern_workspace_state"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("pattern_workspace.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    hypothesis: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class HypothesisItem(Base):
    """unknown / contradiction 的稳定 ID 与权威状态；不随假设重写而变（§5.3）。"""

    __tablename__ = "pattern_hypothesis_item"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("pattern_workspace.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(16))             # unknown | contradiction
    topic: Mapped[str] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(String(8), default="medium")
    affects: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="open")   # open | answered | skipped
    merged_into: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class InterviewTurn(Base):
    __tablename__ = "pattern_interview_turn"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("pattern_workspace.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    round: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(String(64))
    question_artifacts: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[list] = mapped_column(JSON, default=list)
    stop: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Artifact(Base):
    __tablename__ = "pattern_artifact"
    __table_args__ = (UniqueConstraint("tenant_id", "workspace_id", "call_id", "temp_id"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("pattern_workspace.id"), index=True)
    call_id: Mapped[str] = mapped_column(String(64))
    temp_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(24))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ArtifactVersion(Base):
    """不可变正文 ＋ 证据引用 ＋ 分数 ＋ 展示情况（§3.4）。exposure 是唯一会被后写的字段组。"""

    __tablename__ = "pattern_artifact_version"
    artifact_id: Mapped[str] = mapped_column(ForeignKey("pattern_artifact.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    body: Mapped[dict] = mapped_column(JSON)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    produced_by: Mapped[dict] = mapped_column(JSON, default=dict)
    shown: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FeedbackEvent(Base):
    __tablename__ = "pattern_feedback_event"
    __table_args__ = (UniqueConstraint("tenant_id", "call_id", "seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("pattern_workspace.id"), index=True)
    call_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(80))
    artifact_id: Mapped[str] = mapped_column(ForeignKey("pattern_artifact.id"), index=True)
    artifact_version: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(12))
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CallLog(Base):
    __tablename__ = "pattern_call_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    workspace_id: Mapped[str] = mapped_column(String(80), index=True)
    call_id: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(String(24))
    pipeline_version: Mapped[str] = mapped_column(String(40))
    prompt_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    model_alias: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))       # SaaS 侧不存输入原文（§9.3）
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    dropped: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def make_engine(url: str):
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    Base.metadata.create_all(engine)
    return engine


def make_sessionmaker(url: str) -> sessionmaker:
    return sessionmaker(bind=make_engine(url), expire_on_commit=False)
