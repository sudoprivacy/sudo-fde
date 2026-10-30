"""refine（第 ⑥ 步细化 Agent，规格 v1.0）：为槽位建议绑定字段、指标口径，或判为缺口 / 线下。

只读结构元数据（表名、列名、类型、键、行数，K49），不看记录值。
硬校验：槽位 ID 必须是请求里的；字段必须是请求 tables 里的"表.列"（大小写不敏感，回写成原样拼写）；
offline 只用于流程步骤；bind 没有合法字段的整条丢掉。每条建议各存一个产物，FDE 的接受 / 改 / 驳回经 learn 回流。
"""

from __future__ import annotations

from typing import Any

from ps.core import prompts, schemas
from ps.core.store import add_artifact, ensure_workspace, idem_finish
from ps.models import RefineOut, RefineRequest, RefineSuggestionOut, Response, Versions
from ps.pipelines.runtime import Run


def _field_index(req: RefineRequest) -> dict[str, str]:
    idx: dict[str, str] = {}
    for t in req.params.tables:
        idx[f"{t.name}.*".lower()] = f"{t.name}.*"
        for c in t.columns:
            idx[f"{t.name}.{c.name}".lower()] = f"{t.name}.{c.name}"
    return idx


def _clean(raw: dict[str, Any], req: RefineRequest) -> tuple[list[dict[str, Any]], int]:
    slots = {s.id: s for s in req.params.slots}
    idx = _field_index(req)
    out, dropped, seen = [], 0, set()
    for x in raw.get("suggestions") or []:
        sid, action = x.get("slot_id"), x.get("action")
        slot = slots.get(sid)
        if slot is None or sid in seen or action not in ("bind", "gap", "offline") or \
                (action == "offline" and slot.kind != "step"):
            dropped += 1
            continue
        fields = [idx[f.lower()] for f in x.get("fields") or [] if isinstance(f, str) and f.lower() in idx]
        if slot.kind != "object":
            fields = [f for f in fields if not f.endswith(".*")]
        metric = None
        if action == "bind":
            if not fields:
                dropped += 1
                continue
            m = x.get("metric") or None
            if m:
                grain = [idx[g.lower()] for g in m.get("grain") or [] if isinstance(g, str) and g.lower() in idx]
                metric = {"grain": grain, "scope": str(m.get("scope") or "")[:200]}
        else:
            fields = []
        seen.add(sid)
        out.append({"slot_id": sid, "action": action, "fields": fields[:6], "metric": metric,
                    "why": str(x.get("why") or "")[:300],
                    "confidence": max(0.0, min(1.0, float(x.get("confidence") or 0.5)))})
    return out, dropped


def suggest(run: Run, req: RefineRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ensure_workspace(s, tenant, wsid)
    slots = [x.model_dump() for x in req.params.slots]
    tables = [t.model_dump(exclude_none=True) for t in req.params.tables]
    raw = run.llm_json("refine.suggest", "refine/suggest_v1", {"slots": slots, "tables": tables},
                       {"slots": prompts.j(slots), "tables": prompts.j(tables)}, schemas.REFINE)
    rows, dropped = _clean(raw, req)
    try:
        produced = {"capability": "refine", **run.versions()}
        out = []
        for i, r in enumerate(rows):
            aid = add_artifact(s, tenant, wsid, req.call_id, f"rf_{i}", "refine_suggestion", r, [], produced)
            out.append(RefineSuggestionOut(artifact_id=aid, version=1, **r))
        resp = Response(call_id=req.call_id, capability="refine", versions=Versions(**run.versions()),
                        result=RefineOut(suggestions=out, dropped_invalid=dropped),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "refine", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
