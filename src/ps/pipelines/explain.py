"""explain：为一条产物生成 ≤3 句人读理由；理由里出现输入之外的数字或名称时回退模板（规格 §5.4 旧版 / §3.3）。"""

from __future__ import annotations

import json
import re
from typing import Any

from ps.core import prompts, schemas
from ps.core.store import add_artifact, get_artifact, get_workspace, idem_finish
from ps.models import Explanation, ExplainRequest, Response, Versions
from ps.pipelines.runtime import Run

_TOKENS = re.compile(r"\d+(?:\.\d+)?|[A-Za-z][A-Za-z0-9_]{2,}")
_SENT = re.compile(r"[。！？!?]")


def _grounded(text: str, source: str) -> bool:
    return all(tok in source for tok in _TOKENS.findall(text))


def _template(body: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    quotes = "；".join(f"「{(e.get('text') or '')[:40]}」" for e in evidence[:2])
    return f"推荐「{body.get('statement', '')}」，依据：{quotes or '（无可展示的证据原文）'}"


def run(run: Run, req: ExplainRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    get_workspace(s, tenant, wsid)
    _, ver = get_artifact(s, tenant, wsid, req.params.target.artifact_id, req.params.target.version)
    body = ver.body
    cited = set(body.get("evidence") or [])
    evidence = [e.model_dump() for e in req.context.evidence if e.id in cited]

    out = run.llm_json("explain.explain", "explain/explain_v1", {"assertion": body, "evidence": evidence},
                       {"assertion": prompts.j(body), "evidence": prompts.render_evidence(evidence)},
                       schemas.EXPLAIN)
    text = (out.get("text") or "").strip()
    source = json.dumps(body, ensure_ascii=False) + "".join(e.get("text") or "" for e in evidence)
    fallback = (not text or len([x for x in _SENT.split(text) if x.strip()]) > 3 or not _grounded(text, source))
    if fallback:
        text = _template(body, evidence)

    try:
        aid = add_artifact(s, tenant, wsid, req.call_id, "ex0", "explanation",
                           {"text": text, "target": req.params.target.model_dump(), "template_fallback": fallback},
                           [{"evidence_id": e["id"], "snapshot_id": None} for e in evidence],
                           {"capability": "explain", **run.versions()})
        resp = Response(call_id=req.call_id, capability="explain", versions=Versions(**run.versions()),
                        result=Explanation(artifact_id=aid, target=req.params.target, text=text,
                                           evidence=[e["id"] for e in evidence], template_fallback=fallback),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "explain", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
