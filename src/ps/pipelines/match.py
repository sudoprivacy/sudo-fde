"""match：目录预处理 → 回答计划与指纹 → 语义标注 → 四层匹配 → 结论与断言 → 未解释资产。

依据：阶段二详细设计 §5、规格 v0.4 §3.9 §6。
不变量：
- binding 断言的证据只能是目录里的观察（表 / 列 / 外键 / 数据源 / 扫描范围），不能是业务原话。
- 未扫描、权限不足、统计不可用时不下否定结论（absent_in_scanned_scope 只在范围完整时出现）。
- sensitive_column 不作为任何结论的证据。
- 人工确认过的绑定（confirmed 里的 binding）优先于模型与词典。
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ps.core import catalog as cat
from ps.core import prompts, schemas
from ps.core.errors import ServiceError
from ps.core.store import add_artifact, ensure_workspace, find_cached, idem_finish
from ps.core.text import containment, similarity
from ps.models import (ArtifactRef, AssertionRef, MatchRequest, MatchResult, NodeSummary, PlanOut, ProposedAssertion,
                       Response, Versions)
from ps.pipelines.runtime import Run

NODE_KINDS = ("process", "system", "question")


# ---------- ① 输入检查 ----------

def _check_catalog(req: MatchRequest, st) -> tuple[dict[str, Any], set[str], dict[str, Any]]:
    c = req.context.catalog
    if c is None or not c.assets:
        raise ServiceError("CATALOG_INVALID", "match 需要 context.catalog，且至少包含一张表")
    if len(c.assets) > st.t("match.max_assets"):
        raise ServiceError("CONTEXT_TOO_LARGE", "目录表数超过上限，请按 schema 拆分上传",
                           assets=len(c.assets), limit=st.t("match.max_assets"))
    ids: list[str] = [s.evidence_id for s in c.sources]
    for a in c.assets:
        ids.append(a.evidence_id)
        ids += [col.evidence_id for col in a.columns]
        ids += [fk.evidence_id for fk in a.foreign_keys]
    scope = req.context.scan_scope
    ids += [x.evidence_id for x in (scope.systems if scope else [])]
    if len(ids) != len(set(ids)):
        raise ServiceError("CATALOG_INVALID", "目录中的 evidence_id 重复")
    src_ids = {s.id for s in c.sources}
    bad = [a.id for a in c.assets if a.source not in src_ids]
    if bad:
        raise ServiceError("CATALOG_INVALID", "资产引用了不存在的数据源", assets=bad[:10])
    data = c.model_dump(by_alias=True)
    return data, set(ids), (scope.model_dump() if scope else {"systems": [], "unmapped_recommended_systems": []})


def _nodes(req: MatchRequest, max_nodes: int, warnings: list[str]) -> list[AssertionRef]:
    rejected = {r.id for r in req.context.rejected}
    pool = [a for a in req.context.confirmed if a.kind in NODE_KINDS] + \
           [a for a in req.context.proposed if a.kind in NODE_KINDS]
    seen, nodes = set(), []
    for a in pool:
        if a.id in rejected or a.id in seen:
            continue
        seen.add(a.id)
        nodes.append(a)
    if req.params.targets:
        want = {t.artifact_id for t in req.params.targets}
        nodes = [n for n in nodes if n.id in want]
    if len(nodes) > max_nodes:
        warnings.append(f"节点数 {len(nodes)} 超过上限 {max_nodes}，按已确认优先截断")
        nodes = sorted(nodes, key=lambda n: n.status != "confirmed")[:max_nodes]
    return nodes


def _human_bindings(req: MatchRequest) -> dict[str, str]:
    """confirmed 里 FDE 改过 / 确认过的概念绑定：{实体标签: asset_id}。"""
    out = {}
    for a in req.context.confirmed:
        o = a.object or {}
        if a.kind == "binding" and o.get("predicate", "stored_in") == "stored_in" and o.get("semantic") and o.get("asset_id"):
            out[o["semantic"]] = o["asset_id"]
    return out


# ---------- ② 回答计划与指纹 ----------

def _plans(run: Run, nodes: list[AssertionRef]) -> dict[str, dict[str, Any]]:
    """每个流程 / 问题节点：{"plan": 回答计划 | None, "fingerprint": 指纹, "plan_id", "fp_id"}，按 (id, version) 缓存。"""
    s, tenant, ws = run.session, run.ident.tenant_id, run.workspace_id
    v = cat.vocab()
    out: dict[str, dict[str, Any]] = {}
    todo = []
    for n in nodes:
        if n.kind not in ("process", "question"):
            continue
        key = f"fp:{n.id}@{n.version}:v{v.version}"
        hit = find_cached(s, tenant, ws, "fingerprint", key)
        if hit:
            plan_hit = find_cached(s, tenant, ws, "answer_plan", f"plan:{n.id}@{n.version}:v{v.version}")
            out[n.id] = {"fingerprint": hit[1], "fp_id": hit[0], "plan": plan_hit[1] if plan_hit else None,
                         "plan_id": plan_hit[0] if plan_hit else None, "cached": True}
        else:
            todo.append(n)

    def build(n: AssertionRef) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
        obj = n.object or {}
        target = {"kind": n.kind, "statement": n.statement, **{k: obj.get(k) for k in
                  ("name", "steps", "roles", "data_needs", "asked_by", "why_valuable") if obj.get(k)}}
        plan = None
        if n.kind == "question":
            plan = run.llm_json("generate.answer_plan", "generate/answer_plan_v1",
                                {"question": n.statement, "data_needs": obj.get("data_needs") or []},
                                {"question": n.statement, "data_needs": prompts.j(obj.get("data_needs") or []),
                                 "vocabulary": v.prompt_list()}, schemas.ANSWER_PLAN)
        fp = run.llm_json("generate.fingerprint", "generate/fingerprint_v1", {"target": target, "plan": plan},
                          {"target": prompts.j(target), "answer_plan": prompts.j(plan) if plan else "（无，流程）",
                           "vocabulary": v.prompt_list()}, schemas.FINGERPRINT)
        return n.id, plan, fp

    if todo:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            results = list(pool.map(build, todo))
        by_id = {n.id: n for n in todo}
        for nid, plan, fp in results:
            n = by_id[nid]
            reqs = []
            for i, it in enumerate(fp.get("requires") or []):
                it = dict(it)
                it["id"] = it.get("id") or f"f{i + 1}"
                it["weight"] = int(it.get("weight") or 1)
                reqs.append(it)
            fp = {"requires": reqs, "for": {"artifact_id": nid, "version": n.version}}
            entry = {"fingerprint": fp, "plan": plan, "cached": False}
            entry["fp_id"] = add_artifact(s, tenant, ws, run.call_id, f"fp:{nid}@{n.version}:v{v.version}",
                                          "fingerprint", fp, [], {"capability": "match", **run.versions()})
            if plan is not None:
                plan = {**plan, "for": {"artifact_id": nid, "version": n.version}}
                entry["plan"] = plan
                entry["plan_id"] = add_artifact(s, tenant, ws, run.call_id, f"plan:{nid}@{n.version}:v{v.version}",
                                                "answer_plan", plan, [], {"capability": "match", **run.versions()})
            out[nid] = entry
    return out


# ---------- ③ 语义标注 ----------

def _labels(run: Run, catalog: dict[str, Any], prof: dict[str, Any], preferred: list[str]) -> str:
    """模型补齐词典定不了的表与列；结果按 (快照, 词表版本, 提示词版本) 缓存。返回产物 ID。"""
    s, tenant, ws, st = run.session, run.ident.tenant_id, run.workspace_id, run.settings
    v = cat.vocab()
    p = prompts.load("match/label_v1")
    key = f"labels:{catalog['snapshot_id']}:v{v.version}:{p.version}"
    hit = find_cached(s, tenant, ws, "catalog_labels", key)
    if hit:
        _apply_labels(prof, hit[1]["tables"])
        run.prompt_versions["match.label"] = p.version
        return hit[0]
    trust = st.t("match.lexicon_trust")
    assets = {a["id"]: a for a in catalog["assets"]}
    need = []
    for aid, pa in prof["assets"].items():
        unlabeled = sum(1 for c in pa["columns"].values() if not c["label"])
        if pa["confidence"] < trust or unlabeled > len(pa["columns"]) / 2:
            need.append(aid)
    batches = [need[i:i + st.t("match.label_batch")] for i in range(0, len(need), st.t("match.label_batch"))]

    def label_batch(ids: list[str]) -> list[dict[str, Any]]:
        tables = []
        for aid in ids:
            a, pa = assets[aid], prof["assets"][aid]
            tables.append({"asset_id": aid, "name": a["name"], "comment": a.get("comment"), "row_count": a.get("row_count"),
                           "primary_key": a["primary_key"],
                           "foreign_keys": [f"{','.join(f['columns'])}→{prof['assets'][f['ref']]['name']}" for f in pa["fks"]],
                           "columns": [{"name": c["name"], "type": c["type"], "shape": c.get("shape"),
                                        "looks_like": c.get("looks_like"), "comment": c.get("comment")}
                                       for c in a["columns"] if not c.get("sensitive_column")]})
        out = run.llm_json("match.label", "match/label_v1", {"tables": tables, "preferred": preferred},
                           {"preferred": "、".join(preferred) or "（无）", "vocabulary": v.prompt_list(),
                            "tables": prompts.j(tables)}, schemas.LABELS)
        return [t for t in out.get("tables", []) if t.get("asset_id") in set(ids)]

    labeled: list[dict[str, Any]] = []
    if batches:
        with ThreadPoolExecutor(max_workers=min(st.t("match.label_workers"), len(batches))) as pool:
            for part in pool.map(label_batch, batches):
                labeled += part
    _apply_labels(prof, labeled)
    return add_artifact(s, tenant, ws, run.call_id, key, "catalog_labels",
                        {"snapshot_id": catalog["snapshot_id"], "tables": labeled}, [],
                        {"capability": "match", **run.versions()})


def _apply_labels(prof: dict[str, Any], labeled: list[dict[str, Any]]) -> None:
    for t in labeled:
        pa = prof["assets"].get(t.get("asset_id"))
        if pa is None:
            continue
        conf = float(t.get("confidence") or 0)
        if t.get("label") and conf >= pa["confidence"]:
            if pa["label"] and pa["label"] != t["label"]:
                pa["alt"] = [pa["label"], *pa["alt"]][:3]
            pa["label"], pa["confidence"], pa["label_source"] = t["label"], conf, "llm"
            pa["alt"] = [x for x in (t.get("alt") or []) + pa["alt"] if x != t["label"]][:3]
        if t.get("role_hint") and pa.get("role") == "entity" and t["role_hint"] in ("event", "detail"):
            pa["role"] = t["role_hint"]
        for c in t.get("columns") or []:
            pc = pa["columns"].get(c.get("name"))
            if pc is None or pc["sensitive"]:
                continue
            cc = float(c.get("confidence") or 0)
            if c.get("label") and (not pc["label"] or cc > pc["confidence"]):
                pc["label"], pc["confidence"], pc["source"] = c["label"], cc, "llm"
    # 一致性：外键列的标签应为 <被引用实体>_ref
    for pa in prof["assets"].values():
        for fk in pa["fks"]:
            ref_label = prof["assets"][fk["ref"]]["label"]
            for col in fk["columns"]:
                pc = pa["columns"].get(col)
                if pc and ref_label and pc["label"] != f"{ref_label}_ref" and pc["label"] != "identifier":
                    pc["alt"] = [pc["label"]] if pc["label"] else []
                    pc["label"], pc["confidence"] = f"{ref_label}_ref", max(pc["confidence"], 0.7)


# ---------- ④ 四层匹配 ----------

class Matcher:
    def __init__(self, run: Run, catalog: dict[str, Any], prof: dict[str, Any], scope: dict[str, Any],
                 human: dict[str, str]):
        self.st = run.settings
        self.catalog, self.prof, self.human = catalog, prof, human
        self.sources = {s["id"]: s for s in catalog["sources"]}
        self.scope_by_src = {x["source_id"]: x for x in scope.get("systems", [])}
        self.scope_ev = [x["evidence_id"] for x in scope.get("systems", [])] or [s["evidence_id"] for s in catalog["sources"]]
        self.unmapped: list[str] = list(scope.get("unmapped_recommended_systems") or [])
        # FDE 标为"不需要接入"的系统：不再阻止否定结论（规格 v0.5）
        self.waived: set[str] = set(scope.get("waived_systems") or [])
        self.offline: list[str] = []
        self.t_matched = self.st.t("match.matched")
        self.t_unknown = self.st.t("match.unknown_floor")

    # 完整性：只有全部来源都扫全、且没有未接入 / 线下的系统，才允许否定结论
    def incomplete_reasons(self) -> list[str]:
        r = []
        for sid, src in self.sources.items():
            sc = self.scope_by_src.get(sid)
            if sc is None:                       # 没有扫描范围信息：保守处理，不下否定结论
                r.append(f"scope_unknown:{src['name']}")
                continue
            if sc["status"] in ("partial", "permission_denied", "not_scanned"):
                r.append(f"{sc['status']}:{src['name']}")
            elif sc.get("permission_errors"):
                r.append(f"permission_denied:{src['name']}")
            elif (sc.get("coverage") or {}).get("assets_total") is None:
                r.append(f"partial:{src['name']}")
        r += [f"not_scanned:{x}" for x in self.unmapped]
        r += [f"may_be_offline:{x}" for x in self.offline]
        return r

    def absent_or_unscanned(self) -> tuple[str, list[str]]:
        reasons = self.incomplete_reasons()
        return ("not_scanned", reasons) if reasons else ("absent_in_scanned_scope", [])

    # L1
    def map_systems(self, run: Run, systems: list[AssertionRef]) -> list[dict[str, Any]]:
        words = [w.lower() for w in self.st.t("match.offline_system_words")]
        res, unresolved = [], []
        for n in systems:
            if n.id in self.waived:
                res.append({"system_ref": n.id, "source_id": None, "score": 0.0, "status": "waived"})
                continue
            cat_name = f"{(n.object or {}).get('category', '')} {n.statement}"
            # 只看系统类别判断是不是线下方式（说明里提到 Excel 不等于这个系统就是 Excel）
            category = ((n.object or {}).get("category") or n.statement).lower()
            if any(w in category for w in words):
                self.offline.append((n.object or {}).get("category") or n.statement[:20])
                res.append({"system_ref": n.id, "source_id": None, "score": 0.0, "status": "offline"})
                continue
            best, score = None, 0.0
            for sid, src in self.sources.items():
                hint = f"{src.get('system_hint') or ''} {src['name']}"
                sc = max(containment(src.get("system_hint") or src["name"], (n.object or {}).get("category") or ""),
                         similarity(hint, cat_name))
                if sc > score:
                    best, score = sid, sc
            if best and score >= 0.5:
                res.append({"system_ref": n.id, "source_id": best, "score": round(score, 3), "status": "mapped"})
            else:
                unresolved.append(n)
        if unresolved and self.sources:
            srcs = [{"source_id": sid, "name": s["name"], "system_hint": s.get("system_hint"),
                     "top_labels": sorted({p["label"] for p in self.prof["assets"].values()
                                           if p["source"] == sid and p["label"]})[:15]}
                    for sid, s in self.sources.items()]
            sysl = [{"system_ref": n.id, "category": (n.object or {}).get("category"), "statement": n.statement}
                    for n in unresolved]
            out = run.llm_json("match.l1", "match/l1_v1", {"systems": sysl, "sources": srcs},
                               {"systems": prompts.j(sysl), "sources": prompts.j(srcs)}, schemas.L1, alias="critic")
            got = {m["system_ref"]: m for m in out.get("mappings", [])}
            for n in unresolved:
                m = got.get(n.id) or {}
                sid = m.get("source_id") if m.get("source_id") in self.sources else None
                if sid:
                    res.append({"system_ref": n.id, "source_id": sid, "score": float(m.get("confidence") or 0.5),
                                "status": "mapped"})
                else:
                    self.unmapped.append((n.object or {}).get("category") or n.statement[:20])
                    res.append({"system_ref": n.id, "source_id": None, "score": 0.0, "status": "unmapped"})
        elif unresolved:
            for n in unresolved:
                self.unmapped.append((n.object or {}).get("category") or n.statement[:20])
                res.append({"system_ref": n.id, "source_id": None, "score": 0.0, "status": "unmapped"})
        return res

    # L2
    def table_for(self, semantic: str | None) -> tuple[str | None, float, str]:
        if not semantic:
            return None, 0.0, ""
        sem = semantic.removeprefix("new:")
        if sem in self.human:
            aid = self.human[sem]
            if aid in self.prof["assets"]:
                return aid, 1.0, "confirmed_binding"
            return None, 0.0, "confirmed_binding_missing"
        # 先在底表里找；只有没有任何底表对得上时，才退到视图（视图是派生数据，真实库里常有大量同名视图）
        for allow_views in (False, True):
            best, score = None, 0.0
            for aid, p in self.prof["assets"].items():
                if (p["role"] == "view") != allow_views:
                    continue
                base = 1.0 if p["label"] == sem else (0.6 if sem in p["alt"] else 0.0)
                if not base:
                    continue
                sc = base * cat.ROLE_FACTOR.get(p["role"], 0.5) * (0.5 + 0.5 * p["confidence"])
                if sc > score or (sc == score and best and p["referenced_by"] > self.prof["assets"][best]["referenced_by"]):
                    best, score = aid, sc
            if best:
                return best, round(score, 3), "label" if not allow_views else "view"
        return None, 0.0, "label"

    def locate(self, semantic: str | None) -> tuple[str | None, float, str, str | None]:
        """实体在哪：先找表（table_for）；找不到再看它是否以字段形式落在别的表里（semantics.yaml realized_by_columns）。
        返回 (asset_id, score, how, column)。"""
        aid, sc, how = self.table_for(semantic)
        if aid or how == "confirmed_binding_missing" or not semantic:
            return aid, sc, how, None
        best = (None, 0.0, None)
        tables_first = sorted(self.prof["assets"], key=lambda a: self.prof["assets"][a]["role"] == "view")
        for lab in cat.vocab().as_columns.get(semantic.removeprefix("new:"), []):
            for a in tables_first:
                if best[0] and self.prof["assets"][a]["role"] == "view" and self.prof["assets"][best[0]]["role"] != "view":
                    break
                col, csc = self._col(a, lab)
                if col and csc > best[1]:
                    best = (a, csc, col)
        if best[0]:
            return best[0], round(min(best[1], 0.9) * 0.8, 3), "as_columns", best[2]
        return None, 0.0, how, None

    def _col(self, aid: str, semantic: str) -> tuple[str | None, float]:
        p = self.prof["assets"][aid]
        sem = semantic.removeprefix("new:")
        best, score = None, 0.0
        for name, c in p["columns"].items():
            if c["sensitive"]:
                continue
            base = 1.0 if c["label"] == sem else (0.6 if sem in (c.get("alt") or []) else 0.0)
            sc = base * max(c["confidence"], 0.5)
            if sc > score:
                best, score = name, sc
        return best, round(score, 3)

    def _ev_table(self, aid: str) -> str:
        return self.prof["assets"][aid]["evidence_id"]

    def _ev_col(self, aid: str, col: str) -> str:
        return self.prof["assets"][aid]["columns"][col]["evidence_id"]

    def _conclude(self, score: float | None, found: bool) -> tuple[str, list[str]]:
        if score is None:
            return "unknown", ["stats_unavailable"]
        if score >= self.t_matched:
            return "matched", []
        if found and score >= self.t_unknown:
            return "unknown", []
        return self.absent_or_unscanned()

    def score_item(self, it: dict[str, Any]) -> dict[str, Any]:
        kind = it.get("kind")
        out = {"id": it.get("id"), "kind": kind, "semantic": it.get("semantic"), "on": it.get("on"),
               "path": list(it.get("path") or []),
               "weight": int(it.get("weight") or 1), "must": bool(it.get("must")), "match": 0.0,
               "conclusion": "unknown", "asset": None, "column": None, "evidence": [], "reasons": []}
        if kind in ("entity_table", "event_table"):
            aid, sc, how, as_col = self.locate(it.get("semantic"))
            if as_col:
                out["column"] = as_col
                out["reasons"].append(f"as_columns:{self.prof['assets'][aid]['name']}.{as_col}")
            if how == "confirmed_binding_missing":
                out.update(conclusion="conflict", reasons=["confirmed_binding_missing"], evidence=list(self.scope_ev))
                return out
            if aid and kind == "event_table":
                role = self.prof["assets"][aid]["role"]
                if role not in ("event", "detail"):
                    sc *= 0.7
                for need in it.get("needs_columns") or []:
                    col, csc = self._col(aid, need)
                    if not col:
                        sc *= 0.5
                        out["reasons"].append(f"missing_column:{need}")
                    else:
                        out["evidence"].append(self._ev_col(aid, col))
                mh = it.get("min_history_days")
                if mh:
                    spans = [c["date_span_days"] for c in self.prof["assets"][aid]["columns"].values()
                             if c["shape"] in ("date", "datetime") and c["date_span_days"] is not None]
                    if not spans:
                        out.update(asset=aid, match=None, conclusion="unknown",
                                   reasons=out["reasons"] + ["history_unknown"], evidence=[self._ev_table(aid)] + out["evidence"])
                        return out
                    if max(spans) < mh:
                        sc = min(sc, self.t_unknown)
                        out["reasons"].append(f"history_short:{max(spans)}<{mh}")
            out["match"], out["asset"] = round(sc, 3), aid
            concl, reasons = self._conclude(sc, aid is not None)
            if any(r.startswith("history_short") for r in out["reasons"]) and concl == "matched":
                concl = "unknown"
            out["conclusion"], out["reasons"] = concl, out["reasons"] + reasons
            out["evidence"] = ([self._ev_col(aid, as_col)] if as_col else ([self._ev_table(aid)] if aid else [])) \
                + out["evidence"] or list(self.scope_ev)
            if concl in ("absent_in_scanned_scope", "not_scanned") and not aid:
                out["evidence"] = list(self.scope_ev)
            return out
        if kind in ("column_semantic", "status_column", "event_time"):
            sem = it.get("semantic") or ("status" if kind == "status_column" else "event_time")
            aids = []
            if it.get("on"):
                aid, _, how, _c = self.locate(it["on"])
                if how == "confirmed_binding_missing":
                    out.update(conclusion="conflict", reasons=["confirmed_binding_missing"], evidence=list(self.scope_ev))
                    return out
                if aid:
                    aids = [aid]
                else:
                    concl, reasons = self.absent_or_unscanned()
                    out.update(conclusion=concl, reasons=[f"no_table_for:{it['on']}"] + reasons, evidence=list(self.scope_ev))
                    return out
            else:
                aids = list(self.prof["assets"])
            best = (None, None, 0.0)
            for aid in aids:
                if kind == "event_time":
                    cols = [(n, 0.9 if c["shape"] in ("date", "datetime") else 0.6) for n, c in
                            self.prof["assets"][aid]["columns"].items()
                            if not c["sensitive"] and (c["shape"] in ("date", "datetime")
                                                       or (c["label"] or "").endswith(("_date", "_time")))]
                    col, sc = max(cols, key=lambda x: x[1]) if cols else (None, 0.0)
                else:
                    col, sc = self._col(aid, sem)
                if sc > best[2]:
                    best = (aid, col, sc)
            aid, col, sc = best
            concl, reasons = self._conclude(sc, col is not None)
            out.update(match=round(sc, 3), asset=aid, column=col, conclusion=concl, reasons=reasons,
                       evidence=[self._ev_col(aid, col)] if col else list(self.scope_ev))
            return out
        if kind == "fk_chain":
            path = it.get("path") or []
            tables = [self.locate(x)[0] for x in path]
            if len(path) < 2 or not all(tables):
                concl, reasons = self.absent_or_unscanned()
                out.update(conclusion=concl, reasons=["path_table_missing"] + reasons, evidence=list(self.scope_ev))
                return out
            score, ev, inferred = 1.0, [], False
            for a, b in zip(tables, tables[1:]):
                if a == b:              # 两个概念落在同一张表（以字段形式），链在表内成立
                    continue
                edge = next((fk for fk in self.prof["assets"][a]["fks"] if fk["ref"] == b), None) or \
                       next((fk for fk in self.prof["assets"][b]["fks"] if fk["ref"] == a), None)
                if edge is None:
                    score = 0.0
                    break
                score *= edge["score"]
                inferred |= not edge["declared"]
                ev.append(edge["evidence_id"])
            concl, reasons = self._conclude(score, score > 0)
            if inferred and concl == "matched":
                concl, reasons = "unknown", ["inferred_fk"]
            out.update(match=round(score, 3), conclusion=concl, reasons=reasons, evidence=ev or list(self.scope_ev))
            return out
        out["reasons"] = [f"unsupported_kind:{kind}"]
        out["evidence"] = list(self.scope_ev)
        return out

    def summarize(self, node: AssertionRef, items: list[dict[str, Any]], plan: dict[str, Any] | None) -> dict[str, Any]:
        num = den = 0.0
        undetermined_w = 0.0
        for x in items:
            if x["match"] is None:
                undetermined_w += x["weight"]
                continue
            num += x["weight"] * x["match"]
            den += x["weight"]
        coverage = round(num / den, 3) if den else None
        musts = [x for x in items if x["must"]] or items[:1]
        if any(x["conclusion"] == "conflict" for x in items):
            gate = "undetermined"
        elif all(x["conclusion"] == "matched" for x in musts):
            gate = "pass"
        elif any(x["conclusion"] == "absent_in_scanned_scope" for x in musts):
            gate = "fail"
        else:
            gate = "undetermined"
        reasons = sorted({r for x in items for r in x["reasons"]})
        answerability = None
        if node.kind == "question":
            if gate == "fail":
                answerability = "not_answerable"
            elif gate == "undetermined":
                answerability = "undetermined"
            elif all(x["conclusion"] == "matched" for x in items):
                answerability = "answerable"
            else:
                answerability = "partial"
        return {"target": {"artifact_id": node.id, "version": node.version}, "kind": node.kind,
                "statement": node.statement, "coverage": coverage, "gate": gate, "answerability": answerability,
                "undetermined_reasons": reasons if gate != "pass" else [],
                "missing": [x["id"] for x in items if x["conclusion"] != "matched"],
                "items": [{k: x[k] for k in ("id", "kind", "semantic", "on", "path", "must", "weight", "match", "conclusion",
                                             "asset", "column", "reasons", "evidence")}
                          | {"asset_name": self.prof["assets"][x["asset"]]["name"] if x["asset"] else None,
                             "need": _need_for(x, plan)} for x in items]}

    def unclaimed(self, claimed: set[str]) -> list[dict[str, Any]]:
        skip = [w.lower() for w in self.st.t("match.unclaimed_skip_words")]
        rows = []
        for aid, p in self.prof["assets"].items():
            if aid in claimed or p["role"] in ("dictionary", "log", "view"):
                continue
            if (p["row_count"] or 0) < self.st.t("match.unclaimed_min_rows") and p["row_count"] is not None:
                continue
            if any(w in p["name"].lower() for w in skip):
                continue
            has_status = any((c["label"] == "status") for c in p["columns"].values())
            rank = (p["referenced_by"] + 1) * math.log((p["row_count"] or 100) + 1) * (1.5 if has_status else 1.0)
            rows.append({"asset": aid, "name": p["name"], "row_count": p["row_count"], "label": p["label"],
                         "has_status": has_status, "rank_score": round(rank, 3), "evidence_id": p["evidence_id"]})
        rows.sort(key=lambda r: -r["rank_score"])
        return rows[: self.st.t("match.unclaimed_top")]


def _need_for(item: dict[str, Any], plan: dict[str, Any] | None) -> str:
    v = cat.vocab()
    if item.get("kind") == "fk_chain" and item.get("path"):
        return "与".join(v.display(p) for p in item["path"]) + "的关联"
    if plan:
        for inp in plan.get("inputs") or []:
            if inp.get("semantic") in (item.get("semantic"), item.get("on")):
                return inp.get("need") or ""
    return v.display(item.get("semantic") or item.get("on") or item.get("kind") or "")


# ---------- 主流程 ----------

def run(run: Run, req: MatchRequest) -> dict[str, Any]:
    s, st = run.session, run.settings
    tenant, wsid = run.ident.tenant_id, req.workspace_id
    ensure_workspace(s, tenant, wsid)
    catalog, known_ev, scope = _check_catalog(req, st)
    nodes = _nodes(req, st.t("match.max_nodes"), run.warnings)
    prof = cat.profile(catalog)

    plans = _plans(run, nodes)
    preferred = sorted({x for p in plans.values() for it in p["fingerprint"]["requires"]
                        for x in [it.get("semantic"), it.get("on"), *(it.get("path") or []), *(it.get("needs_columns") or [])]
                        if x})
    labels_id = _labels(run, catalog, prof, preferred)

    m = Matcher(run, catalog, prof, scope, _human_bindings(req))
    systems = m.map_systems(run, [n for n in nodes if n.kind == "system"])
    summaries, claimed = [], set()
    semantics_used: dict[str, tuple[str | None, float, str]] = {}
    for n in nodes:
        if n.kind == "system":
            continue
        fp = plans[n.id]["fingerprint"]
        items = [m.score_item(it) for it in fp["requires"]]
        for x in items:
            if x["asset"]:
                claimed.add(x["asset"])
            for sem in (x["semantic"], x["on"]):
                if sem and x["kind"] in ("entity_table", "event_table") and sem not in semantics_used:
                    semantics_used[sem] = m.table_for(sem)
        summaries.append(m.summarize(n, items, plans[n.id].get("plan")))
    unclaimed = m.unclaimed(claimed)

    try:
        items_out: list[ProposedAssertion] = []

        def emit(temp: str, kind: str, subject: str, predicate: str, obj: dict[str, Any], statement: str,
                 conf: float, evidence: list[str]) -> None:
            ev = [e for e in dict.fromkeys(evidence) if e in known_ev] or [e for e in m.scope_ev if e in known_ev]
            body = {"layer": "binding", "kind": kind, "subject": subject, "predicate": predicate, "object": obj,
                    "statement": statement, "origin": "inferred", "confidence": round(max(0.0, min(1.0, conf)), 3),
                    "evidence": ev, "explain": ""}
            aid = add_artifact(s, tenant, wsid, req.call_id, temp, "assertion", body,
                               [{"evidence_id": e, "snapshot_id": catalog["snapshot_id"]} for e in ev],
                               {"capability": "match", **run.versions(), "p_raw": body["confidence"]})
            items_out.append(ProposedAssertion(artifact_id=aid, version=1, **body))

        for i, sysm in enumerate(systems):
            if sysm["source_id"]:
                src = m.sources[sysm["source_id"]]
                emit(f"l1_{i}", "binding", sysm["system_ref"], "realized_by",
                     {"source_id": sysm["source_id"], "source_name": src["name"], "score": sysm["score"]},
                     f"系统对应数据源 {src['name']}", sysm["score"],
                     [src["evidence_id"]] + [x["evidence_id"] for x in scope.get("systems", []) if x["source_id"] == sysm["source_id"]])
        for i, (sem, (aid, sc, how)) in enumerate(sorted(semantics_used.items())):
            if aid:
                p = prof["assets"][aid]
                emit(f"l2_{i}", "binding", f"concept:{sem}", "stored_in",
                     {"semantic": sem, "semantic_display": cat.vocab().display(sem), "asset_id": aid,
                      "asset_name": p["name"], "score": sc, "role": p["role"], "how": how,
                      "candidates": _candidates(prof, sem)},
                     f"「{cat.vocab().display(sem)}」存放在表 {p['name']}", sc, [p["evidence_id"]])
        for i, summ in enumerate(summaries):
            ev = [e for x in summ["items"] for e in x["evidence"]]
            emit(f"ds_{i}", "binding", f"{summ['target']['artifact_id']}@{summ['target']['version']}", "data_support",
                 {k: summ[k] for k in ("target", "coverage", "gate", "answerability", "undetermined_reasons", "missing")}
                 | {"items": summ["items"]},
                 f"数据支撑：覆盖 {summ['coverage'] if summ['coverage'] is not None else '—'}，结论 {summ['gate']}",
                 summ["coverage"] or 0.0, ev)
        for i, u in enumerate(unclaimed):
            emit(f"un_{i}", "gap", f"asset:{u['asset']}", "unclaimed", u,
                 f"表 {u['name']} 没有对应到任何推荐的业务", 0.5, [u["evidence_id"]])

        plans_out = []
        for nid, p in plans.items():
            tgt = ArtifactRef(**p["fingerprint"]["for"])
            plans_out.append(PlanOut(artifact_id=p["fp_id"], kind="fingerprint", target=tgt, body=p["fingerprint"]))
            if p.get("plan") is not None and p.get("plan_id"):
                plans_out.append(PlanOut(artifact_id=p["plan_id"], kind="answer_plan", target=tgt, body=p["plan"]))
        result = MatchResult(items=items_out, plans=plans_out,
                             labels_ref={"snapshot_id": catalog["snapshot_id"], "artifact_id": labels_id},
                             node_summary=[NodeSummary(**x) for x in summaries], systems=systems, unclaimed=unclaimed)
        # 供 interview 读取的最新匹配结果（规格 v0.4 §3.9）
        add_artifact(s, tenant, wsid, req.call_id, "match_result", "match_result",
                     {"snapshot_id": catalog["snapshot_id"], "node_summary": summaries, "unclaimed": unclaimed,
                      "systems": systems, "incomplete_reasons": m.incomplete_reasons()}, [],
                     {"capability": "match", **run.versions()})
        resp = Response(call_id=req.call_id, capability="match", versions=Versions(**run.versions()), result=result,
                        warnings=run.warnings).model_dump(mode="json", by_alias=True)
        idem_finish(s, tenant, "match", req.call_id, resp)
        s.add(run.log("ok"))
        s.commit()
    except Exception:
        s.rollback()
        raise
    return resp


def _candidates(prof: dict[str, Any], sem: str) -> list[dict[str, Any]]:
    """给 FDE 改绑定时用的候选表：同标签 / 备选标签的表在前。"""
    rows = []
    for aid, p in prof["assets"].items():
        base = 1.0 if p["label"] == sem else (0.6 if sem in p["alt"] else 0.0)
        rows.append((base, aid, p["name"]))
    rows.sort(key=lambda r: (-r[0], r[2]))
    return [{"asset_id": aid, "name": name, "score": b} for b, aid, name in rows[:8]]
