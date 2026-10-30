"""summarize（第一步：业务概要 ＋ 挑战问题）与 report（第五步：业务理解与建设建议），规格 v0.7。

两者都不改假设状态（不需要 expected_state_version），只把结果存成产物，便于追溯与以后学习。
引用检查是硬规则：引用了不存在的证据 ID 的条目整条丢掉；没引用证据的事实字段内容清空。
"""

from __future__ import annotations

from typing import Any

from ps.core import prompts, schemas
from ps.core.store import add_artifact, ensure_workspace, idem_finish
from ps.models import (BusinessReportOut, BusinessSummaryOut, InterviewQuestion, ProposedAssertion, ReportRequest,
                       Response, SummarizeRequest, Versions)
from ps.pipelines.runtime import Run

_DIMS = ("challenge", "problem", "goal")
_DEFAULT_Q = {
    "challenge": ("当前业务上最大的挑战是什么？", "先知道最想突破的地方，后面的追问和建议都围绕它"),
    "problem": ("日常运营里，具体碰到了哪些问题？", "具体问题决定要看哪些流程和数据"),
    "goal": ("最希望先解决什么？解决到什么程度算成功？", "有了目标才能判断建什么系统、什么 Agent"),
}


def _cited(ids: list[str] | None, known: set[str]) -> list[str]:
    return [i for i in (ids or []) if i in known]


def _clean_summary(raw: dict[str, Any], known: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"one_liner": raw.get("one_liner", ""), "industry": raw.get("industry", "")}
    for key, field in (("offerings", "what"), ("customers", "who"), ("revenue_model", "how"), ("scale", "note")):
        v = raw.get(key) or {}
        ev = _cited(v.get("evidence"), known)
        out[key] = {field: (v.get(field) or "") if ev else "未提及", "evidence": ev}
    for key in ("business_lines", "value_chain", "systems_mentioned"):
        rows = []
        for r in raw.get(key) or []:
            rows.append({**r, "evidence": _cited(r.get("evidence"), known)})
        # 业务线与系统必须有出处；价值链环节允许空引用（是追问线索）
        out[key] = rows if key == "value_chain" else [r for r in rows if r["evidence"]]
    out["open_points"] = [x for x in (raw.get("open_points") or []) if x][:5]
    return out


def _questions(raw: list[dict[str, Any]]) -> list[InterviewQuestion]:
    by_dim = {}
    for q in raw or []:
        d = q.get("dimension")
        if d in _DIMS and d not in by_dim and (q.get("text") or "").strip():
            by_dim[d] = q
    out = []
    for i, d in enumerate(_DIMS):
        q = by_dim.get(d) or {"text": _DEFAULT_Q[d][0], "why": _DEFAULT_Q[d][1], "options": []}
        out.append(InterviewQuestion(id=f"ch_{d}", text=q["text"].strip(), why=(q.get("why") or _DEFAULT_Q[d][1]).strip(),
                                     options=[o for o in (q.get("options") or []) if o][:4], allow_other=True,
                                     kind="ask", dimension=d))
    return out


def summarize(run: Run, req: SummarizeRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ensure_workspace(s, tenant, wsid)
    evidence = [e.model_dump() for e in req.context.evidence if e.kind in ("user_quote", "doc_snippet")]
    known = {e["id"] for e in evidence}
    raw = run.llm_json("understand.summary", "understand/summary_v1", {"evidence": evidence},
                       {"evidence": prompts.render_evidence(evidence)}, schemas.SUMMARY)
    summary = _clean_summary(raw, known)
    questions = _questions(raw.get("challenge_questions") or [])
    refs = sorted({i for v in summary.values() if isinstance(v, dict) for i in v.get("evidence", [])}
                  | {i for v in summary.values() if isinstance(v, list) for r in v if isinstance(r, dict)
                     for i in r.get("evidence", [])})
    try:
        aid = add_artifact(s, tenant, wsid, req.call_id, "sum0", "business_summary",
                           {"summary": summary, "challenge_questions": [q.model_dump() for q in questions]},
                           [{"evidence_id": i, "snapshot_id": None} for i in refs],
                           {"capability": "summarize", **run.versions()})
        resp = Response(call_id=req.call_id, capability="summarize", versions=Versions(**run.versions()),
                        result=BusinessSummaryOut(artifact_id=aid, version=1, summary=summary,
                                                  challenge_questions=questions),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "summarize", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp


_REPORT_LISTS = ("business_problems", "processes", "systems_to_build", "agents_to_build")
_ONTO_LISTS = ("object_types", "link_types", "action_types")


def _clean_report(raw: dict[str, Any], known: set[str], tables: set[str]) -> tuple[dict[str, Any], int]:
    dropped = 0

    def keep(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal dropped
        out = []
        for r in rows or []:
            ev = _cited(r.get("evidence"), known)
            if not ev:
                dropped += 1
                continue
            r = {**r, "evidence": ev}
            if "backed_by" in r:   # 表名只能来自扫描到的表
                r["backed_by"] = [t for t in r["backed_by"] if t.lower() in tables]
            out.append(r)
        # 只有数据支持的排到后面（规格 v0.7 §5.7）
        return sorted(out, key=lambda r: r.get("basis") == "data")

    report = {"overview": raw.get("overview", "")}
    for k in _REPORT_LISTS:
        report[k] = keep(raw.get(k))
    onto = raw.get("ontology") or {}
    report["ontology"] = {k: keep(onto.get(k)) for k in _ONTO_LISTS}
    report["open_points"] = [x for x in (raw.get("open_points") or []) if x][:10]
    return report, dropped


_ONTO_TYPE = {"object_types": "object", "link_types": "link", "action_types": "action"}
_BASIS_CONF = {"both": 0.8, "said": 0.7, "data": 0.5}   # 业务本体没有采样，置信度按依据给定


def _onto_statement(t: str, x: dict[str, Any]) -> str:
    if t == "object":
        return f"「{x['name']}」是核心业务对象" + (f"：{x['description']}" if x.get("description") else "")
    if t == "link":
        return f"「{x.get('from', '')}」与「{x.get('to', '')}」：{x['name']}" + (f"（{x['cardinality']}）" if x.get("cardinality") else "")
    return f"对「{x.get('on', '')}」的业务动作：{x['name']}" + (f"——{x['description']}" if x.get("description") else "")


def _ontology_assertions(s, tenant: str, wsid: str, call_id: str, rep: dict[str, Any],
                         produced_by: dict[str, Any]) -> list[ProposedAssertion]:
    """v0.8：业务本体的每个对象 / 关系 / 动作各存一个产物，作为 proposed 断言返回，可像流程、系统、问题一样裁决。"""
    out = []
    for key, t in _ONTO_TYPE.items():
        for i, x in enumerate(rep["ontology"][key]):
            obj = {"type": t, **{k: v for k, v in x.items() if k not in ("evidence", "basis")}, "basis": x.get("basis", "both")}
            body = {"layer": "semantic", "kind": "ontology", "subject": f"ontology:{t}:{x['name']}", "predicate": "defines",
                    "object": obj, "statement": _onto_statement(t, x), "origin": "inferred", "review_status": "proposed",
                    "confidence": _BASIS_CONF.get(x.get("basis"), 0.7), "evidence": x["evidence"], "pattern_refs": [],
                    "explain": ""}
            aid = add_artifact(s, tenant, wsid, call_id, f"on_{t}_{i}", "ontology", body,
                               [{"evidence_id": e, "snapshot_id": None} for e in x["evidence"]], produced_by)
            x["artifact_id"] = aid
            out.append(ProposedAssertion(artifact_id=aid, version=1, **body))
    return out


def report(run: Run, req: ReportRequest) -> dict[str, Any]:
    s = run.session
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ensure_workspace(s, tenant, wsid)
    ctx = req.context
    evidence = [e.model_dump() for e in ctx.evidence]
    known = {e["id"] for e in evidence}
    tables = {str((e.get("observation") or {}).get("locator", {}).get("asset") or "").lower()
              for e in evidence if e["kind"] == "observation"} - {""}

    def refs(rows):
        return prompts.j([{"id": a.id, "kind": a.kind, "statement": a.statement,
                           **({"reason": a.reason_code} if a.reason_code else {})} for a in rows])

    raw = run.llm_json("understand.report", "understand/report_v2",
                       {"evidence": evidence, "match_digest": req.params.match_digest},
                       {"evidence": prompts.render_evidence(evidence), "confirmed": refs(ctx.confirmed),
                        "proposed": refs(ctx.proposed), "rejected": refs(ctx.rejected),
                        "match_digest": prompts.j(req.params.match_digest)}, schemas.REPORT)
    rep, dropped = _clean_report(raw, known, tables)
    cited = sorted({i for k in _REPORT_LISTS for r in rep[k] for i in r["evidence"]}
                   | {i for k in _ONTO_LISTS for r in rep["ontology"][k] for i in r["evidence"]})
    try:
        produced = {"capability": "report", **run.versions()}
        onto = _ontology_assertions(s, tenant, wsid, req.call_id, rep, produced)
        aid = add_artifact(s, tenant, wsid, req.call_id, "rep0", "business_report", rep,
                           [{"evidence_id": i, "snapshot_id": None} for i in cited], produced)
        resp = Response(call_id=req.call_id, capability="report", versions=Versions(**run.versions()),
                        result=BusinessReportOut(artifact_id=aid, version=1, report=rep, ontology_items=onto,
                                                 dropped_uncited=dropped),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "report", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
