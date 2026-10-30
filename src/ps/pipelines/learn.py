"""learn 与 exposure（规格 §3.7；exposure 为缺口 C-2）。阶段一只记录，不驱动学习。

折叠规则：同一 (artifact, actor) 只取最后一次决策为终态；edited 后 accepted 记为 edited；
重复提交同一事件不改变统计（call_id 幂等）；没有反馈不等于驳回。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ps.core.store import get_artifact, get_workspace, idem_finish
from ps.db import ArtifactVersion, FeedbackEvent
from ps.models import ArtifactRef, ExposureRequest, ExposureResult, LearnRequest, LearnResult, Response, Versions
from ps.pipelines.runtime import Run


def run_learn(run: Run, req: LearnRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    get_workspace(s, tenant, wsid)
    # 先整体校验归属：任一产物越权，整个请求 FORBIDDEN
    resolved = [get_artifact(s, tenant, wsid, ev.artifact_id, ev.artifact_version) for ev in req.events]
    accepted, rejected, new_versions = 0, [], []
    try:
        for seq, (ev, (art, ver)) in enumerate(zip(req.events, resolved)):
            if ev.decision == "edited" and not ev.edited_body:
                rejected.append({"index": seq, "reason": "edited 必须带 edited_body"})
                continue
            if ev.decision == "rejected" and not ev.reason_code:
                rejected.append({"index": seq, "reason": "rejected 必须带 reason_code"})
                continue
            result_version = None
            if ev.decision == "edited":
                art.current_version += 1
                result_version = art.current_version
                s.add(ArtifactVersion(artifact_id=art.id, version=result_version, tenant_id=tenant,
                                      body={**ver.body, **ev.edited_body}, evidence_refs=ver.evidence_refs,
                                      produced_by={"edited_from": ver.version, "actor": run.ident.actor}))
                new_versions.append(ArtifactRef(artifact_id=art.id, version=result_version))
            s.add(FeedbackEvent(tenant_id=tenant, workspace_id=wsid, call_id=req.call_id, seq=seq,
                                actor=run.ident.actor, artifact_id=art.id, artifact_version=ev.artifact_version,
                                decision=ev.decision, result_version=result_version, reason_code=ev.reason_code,
                                reason_text=ev.reason_text, decided_at=ev.decided_at))
            accepted += 1
        resp = Response(call_id=req.call_id, capability="learn", versions=Versions(**run.versions()),
                        result=LearnResult(accepted=accepted, rejected=rejected, new_versions=new_versions)
                        ).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "learn", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp


def run_exposure(run: Run, req: ExposureRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    get_workspace(s, tenant, wsid)
    resolved = [get_artifact(s, tenant, wsid, it.artifact_id, it.version) for it in req.items]
    try:
        for it, (_, ver) in zip(req.items, resolved):
            ver.shown, ver.rank, ver.truncated_by = it.shown, it.rank, it.truncated_by
        resp = Response(call_id=req.call_id, capability="exposure", versions=Versions(**run.versions()),
                        result=ExposureResult(updated=len(resolved))).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "exposure", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp


def feedback_summary(s: Session, tenant_id: str, workspace_id: str | None = None) -> dict[str, Any]:
    """按折叠规则汇总终态。供评测与运营查看；校准（阶段三）也从这里取数。"""
    q = select(FeedbackEvent).where(FeedbackEvent.tenant_id == tenant_id)
    if workspace_id:
        q = q.where(FeedbackEvent.workspace_id == workspace_id)
    history: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ev in s.execute(q.order_by(FeedbackEvent.created_at, FeedbackEvent.id)).scalars():
        history[(ev.artifact_id, ev.actor)].append(ev.decision)
    final: dict[str, int] = {"accepted": 0, "edited": 0, "rejected": 0}
    for decisions in history.values():
        last = decisions[-1]
        if last == "accepted" and "edited" in decisions:
            last = "edited"
        final[last] += 1
    decided = sum(final.values())
    rate = (final["accepted"] + 0.5 * final["edited"]) / decided if decided else None
    return {**final, "decided": decided, "acceptance_rate": rate}

