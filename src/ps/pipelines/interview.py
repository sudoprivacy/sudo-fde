"""interview：更新工作假设（B）→ 停止判断 → 生成追问（C）。规格 §5.2–§5.4。

不变量：
- unknown / contradiction 的 ID 与状态由服务持有（hypothesis_item），模型只能新增、改描述、声明合并；
  已回答 / 已跳过的条目不会因重写复活。
- 产物、轮次、假设新版本、幂等记录在同一事务提交；版本 CAS 失败整体回滚。
"""

from __future__ import annotations

from typing import Any

from ps.core import prompts, schemas
from ps.core.errors import ServiceError
from ps.core.store import (add_artifact, cas_bump, check_version, ensure_workspace, idem_finish, items,
                           latest_hypothesis, new_id, rounds_done)
from ps.core.text import similarity
from ps.db import HypothesisItem, InterviewTurn, WorkspaceState, now
from ps.models import InterviewQuestion, InterviewRequest, InterviewResult, Response, StopDecision, Versions
from ps.pipelines.runtime import Run

_REUSE_SIMILARITY = 0.6     # 模型未给 id 时，按描述相似度认领已有条目
_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}


def _filter_ids(ids: list[str] | None, known: set[str]) -> list[str]:
    return [i for i in (ids or []) if i in known]


def _clean_hypothesis(h: dict[str, Any], known: set[str]) -> dict[str, Any]:
    """证据规则落到数据上：known 必须有证据；unknown 必须有 affects；矛盾必须指向两条证据。"""
    prof = h.get("profile", {})
    for c in prof.get("business_type_candidates", []):
        c["evidence"] = _filter_ids(c.get("evidence"), known)
    for key in ("customers", "offerings", "revenue_model"):
        if isinstance(prof.get(key), dict):
            prof[key]["evidence"] = _filter_ids(prof[key].get("evidence"), known)
    for st in h.get("value_chain_draft", []):
        st["evidence"] = _filter_ids(st.get("evidence"), known)
    h["known"] = [k for k in h.get("known", []) if _filter_ids(k.get("evidence"), known)]
    for k in h["known"]:
        k["evidence"] = _filter_ids(k["evidence"], known)
    h["unknowns"] = [u for u in h.get("unknowns", []) if u.get("affects")]
    contr = []
    for c in h.get("contradictions", []):
        c["between"] = _filter_ids(c.get("between"), known)
        if len(c["between"]) >= 2:
            contr.append(c)
    h["contradictions"] = contr
    h["pains"] = [p for p in h.get("pains", []) if _filter_ids(p.get("evidence"), known)]
    return h


def _merge_items(existing: list[HypothesisItem], h: dict[str, Any], tenant: str, ws: str) -> tuple[
        list[HypothesisItem], list[HypothesisItem]]:
    """返回（全部条目的新视图, 需新增的条目）。只改内存对象，事务内统一写入。"""
    by_id = {i.id: i for i in existing}
    touched: set[str] = set()
    new_items: list[HypothesisItem] = []

    def claim(kind: str, given_id: str | None, topic: str) -> HypothesisItem | None:
        if given_id and given_id in by_id and by_id[given_id].kind == kind:
            return by_id[given_id]
        best = max((i for i in existing if i.kind == kind), key=lambda i: similarity(i.topic, topic), default=None)
        if best is not None and similarity(best.topic, topic) >= _REUSE_SIMILARITY:
            return best
        return None

    def upsert(kind: str, given_id: str | None, topic: str, impact: str, affects: list[str]) -> None:
        item = claim(kind, given_id, topic)
        if item is None:
            item = HypothesisItem(id=new_id("hi"), workspace_id=ws, tenant_id=tenant, kind=kind, topic=topic,
                                  impact=impact, affects=affects, status="open")
            new_items.append(item)
            by_id[item.id] = item
        elif item.status == "open":
            item.topic, item.impact, item.affects = topic, impact, affects
        touched.add(item.id)   # 已关闭的条目被认领：不复活，也不重复新建

    for u in h.get("unknowns", []):
        upsert("unknown", u.get("id"), u["topic"], u.get("impact", "medium"), u.get("affects", []))
        for old in u.get("merges") or []:
            if old in by_id and by_id[old].status == "open" and old != u.get("id"):
                by_id[old].status, by_id[old].merged_into = "resolved", u.get("id")
                touched.add(old)
    for c in h.get("contradictions", []):
        upsert("contradiction", c.get("id"), c["note"], "high", ["澄清矛盾"])

    # 模型不再列出的开放条目：视为已被新证据解决（数据对撞产生的条目只由人的回答 / 跳过关闭）
    for item in existing:
        if item.status == "open" and item.id not in touched and not item.kind.startswith("data_"):
            item.status = "resolved"
    return list(by_id.values()), new_items


def _ordered_open(all_items: list[HypothesisItem]) -> list[HypothesisItem]:
    open_items = [i for i in all_items if i.status == "open"]
    return sorted(open_items, key=lambda i: (i.kind != "contradiction", not i.kind.startswith("data_"),
                                             _IMPACT_ORDER.get(i.impact, 1), -len(i.affects or [])))


def _item_view(i: HypothesisItem) -> dict[str, Any]:
    return {"id": i.id, "kind": i.kind, "topic": i.topic, "impact": i.impact, "affects": i.affects or []}


def _data_signals(s, tenant: str, wsid: str) -> list[tuple[str, str, str, list[str]]]:
    """读取最新一次 match 的结果（规格 v0.4 §3.9），转成 (kind, topic, impact, affects)。topic 即稳定键。"""
    from ps.core.store import latest_of_kind
    mr = latest_of_kind(s, tenant, wsid, "match_result")
    if not mr:
        return []
    out = []
    for u in (mr.get("unclaimed") or [])[:3]:
        rows = f"约 {u['row_count']} 行" if u.get("row_count") is not None else "行数未知"
        out.append(("data_unclaimed", f"数据里有一张没对应到任何业务的表：{u['name']}（{rows}{'，有状态字段' if u.get('has_status') else ''}）",
                    "high", [f"数据:{u['name']}"]))
    for n in mr.get("node_summary") or []:
        if n.get("gate") == "fail":
            miss = "、".join(x.get("need") or x.get("semantic") or "" for x in n.get("items") or []
                            if x.get("conclusion") == "absent_in_scanned_scope")
            out.append(("data_absent", f"「{n['statement']}」在已接入的数据里找不到：{miss or '关键数据'}", "high",
                        [f"节点:{n['statement'][:30]}"]))
    for r in mr.get("incomplete_reasons") or []:
        if r.startswith("not_scanned:"):
            out.append(("data_not_scanned", f"还没接入的系统：{r.split(':', 1)[1]}", "medium", [f"系统:{r.split(':', 1)[1]}"]))
    return out


def _validate_questions(raw: list[dict[str, Any]], open_items: list[HypothesisItem], round_no: int,
                        dims: list[str], max_q: int) -> list[dict[str, Any]]:
    open_ids = {i.id for i in open_items}
    seen: set[str] = set()
    out = []
    for q in raw:
        res = q.get("resolves")
        is_data = any(i.id == res and i.kind.startswith("data_") for i in open_items)
        if round_no == 1 and not is_data:      # 数据驱动的追问不受首轮开场维度限制
            if q.get("dimension") not in dims:
                continue
            if res and res not in open_ids:
                q["resolves"] = None
        elif res not in open_ids:
            continue
        item = next((i for i in open_items if i.id == q.get("resolves")), None)
        if item is not None and item.kind == "data_unclaimed":
            table = (item.affects or ["数据:"])[0].split(":", 1)[1]
            if table and table.lower() in (q.get("text") or "").lower():
                q["why"] = f"{q.get('why') or ''}（对应表 {table}）".strip()
                q["text"] = "系统里还记着一类没对上任何业务的记录，它对应你们的哪项业务？"
        key = q.get("resolves") or q.get("dimension") or q.get("text")
        if key in seen or not (q.get("text") or "").strip():
            continue
        seen.add(key)
        out.append(q)
    # 矛盾先问（原则 6）：有开放矛盾而模型没问，补一个模板问题
    contradictions = [i for i in open_items if i.kind == "contradiction"]
    if contradictions:
        c = contradictions[0]
        first = next((q for q in out if q.get("resolves") == c.id), None)
        if first is None:
            first = {"text": f"想确认一下：{c.topic}。实际情况是怎样的？", "why": "证据之间有矛盾，先澄清",
                     "resolves": c.id, "options": [], "allow_other": True, "kind": "ask",
                     "dimension": dims[0] if round_no == 1 else None}
        out = [first] + [q for q in out if q is not first]
    return out[:max_q]


def run(run: Run, req: InterviewRequest) -> dict[str, Any]:
    s, st = run.session, run.settings
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ws = ensure_workspace(s, tenant, wsid)
    check_version(ws, req.expected_state_version)

    evidence = [e.model_dump() for e in req.context.evidence]
    known = {e["id"] for e in evidence}
    prev = latest_hypothesis(s, tenant, wsid) or {}
    seen_before = set(prev.get("_meta", {}).get("seen_evidence", []))
    existing = items(s, tenant, wsid)
    turns = rounds_done(s, tenant, wsid)
    round_no = len(turns) + 1
    # 数据对撞的信号：没出现过的话题建为新条目（状态由服务持有，已关闭的不复活）
    known_topics = {i.topic for i in existing}
    data_new: list[HypothesisItem] = []
    for kind, topic, impact, affects in _data_signals(s, tenant, wsid):
        if topic not in known_topics:
            it = HypothesisItem(id=new_id("hi"), workspace_id=wsid, tenant_id=tenant, kind=kind, topic=topic,
                                impact=impact, affects=affects, status="open")
            data_new.append(it)
            existing.append(it)
            known_topics.add(topic)

    # ① 用户对上一轮追问的回答 / 跳过 → 条目权威状态
    from ps.db import Artifact, ArtifactVersion  # 局部导入，避免循环
    by_id = {i.id: i for i in existing}
    answers_log = []
    for a in req.params.answers:
        art = s.get(Artifact, a.question_id)
        if art is None or art.tenant_id != tenant or art.workspace_id != wsid:
            raise ServiceError("FORBIDDEN", "回答引用了无权访问的追问", question_id=a.question_id)
        body = s.get(ArtifactVersion, (art.id, art.current_version)).body
        bad = [i for i in a.evidence_ids if i not in known]
        if bad:
            raise ServiceError("SCHEMA_VIOLATION", "回答引用了请求中不存在的证据", evidence_ids=bad)
        target = by_id.get(body.get("resolves") or "")
        if target is not None and target.status == "open":
            target.status = "skipped" if a.skipped else "answered"
        answers_log.append(a.model_dump())

    # ② B：重写工作假设
    closed = [i for i in existing if i.status != "open"]
    open_before = [i for i in existing if i.status == "open" and not i.kind.startswith("data_")]
    company_basic = [e for e in evidence if (e.get("ref") or "").startswith("form.")]
    data = {"evidence": evidence, "open_items": [_item_view(i) for i in open_before],
            "closed_items": [_item_view(i) for i in closed if not i.kind.startswith("data_")]}
    h = run.llm_json(
        "interview.hypothesis", "interview/hypothesis_v2", data,
        render={"company_basic": prompts.render_evidence(company_basic),
                "previous_hypothesis": prompts.j({k: v for k, v in prev.items() if k != "_meta"}) if prev else "（首轮，无）",
                "evidence": prompts.render_evidence(evidence),
                "new_evidence_ids": prompts.j(sorted(known - seen_before)),
                "open_items": prompts.j(data["open_items"]), "closed_items": prompts.j(data["closed_items"]),
                "confirmed": prompts.j([c.model_dump() for c in req.context.confirmed]),
                "rejected": prompts.j([r.model_dump() for r in req.context.rejected])},
        schema=schemas.HYPOTHESIS)
    h = _clean_hypothesis(h, known)
    all_items, new_items = _merge_items(existing, h, tenant, wsid)
    new_items = data_new + new_items
    open_items = _ordered_open(all_items)

    # ③ 停止判断（服务端权威）
    max_rounds = st.t("interview.max_rounds")
    high_open = [i for i in open_items if i.impact == "high" or i.kind == "contradiction"]
    data_open = [i for i in open_items if i.kind.startswith("data_")]
    if data_open and round_no <= max_rounds + st.t("interview.extra_rounds_for_data"):
        stop = StopDecision(should_stop=False, reason=f"数据对撞产生 {len(data_open)} 个待问事项")
    elif round_no > max_rounds:
        stop = StopDecision(should_stop=True, reason=f"已问满 {max_rounds} 轮")
    elif not high_open and round_no > 1:
        stop = StopDecision(should_stop=True, reason="高影响的未知都已回答或跳过")
    else:
        stop = StopDecision(should_stop=False, reason=f"仍有 {len(high_open)} 个高影响未知")

    # ④ C：生成追问
    questions: list[dict[str, Any]] = []
    if not stop.should_stop:
        max_q = req.params.max_questions or st.t("interview.max_questions")
        dims = st.t("interview.opening_dimensions")
        asked = [{"text": t.stop.get("_texts", [])} for t in turns]
        qdata = {"open_items_list": [_item_view(i) for i in open_items], "round": round_no, "max_questions": max_q}
        raw = run.llm_json(
            "interview.questions", "interview/questions_v3", qdata,
            render={"hypothesis": prompts.j(h), "open_items": prompts.j(qdata["open_items_list"]),
                    "asked_before": prompts.j(asked), "round": round_no, "max_questions": max_q,
                    "opening_dimensions": "、".join(dims)},
            schema=schemas.QUESTIONS)
        questions = _validate_questions(raw.get("questions", []), open_items, round_no, dims, max_q)
        if not questions:
            stop = StopDecision(should_stop=True, reason="没有值得再问的问题")

    # ⑤ 同一事务提交
    try:
        new_version = cas_bump(s, tenant, wsid, req.expected_state_version)
        for it in new_items:
            s.add(it)
        h["_meta"] = {"seen_evidence": sorted(known | seen_before), "round": round_no}
        s.add(WorkspaceState(workspace_id=wsid, version=new_version, tenant_id=tenant, hypothesis=h))
        out_questions = []
        for idx, q in enumerate(questions):
            body = {k: q.get(k) for k in ("text", "why", "resolves", "options", "allow_other", "kind", "dimension")}
            aid = add_artifact(s, tenant, wsid, req.call_id, f"iq{idx}", "question", body, [],
                               {"capability": "interview", **run.versions()})
            out_questions.append(InterviewQuestion(id=aid, **{**body, "options": body["options"] or [],
                                                              "allow_other": bool(body["allow_other"]),
                                                              "kind": body["kind"] or "ask"}))
        s.add(InterviewTurn(workspace_id=wsid, tenant_id=tenant, round=round_no, call_id=req.call_id,
                            question_artifacts=[q.id for q in out_questions], answers=answers_log,
                            stop={**stop.model_dump(), "_texts": [q.text for q in out_questions]}))
        resp = Response(call_id=req.call_id, capability="interview", versions=Versions(**run.versions()),
                        state_version=new_version,
                        result=InterviewResult(round=round_no, questions=out_questions, stop=stop,
                                               hypothesis_version=new_version),
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "interview", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
