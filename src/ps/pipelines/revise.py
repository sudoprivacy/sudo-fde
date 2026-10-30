"""revise（服务规格 v1.1 §5.11）：把一句修改意见变成可预览的修订集，不直接改任何东西。

硬校验（不靠提示词）：
1. update / remove 的 target 必须是请求 targets 里的路径，before 必须与当前文字一致（规范化比较），否则整条丢弃；
2. add 只允许"已有列表路径[+]"或 card:new:<kind>；
3. 每条改动都引用修改意见的证据 ID；ripple 必须有 why；
4. intent ≠ revise 或需要澄清时，changes 为空；
5. 超过 max_changes 截断并置 suggest_regenerate；
6. conflicts 的证据 ID 必须存在，quote 必须是该证据原文里的连续片段，否则去掉 quote。
修订集存一个产物（revision），每条改动另存一个子产物，FDE 的应用 / 修改 / 不采纳经 learn 逐条回流。
"""

from __future__ import annotations

import re
from typing import Any

from ps.core import prompts, schemas
from ps.core.errors import ServiceError
from ps.core.store import add_artifact, ensure_workspace, idem_finish
from ps.models import ReviseChangeOut, ReviseOut, ReviseRequest, Response, Versions
from ps.pipelines.runtime import Run

CARD_KINDS = ("process", "system", "question", "ontology")


def _norm(x: Any) -> str:
    return re.sub(r"\s+", "", str(x or ""))


def _clean(raw: dict[str, Any], req: ReviseRequest, evidence: dict[str, str]) -> tuple[dict[str, Any], int]:
    p = req.params
    targets = {t.path: t for t in p.targets}
    lists = {re.sub(r"\[\d+\]$", "", t.path) for t in p.targets if re.search(r"\[\d+\]$", t.path)}
    intent = raw.get("intent") if raw.get("intent") in ("revise", "material", "answer", "question", "unclear") else "unclear"
    if p.mode == "revise" and intent in ("material", "answer"):
        intent = "revise"
    clarify = raw.get("clarify") or None
    if clarify and not (clarify.get("text") or "").strip():
        clarify = None
    if intent == "unclear" and not clarify:
        clarify = {"text": "这句话是要修改已有的结果，还是补充新的资料？", "options": ["修改已有结果", "补充资料"]}
    out = {"intent": intent, "understood": str(raw.get("understood") or "")[:300], "clarify": clarify,
           "changes": [], "conflicts": [], "suggest_regenerate": bool(raw.get("suggest_regenerate"))}
    dropped = 0
    if intent == "revise" and not clarify:
        seen = set()
        for c in raw.get("changes") or []:
            target, op = str(c.get("target") or ""), c.get("op")
            role = c.get("role") if c.get("role") in ("direct", "ripple") else "direct"
            ok = False
            if op in ("update", "remove") and target in targets:
                ok = _norm(c.get("before")) == _norm(targets[target].text)
                if op == "update":
                    ok = ok and bool(_norm(c.get("after"))) and _norm(c.get("after")) != _norm(c.get("before"))
            elif op == "add":
                m = re.match(r"^card:new:(\w+)$", target)
                ok = bool(_norm(c.get("after"))) and ((m and m.group(1) in CARD_KINDS) or
                                                      (target.endswith("[+]") and target[:-3] in lists))
            if ok and role == "ripple" and not (c.get("why") or "").strip():
                ok = False
            if not ok or (target, op) in seen:
                dropped += 1
                continue
            seen.add((target, op))
            base = target if target in targets else None
            out["changes"].append({
                "target": target, "op": op, "role": role,
                "base_version": targets[base].version if base else None,
                "before": targets[target].text if op in ("update", "remove") else None,
                "after": str(c.get("after"))[:2000] if op != "remove" else None,
                "why": str(c.get("why") or "")[:300], "evidence": [p.instruction_evidence_id]})
        if len(out["changes"]) > p.max_changes:
            out["changes"] = out["changes"][:p.max_changes]
            out["suggest_regenerate"] = True
    for k in raw.get("conflicts") or []:
        eid = k.get("evidence")
        if eid not in evidence or eid == p.instruction_evidence_id:
            continue
        quote = str(k.get("quote") or "")
        out["conflicts"].append({"evidence": eid, "quote": quote if quote and quote in evidence[eid] else "",
                                 "note": str(k.get("note") or "")[:200]})
    return out, dropped


def run(run: Run, req: ReviseRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ensure_workspace(s, tenant, wsid)
    evidence = {e.id: e.text or "" for e in req.context.evidence}
    iid = req.params.instruction_evidence_id
    if iid not in evidence:
        raise ServiceError("SCHEMA_VIOLATION", "instruction_evidence_id 不在 context.evidence 里", evidence_id=iid)
    others = [e.model_dump() for e in req.context.evidence if e.id != iid]
    targets = [t.model_dump(exclude_none=True) for t in req.params.targets]
    raw = run.llm_json("revise.revise", "revise/revise_v1",
                       {"instruction": evidence[iid], "mode": req.params.mode, "targets": targets, "evidence": others},
                       {"instruction": evidence[iid], "instruction_id": iid, "mode": req.params.mode,
                        "max_changes": req.params.max_changes,
                        "targets": "\n".join(f"{t['path']}\t{t['text']}" for t in targets) or "（无）",
                        "evidence": prompts.render_evidence(others)}, schemas.REVISE)
    res, dropped = _clean(raw, req, evidence)
    try:
        produced = {"capability": "revise", **run.versions()}
        aid = add_artifact(s, tenant, wsid, req.call_id, "rv0", "revision",
                           {k: v for k, v in res.items() if k != "changes"} | {"instruction": iid},
                           [{"evidence_id": iid, "snapshot_id": None}], produced)
        changes = []
        for i, c in enumerate(res["changes"]):
            cid = add_artifact(s, tenant, wsid, req.call_id, f"rv_{i}", "revision_change", c,
                               [{"evidence_id": iid, "snapshot_id": None}], produced)
            changes.append(ReviseChangeOut(artifact_id=cid, version=1, **c))
        resp = Response(call_id=req.call_id, capability="revise", versions=Versions(**run.versions()),
                        result=ReviseOut(artifact_id=aid, changes=changes, dropped_invalid=dropped,
                                         **{k: v for k, v in res.items() if k != "changes"}),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "revise", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
