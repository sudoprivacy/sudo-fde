"""存储操作。每个函数都带 tenant_id，私有数据只能经这里按租户读写（§3.1 §9.3）。"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ps.core.errors import ServiceError
from ps.db import (Artifact, ArtifactVersion, HypothesisItem, Idempotency, InterviewTurn, Workspace,
                   WorkspaceState)


_id_scope: ContextVar[tuple[str, dict[str, int]] | None] = ContextVar("pattern_identity_scope", default=None)


@contextmanager
def stable_identities(seed: str):
    """Keep intermediate references stable when a failed transaction is replayed.

    The SaaS worker pins the pipeline revision and request snapshot before entering
    this scope. Per-prefix ordinals are therefore reproducible across restarts.
    """
    token = _id_scope.set((seed, {}))
    try:
        yield
    finally:
        _id_scope.reset(token)


def new_id(prefix: str) -> str:
    scope = _id_scope.get()
    if scope is None:
        return f"{prefix}_{uuid.uuid4().hex[:20]}"
    seed, counters = scope
    index = counters.get(prefix, 0)
    counters[prefix] = index + 1
    return prefix + "_" + hashlib.sha256(f"{seed}:{prefix}:{index}".encode()).hexdigest()[:24]


def input_hash(payload: dict[str, Any]) -> str:
    """幂等比对用。expected_state_version 不计入：版本冲突后用同一 call_id 重试是合法的（§3.6）。"""
    p = {k: v for k, v in payload.items() if k != "expected_state_version"}
    return hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------- 工作区归属 ----------

def ensure_workspace(s: Session, tenant_id: str, workspace_id: str) -> Workspace:
    ws = s.get(Workspace, workspace_id)
    if ws is None:
        ws = Workspace(id=workspace_id, tenant_id=tenant_id, state_version=0)
        s.add(ws)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            ws = s.get(Workspace, workspace_id)
    if ws is None or ws.tenant_id != tenant_id:
        raise ServiceError("FORBIDDEN", "无权访问该工作区")
    return ws


def get_workspace(s: Session, tenant_id: str, workspace_id: str) -> Workspace:
    ws = s.get(Workspace, workspace_id)
    if ws is None or ws.tenant_id != tenant_id:
        raise ServiceError("FORBIDDEN", "无权访问该工作区")
    return ws


# ---------- 幂等 ----------

def idem_begin(s: Session, tenant_id: str, capability: str, call_id: str, h: str) -> dict[str, Any] | None:
    """返回已存的响应（重放）；或占位后返回 None。"""
    row = s.get(Idempotency, (tenant_id, capability, call_id))
    if row is not None:
        if row.input_hash != h:
            raise ServiceError("IDEMPOTENCY_CONFLICT", "同一 call_id 的请求内容不同")
        if row.status == "done":
            return row.response
        raise ServiceError("IDEMPOTENCY_IN_PROGRESS", "该 call_id 正在处理，请稍后重试")
    s.add(Idempotency(tenant_id=tenant_id, capability=capability, call_id=call_id, input_hash=h,
                      status="in_progress"))
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        return idem_begin(s, tenant_id, capability, call_id, h)
    return None


def idem_release(s: Session, tenant_id: str, capability: str, call_id: str) -> None:
    """失败的调用不留记录，允许同一 call_id 重试。"""
    s.rollback()
    s.execute(delete(Idempotency).where(Idempotency.tenant_id == tenant_id, Idempotency.capability == capability,
                                        Idempotency.call_id == call_id, Idempotency.status == "in_progress"))
    s.commit()


def idem_finish(s: Session, tenant_id: str, capability: str, call_id: str, response: dict[str, Any]) -> None:
    """在调用方的事务里执行，与产物、状态一起提交。"""
    s.execute(update(Idempotency)
              .where(Idempotency.tenant_id == tenant_id, Idempotency.capability == capability,
                     Idempotency.call_id == call_id)
              .values(status="done", response=response))


# ---------- 状态版本（CAS） ----------

def check_version(ws: Workspace, expected: int) -> None:
    if ws.state_version != expected:
        raise ServiceError("STATE_VERSION_CONFLICT", "假设状态版本不符", current_version=ws.state_version)


def cas_bump(s: Session, tenant_id: str, workspace_id: str, expected: int) -> int:
    res = s.execute(update(Workspace)
                    .where(Workspace.id == workspace_id, Workspace.tenant_id == tenant_id,
                           Workspace.state_version == expected)
                    .values(state_version=expected + 1))
    if res.rowcount != 1:
        current = s.execute(select(Workspace.state_version).where(Workspace.id == workspace_id)).scalar_one()
        raise ServiceError("STATE_VERSION_CONFLICT", "并发写入，假设状态已被更新", current_version=current)
    return expected + 1


def latest_hypothesis(s: Session, tenant_id: str, workspace_id: str) -> dict[str, Any] | None:
    row = s.execute(select(WorkspaceState)
                    .where(WorkspaceState.workspace_id == workspace_id, WorkspaceState.tenant_id == tenant_id)
                    .order_by(WorkspaceState.version.desc()).limit(1)).scalar_one_or_none()
    return row.hypothesis if row else None


def items(s: Session, tenant_id: str, workspace_id: str) -> list[HypothesisItem]:
    return list(s.execute(select(HypothesisItem)
                          .where(HypothesisItem.workspace_id == workspace_id, HypothesisItem.tenant_id == tenant_id)
                          .order_by(HypothesisItem.updated_at, HypothesisItem.id)).scalars())


def rounds_done(s: Session, tenant_id: str, workspace_id: str) -> list[InterviewTurn]:
    return list(s.execute(select(InterviewTurn)
                          .where(InterviewTurn.workspace_id == workspace_id, InterviewTurn.tenant_id == tenant_id)
                          .order_by(InterviewTurn.round)).scalars())


# ---------- 产物 ----------

def add_artifact(s: Session, tenant_id: str, workspace_id: str, call_id: str, temp_id: str, kind: str,
                 body: dict[str, Any], evidence_refs: list[dict[str, Any]], produced_by: dict[str, Any]) -> str:
    aid = new_id("art")
    s.add(Artifact(id=aid, tenant_id=tenant_id, workspace_id=workspace_id, call_id=call_id, temp_id=temp_id,
                   kind=kind, current_version=1))
    s.flush()
    s.add(ArtifactVersion(artifact_id=aid, version=1, tenant_id=tenant_id, body=body,
                          evidence_refs=evidence_refs, produced_by=produced_by))
    return aid


def get_artifact(s: Session, tenant_id: str, workspace_id: str, artifact_id: str,
                 version: int | None = None) -> tuple[Artifact, ArtifactVersion]:
    a = s.get(Artifact, artifact_id)
    if a is None or a.tenant_id != tenant_id or a.workspace_id != workspace_id:
        raise ServiceError("FORBIDDEN", "无权访问该产物", artifact_id=artifact_id)
    v = s.get(ArtifactVersion, (artifact_id, version or a.current_version))
    if v is None:
        raise ServiceError("FORBIDDEN", "产物版本不存在", artifact_id=artifact_id, version=version)
    return a, v


def find_cached(s: Session, tenant_id: str, workspace_id: str, kind: str, key: str) -> tuple[str, dict[str, Any]] | None:
    """按 (kind, temp_id=缓存键) 取本工作区最近一次的产物正文；用于计划、指纹、语义标注的缓存。"""
    row = s.execute(select(Artifact)
                    .where(Artifact.tenant_id == tenant_id, Artifact.workspace_id == workspace_id,
                           Artifact.kind == kind, Artifact.temp_id == key)
                    .order_by(Artifact.created_at.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        return None
    v = s.get(ArtifactVersion, (row.id, row.current_version))
    return row.id, v.body


def latest_of_kind(s: Session, tenant_id: str, workspace_id: str, kind: str) -> dict[str, Any] | None:
    row = s.execute(select(Artifact)
                    .where(Artifact.tenant_id == tenant_id, Artifact.workspace_id == workspace_id, Artifact.kind == kind)
                    .order_by(Artifact.created_at.desc()).limit(1)).scalar_one_or_none()
    if row is None:
        return None
    return s.get(ArtifactVersion, (row.id, row.current_version)).body
