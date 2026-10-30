"""recommend（零模式）：n 次独立采样（D）→ 校验 → 聚类投票 → 自检（E）→ 排序截断。规格 §5.5 §5.7。"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ps.core import prompts, schemas
from ps.core.store import add_artifact, check_version, ensure_workspace, idem_finish, latest_hypothesis
from ps.core.text import containment, similarity
from ps.models import AssertionBatch, Dropped, ProposedAssertion, RecommendRequest, Response, Versions
from ps.pipelines.runtime import Run

_KIND = {"processes": "process", "systems": "system", "questions": "question"}
_PREDICATE = {"process": "has_process", "system": "uses_system", "question": "wants_to_know"}


def _statement(kind: str, item: dict[str, Any]) -> str:
    if kind == "question":
        return item.get("text", "")
    return item.get("statement") or item.get("name") or item.get("category") or ""


@dataclass
class Cluster:
    kind: str
    members: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    samples: set[int] = field(default_factory=set)

    def representative(self) -> dict[str, Any]:
        return max((m for _, m in self.members), key=lambda m: len(m.get("evidence") or []))

    def mean_conf(self) -> float:
        return sum(float(m["confidence"]) for _, m in self.members) / len(self.members)


def _name(kind: str, item: dict[str, Any]) -> str:
    if kind == "process":
        return item.get("name", "")
    if kind == "system":
        return item.get("category", "")
    return item.get("text", "")


def _cluster(run: Run, candidates: list[tuple[int, str, dict[str, Any]]], n: int) -> list[Cluster]:
    """把各次采样里指同一件事的条目归成簇（规格 §5.7）。

    阶段一没有 embedder：默认让模型判断"是不是同一件事"，失败或 n=1 时退回名字的字面包含度。
    无论哪种方法，每个簇每次采样最多计一票。
    """
    groups: list[list[int]] | None = None
    if n > 1 and run.settings.t("recommend.cluster_method") == "llm" and candidates:
        items = [{"id": f"i{idx}", "kind": kind, "name": _name(kind, it), "statement": _statement(kind, it)}
                 for idx, (_, kind, it) in enumerate(candidates)]
        try:
            out = run.llm_json("recommend.merge", "recommend/merge_v1", {"items": items},
                               {"items": prompts.j(items)}, schemas.MERGE, alias="critic")
            groups, used = [], set()
            for g in out.get("groups", []):
                ids = [int(m[1:]) for m in g.get("members", [])
                       if isinstance(m, str) and m[1:].isdigit() and int(m[1:]) < len(candidates)]
                ids = [i for i in ids if i not in used]
                # 只允许同类别同组：按 kind 拆开
                by_kind: dict[str, list[int]] = {}
                for i in ids:
                    by_kind.setdefault(candidates[i][1], []).append(i)
                    used.add(i)
                groups.extend(by_kind.values())
            groups.extend([i] for i in range(len(candidates)) if i not in used)
        except Exception as e:  # noqa: BLE001 - 合并失败不致命，退回字面方法
            run.warnings.append(f"模型合并失败，改用字面相似度：{getattr(e, 'code', e.__class__.__name__)}")
            groups = None
    if groups is None:
        thr = run.settings.t("recommend.cluster_similarity")
        groups = []
        for idx, (_, kind, it) in enumerate(candidates):
            g = next((g for g in groups if candidates[g[0]][1] == kind and
                      containment(_name(kind, candidates[g[0]][2]), _name(kind, it)) >= thr), None)
            if g is None:
                groups.append([idx])
            else:
                g.append(idx)
    clusters = []
    for g in groups:
        c = Cluster(kind=candidates[g[0]][1])
        for i in g:
            sample, _, it = candidates[i]
            c.members.append((sample, it))
            c.samples.add(sample)
        clusters.append(c)
    return clusters


def run(run: Run, req: RecommendRequest) -> dict[str, Any]:
    s, st = run.session, run.settings
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ws = ensure_workspace(s, tenant, wsid)
    check_version(ws, req.expected_state_version)

    evidence = [e.model_dump() for e in req.context.evidence]
    known = {e["id"] for e in evidence}
    hyp = {k: v for k, v in (latest_hypothesis(s, tenant, wsid) or {}).items() if k != "_meta"}
    rejected = [r.model_dump() for r in req.context.rejected]
    want = req.params.want
    n = req.params.samples or st.t("recommend.samples")
    dropped = Dropped()
    if n == 1:
        run.warnings.append("未做一致性检查（samples=1）")

    render = {"hypothesis": prompts.j(hyp) if hyp else "（尚无工作假设）",
              "evidence": prompts.render_evidence(evidence),
              "confirmed": prompts.j([c.model_dump() for c in req.context.confirmed]),
              "rejected": prompts.j(rejected), "want": prompts.j(want),
              "max_processes": st.t("recommend.max_processes"), "max_systems": st.t("recommend.max_systems"),
              "max_questions": st.t("recommend.max_questions")}

    # ① D：n 次独立采样，逐条校验
    rej_thr = st.t("recommend.rejected_similarity")
    candidates: list[tuple[int, str, dict[str, Any]]] = []     # (采样序号, kind, 条目)
    def one_sample(sample: int) -> dict[str, Any]:
        return run.llm_json("recommend.zero_pattern", "recommend/zero_pattern_v2",
                            {"evidence": evidence, "want": want, "sample": sample}, render, schemas.RECOMMEND,
                            temperature=st.t("recommend.sample_temperature") if n > 1 else None)

    # n 次采样彼此独立，并行调用以缩短等待（真模型下串行要几十秒）
    with ThreadPoolExecutor(max_workers=n) as pool:
        outs = list(pool.map(one_sample, range(n)))
    for sample, out in enumerate(outs):
        for group, kind in _KIND.items():
            if kind not in want:
                continue
            for item in out.get(group) or []:
                try:
                    conf = float(item.get("confidence"))
                except (TypeError, ValueError):
                    dropped.schema_ += 1
                    continue
                if not 0 <= conf <= 1:
                    dropped.schema_ += 1
                    continue
                cited = [i for i in (item.get("evidence") or []) if i in known]
                if not cited or len(cited) != len(item.get("evidence") or []):
                    # 引用了不存在的证据 → 整条丢弃（§3.4 硬规则），不做"修剪后保留"
                    dropped.no_citation += 1
                    continue
                if any(similarity(_statement(kind, item), r["statement"]) >= rej_thr for r in rejected):
                    dropped.rejected_repeat += 1
                    continue
                item["confidence"] = conf
                candidates.append((sample, kind, item))

    clusters = _cluster(run, candidates, n)

    # ② 多数票
    need = math.ceil(n / 2)
    kept = []
    for c in clusters:
        if len(c.samples) >= need:
            kept.append(c)
        else:
            dropped.minority += 1

    # ③ E：自检
    reps = []
    for idx, c in enumerate(kept):
        r = dict(c.representative())
        r["temp_id"] = f"c{idx}"
        r["_kind"] = c.kind
        reps.append(r)
    verdicts: dict[str, dict[str, Any]] = {}
    if reps:
        crit = run.llm_json("recommend.critic", "recommend/critic_v1",
                            {"evidence": evidence, "recommendations": reps},
                            {"evidence": prompts.render_evidence(evidence),
                             "confirmed": prompts.j([c.model_dump() for c in req.context.confirmed]),
                             "recommendations": prompts.j([{k: v for k, v in r.items() if k != "_kind"} for r in reps])},
                            schemas.CRITIC, alias="critic")
        verdicts = {v["temp_id"]: v for v in crit.get("verdicts", [])}

    scored = []
    for c, r in zip(kept, reps):
        v = verdicts.get(r["temp_id"])
        if v is None:
            run.warnings.append(f"自检未返回 {r['temp_id']} 的判定，按未通过处理")
            dropped.generic += 1
            continue
        if v.get("generic") or (c.kind == "question" and not v.get("answerable", True)):
            dropped.generic += 1
            continue
        if not v.get("citation_ok", False):
            dropped.no_citation += 1
            continue
        if v.get("contradiction"):
            dropped.contradiction += 1
            continue
        freq = len(c.samples) / n
        p_raw = c.mean_conf()
        conf = round(0.5 * freq + 0.5 * p_raw, 4)
        if conf < req.limits.min_confidence:
            continue
        scored.append((conf, freq, p_raw, c, r, v))

    scored.sort(key=lambda x: -x[0])
    per_kind_max = {"process": st.t("recommend.max_processes"), "system": st.t("recommend.max_systems"),
                    "question": st.t("recommend.max_questions")}
    counts: dict[str, int] = {}
    final = []
    for row in scored:
        kind = row[3].kind
        if counts.get(kind, 0) >= per_kind_max[kind] or len(final) >= req.limits.max_items:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        final.append(row)

    # ④ 落产物并返回（同一事务）
    if not final:
        run.warnings.append("本轮没有通过证据与具体性审核的业务卡片，请补充业务角色、操作步骤或数据口径后重新生成。")
    try:
        items_out = []
        for conf, freq, p_raw, c, r, v in final:
            body_obj = {k: val for k, val in r.items() if k not in ("temp_id", "_kind", "evidence", "confidence")}
            if v.get("jargon"):
                body_obj["_critic_note"] = "可能含技术词"
            statement = _statement(c.kind, r)
            body = {"layer": "business", "kind": c.kind, "subject": "company", "predicate": _PREDICATE[c.kind],
                    "object": body_obj, "statement": statement, "origin": "inferred", "confidence": conf,
                    "evidence": r["evidence"], "explain": r.get("why_valuable", "")}
            aid = add_artifact(s, tenant, wsid, req.call_id, r["temp_id"], "assertion", body,
                               [{"evidence_id": e, "snapshot_id": None} for e in r["evidence"]],
                               {"capability": "recommend", **run.versions(), "p_raw": round(p_raw, 4),
                                "frequency": freq, "sample_votes": sorted(c.samples), "samples": n,
                                "p_cal": None})
            items_out.append(ProposedAssertion(artifact_id=aid, version=1, **body))
        resp = Response(call_id=req.call_id, capability="recommend", versions=Versions(**run.versions()),
                        state_version=ws.state_version, result=AssertionBatch(items=items_out), dropped=dropped,
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "recommend", req.call_id, resp)
        s.add(run.log("ok", resp["dropped"]))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp
