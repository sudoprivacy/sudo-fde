"""确定性 LLM 桩：按关键词从证据里拼出输出。

只用于：① 自动化测试；② 没有模型时把端到端流程跑通。
**它的推荐质量没有任何意义**，评测报告会把 fake 后端的结果标为"不可作为基线"。
测试可用 overrides={task: fn(call) -> dict} 替换某一步的输出。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ps.core.llm import LLMCall, Usage
from ps.core.text import similarity

# topic → (关键词, impact, affects, 开场维度, 追问文本, 选项)
_TOPICS: list[tuple[str, str, str, list[str], str | None, str, list[str]]] = [
    ("customers", "主要客户是谁", r"客户|餐厅|门店|企业客户|经销商", ["流程:销售下单"], "卖什么给谁",
     "你们主要把什么卖给哪类客户？", ["企业客户", "门店或餐厅", "个人消费者", "混合"]),
    ("revenue", "怎么赚钱", r"差价|订阅|服务费|佣金|赚钱|利润", ["问题:利润"], "怎么赚钱",
     "公司主要靠什么赚钱？", ["进销差价", "服务费或佣金", "订阅费", "项目款"]),
    ("fulfilment", "货或服务怎么送到客户手上", r"配送|物流|发货|自提|车队|快递", ["流程:配送", "系统:运输"],
     "货或服务怎么流转", "货送到客户手上，主要靠谁？", ["自己的车队", "第三方物流", "客户自提", "混合"]),
    ("payment", "客户是否有账期", r"账期|回款|赊账|月结|收款|应收", ["流程:回款", "问题:应收逾期"], None,
     "客户付款是现结还是有账期？", ["全部现结", "部分客户有账期", "大部分有账期"]),
    ("procurement", "采购怎么做", r"采购|供应商|订货|进货|补货", ["流程:补货", "问题:断货"], None,
     "能否上传一份采购制度或最近的采购单？", []),
    ("systems", "用什么系统或表格管业务", r"系统|ERP|Excel|表格|进销存|软件", ["系统:核心业务系统"],
     "用什么系统或表格管", "订单和库存现在记在哪里？", ["专门的软件系统", "Excel 表格", "纸质单据", "混合"]),
    ("pains", "最头疼的事", r"头疼|麻烦|经常|总是|痛点|压货|断货", ["问题:核心问题"], "最头疼的三件事",
     "现在业务上最头疼的三件事是什么？", []),
    ("headcount", "员工人数", r"员工|人数", [], None, "公司有多少员工？", []),   # 无 affects，应被丢弃
]

_CONTRA = ["零售", "生产", "配送"]

_PROCESSES = [
    ("补货", r"库存|盘点|断货|订货|补货|采购", ["盘点库存", "对比安全库存", "选择供应商", "下采购单", "到货入库"], ["仓库", "采购"]),
    ("订单到发货", r"订单|下单|发货|配送|物流", ["接单", "确认库存", "拣货打包", "安排发货", "签收"], ["销售", "仓库"]),
    ("回款", r"回款|账期|收款|应收|对账", ["开票", "跟踪账期", "催收", "确认到账"], ["财务", "销售"]),
]
_SYSTEMS = [
    ("进销存 / ERP", r"系统|ERP|进销存|库存"),
    ("Excel 表格", r"Excel|表格"),
    ("财务软件", r"财务|开票|对账"),
]
_QUESTIONS = [
    ("采购想知道：下两周哪些商品会断货？", "采购", r"断货|库存|压货",
     ["每个商品的当前库存", "安全库存或补货阈值", "已下单未到货数量及预计到货日", "过去至少 90 天的每日销量"]),
    ("财务想知道：哪些客户回款慢？", "财务", r"回款|账期|应收", ["每张发票的金额与开票日", "约定账期", "实际到账日"]),
    ("销售想知道：哪些订单发货延迟了？", "销售", r"发货|配送|延迟|物流", ["订单下单时间", "承诺发货时间", "实际发货时间"]),
]


def _ev_with(evidence: list[dict[str, Any]], pattern: str) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [e["id"] for e in evidence if rx.search(e.get("text") or "")]


class FakeLLM:
    name = "fake"

    def __init__(self, overrides: dict[str, Callable[[LLMCall], dict[str, Any]]] | None = None):
        self.overrides = overrides or {}
        self.calls: list[LLMCall] = []

    def complete_json(self, call: LLMCall) -> tuple[dict[str, Any], Usage]:
        self.calls.append(call)
        fn = self.overrides.get(call.task) or getattr(self, "_" + call.task.replace(".", "_"))
        return fn(call), Usage(calls=1)

    # ---- interview ----
    def _interview_hypothesis(self, call: LLMCall) -> dict[str, Any]:
        ev = call.data["evidence"]
        open_items = {i["topic"]: i["id"] for i in call.data["open_items"]}
        closed_topics = {i["topic"] for i in call.data["closed_items"]}
        unknowns, known = [], []
        for _key, topic, kw, affects, _dim, _q, _opts in _TOPICS:
            hits = _ev_with(ev, kw)
            if hits:
                known.append({"fact": f"已提到：{topic}", "evidence": hits[:3]})
            elif topic not in closed_topics:
                unknowns.append({"id": open_items.get(topic), "topic": topic,
                                 "impact": "high" if affects else "low", "affects": affects, "merges": []})
        contradictions = []
        for word in _CONTRA:
            neg = _ev_with(ev, f"不做{word}|没有{word}|只做批发" if word == "零售" else f"不做{word}|没有{word}")
            pos = [i for i in _ev_with(ev, word) if i not in neg]
            if neg and pos:
                topic = f"关于是否做{word}的说法不一致"
                if topic not in closed_topics:
                    contradictions.append({"id": open_items.get(topic), "note": topic, "between": [neg[0], pos[0]]})
        first = ev[0]["id"] if ev else None
        return {
            "profile": {
                "one_liner": (ev[0].get("text") or "")[:40] if ev else "",
                "business_type_candidates": [{"label": "待判断", "p": 0.5, "evidence": [first] if first else []}],
                "customers": {"who": "", "evidence": _ev_with(ev, _TOPICS[0][2])[:2]},
                "offerings": {"what": "", "evidence": []},
                "revenue_model": {"how": "", "evidence": _ev_with(ev, _TOPICS[1][2])[:2]},
            },
            "value_chain_draft": [{"stage": s, "evidence": _ev_with(ev, k)[:2]}
                                  for s, k in (("采购", "采购|供应商"), ("仓储", "库存|仓库"), ("销售", "订单|客户"),
                                               ("配送", "配送|物流|发货"), ("回款", "回款|收款"))],
            "known": known,
            "unknowns": unknowns,
            "contradictions": contradictions,
            "pains": [{"text": e.get("text", "")[:60], "evidence": [e["id"]]} for e in ev
                      if re.search(_TOPICS[6][2], e.get("text") or "")][:3],
        }

    def _interview_questions(self, call: LLMCall) -> dict[str, Any]:
        items = call.data["open_items_list"]
        by_topic = {t[1]: t for t in _TOPICS}
        out = []
        for it in items:
            if it["kind"].startswith("data_"):
                text = {"data_unclaimed": "系统里还记着一类没对上任何业务的记录，它对应你们的哪项业务？",
                        "data_absent": "这件事你们是在线下做、记在表格里，还是在别的系统里？",
                        "data_not_scanned": "能否把那个系统的数据结构也导出一份给我们？"}[it["kind"]]
                out.append({"text": text, "why": it["topic"], "resolves": it["id"], "options": [],
                            "allow_other": True, "kind": "request_document" if it["kind"] == "data_not_scanned" else "ask",
                            "dimension": None})
                continue
            if it["kind"] == "contradiction":
                out.append({"text": f"想确认一下：{it['topic']}，实际情况是？", "why": "证据之间有矛盾，先澄清",
                            "resolves": it["id"], "options": [], "allow_other": True, "kind": "ask", "dimension": None})
                continue
            t = by_topic.get(it["topic"])
            if not t:
                continue
            _k, topic, _kw, _aff, dim, qtext, opts = t
            out.append({"text": qtext, "why": f"影响：{'、'.join(it['affects']) or '—'}", "resolves": it["id"],
                        "options": opts, "allow_other": True,
                        "kind": "request_document" if _k == "procurement" else "ask",
                        "dimension": dim if call.data["round"] == 1 else None})
        if call.data["round"] == 1:
            data_ids = {i["id"] for i in items if i["kind"].startswith("data_")}
            out = [q for q in out if q["dimension"] or q["resolves"] in data_ids] or out
        return {"questions": out[: call.data["max_questions"]], "stop_reason": "fake"}

    # ---- recommend ----
    def _recommend_zero_pattern(self, call: LLMCall) -> dict[str, Any]:
        ev = call.data["evidence"]
        want = call.data["want"]
        res: dict[str, list] = {"processes": [], "systems": [], "questions": []}
        if "process" in want:
            for i, (name, kw, steps, roles) in enumerate(_PROCESSES):
                ids = _ev_with(ev, kw)
                if ids:
                    res["processes"].append({"temp_id": f"p{i}", "name": name, "statement": f"公司有{name}流程",
                                             "steps": steps, "roles": roles, "evidence": ids[:3],
                                             "confidence": min(0.9, 0.5 + 0.1 * len(ids)), "open_points": []})
        if "system" in want:
            for i, (cat, kw) in enumerate(_SYSTEMS):
                ids = _ev_with(ev, kw)
                if ids:
                    res["systems"].append({"temp_id": f"s{i}", "category": cat, "statement": f"业务记录在{cat}里",
                                           "candidates_named_by_user": [], "evidence": ids[:2],
                                           "confidence": 0.55, "open_points": []})
        if "question" in want:
            for i, (text, who, kw, needs) in enumerate(_QUESTIONS):
                ids = _ev_with(ev, kw)
                if ids:
                    res["questions"].append({"temp_id": f"q{i}", "text": text, "asked_by": who,
                                             "why_valuable": f"你提到：{(next(e for e in ev if e['id'] == ids[0]).get('text') or '')[:30]}",
                                             "data_needs": needs, "related_processes": [],
                                             "evidence": ids[:2], "confidence": 0.7})
        return res

    def _recommend_critic(self, call: LLMCall) -> dict[str, Any]:
        known = {e["id"] for e in call.data["evidence"]}
        verdicts = []
        for r in call.data["recommendations"]:
            ev = r.get("evidence") or []
            verdicts.append({"temp_id": r["temp_id"], "generic": not ev,
                             "citation_ok": bool(ev) and all(i in known for i in ev), "contradiction": False,
                             "answerable": True, "jargon": False, "note": ""})
        return {"verdicts": verdicts}

    def _recommend_merge(self, call: LLMCall) -> dict[str, Any]:
        from ps.core.text import containment
        groups: list[list[dict[str, Any]]] = []
        for it in call.data["items"]:
            g = next((g for g in groups if g[0]["kind"] == it["kind"]
                      and containment(g[0]["name"], it["name"]) >= 0.6), None)
            (g.append(it) if g else groups.append([it]))
        return {"groups": [{"members": [x["id"] for x in g]} for g in groups]}

    # ---- 阶段二：回答计划 / 指纹 / 语义标注 / L1 ----
    @staticmethod
    def _zh_labels(text: str, kinds: str = "both") -> list[str]:
        """按词表中文名在文本里出现的位置取标签（fake 专用的粗糙规则）。"""
        from ps.core.catalog import vocab
        v = vocab()
        pools = []
        if kinds in ("both", "columns"):
            pools.append(v.columns)
        if kinds in ("both", "entities"):
            pools.append(v.entities)
        hits = []
        for pool in pools:
            for lab, spec in pool.items():
                for zh in re.split(r"\s*/\s*", spec["zh"]):
                    if zh and zh in text and lab not in hits:
                        hits.append(lab)
        return hits

    _EXTRA = [("销量", "sales_order_line", "history"), ("断货", "stock_on_hand", "snapshot"),
              ("回款", "paid_date", "event"), ("账期", "payment_terms_days", "snapshot"),
              ("发票", "invoice", "event"), ("在途", "in_transit_qty", "snapshot"),
              ("阈值", "reorder_threshold", "snapshot"), ("安全库存", "reorder_threshold", "snapshot")]

    def _generate_answer_plan(self, call: LLMCall) -> dict[str, Any]:
        needs = call.data.get("data_needs") or [call.data.get("question", "")]
        inputs = []
        for n in needs:
            labs = [lab for kw, lab, _ in self._EXTRA if kw in n] or self._zh_labels(n)
            tmp = next((t for kw, _, t in self._EXTRA if kw in n), "snapshot")
            for lab in labs[:1]:
                inputs.append({"need": n, "semantic": lab, "temporal": tmp,
                               "min_history_days": 90 if tmp == "history" else None})
        q = call.data.get("question", "")
        return {"output": "list", "time_window": "未来 14 天" if "未来" in q or "下两周" in q else "当前",
                "method": "forecast" if ("未来" in q or "下两周" in q or "会" in q) else "lookup",
                "steps": ["取数", "计算", "输出"], "inputs": inputs}

    def _generate_fingerprint(self, call: LLMCall) -> dict[str, Any]:
        from ps.core.catalog import vocab
        v = vocab()
        plan = call.data.get("plan")
        t = call.data["target"]
        reqs = []
        if plan:
            for i, inp in enumerate(plan.get("inputs") or []):
                sem = inp["semantic"]
                if sem in v.entities:
                    reqs.append({"id": f"f{i + 1}", "kind": "event_table" if inp["temporal"] == "history" else "entity_table",
                                 "semantic": sem, "on": None, "path": [], "needs_columns": [],
                                 "min_history_days": inp.get("min_history_days"), "weight": 2, "must": i == 0})
                else:
                    reqs.append({"id": f"f{i + 1}", "kind": "column_semantic", "semantic": sem, "on": None, "path": [],
                                 "needs_columns": [], "min_history_days": None, "weight": 1, "must": i == 0})
        else:
            text = " ".join([t.get("name") or "", t.get("statement") or "", " ".join(t.get("steps") or [])])
            ents = self._zh_labels(text, "entities") or ["sales_order"]
            for i, e in enumerate(ents[:4]):
                reqs.append({"id": f"f{i + 1}", "kind": "entity_table", "semantic": e, "on": None, "path": [],
                             "needs_columns": [], "min_history_days": None, "weight": 2, "must": i == 0})
            if len(ents) >= 2:
                reqs.append({"id": f"f{len(reqs) + 1}", "kind": "fk_chain", "semantic": None, "on": None,
                             "path": ents[:2], "needs_columns": [], "min_history_days": None, "weight": 1, "must": False})
        return {"requires": reqs}

    def _match_label(self, call: LLMCall) -> dict[str, Any]:
        from ps.core.catalog import label_column, label_table
        out = []
        for t in call.data["tables"]:
            tl = label_table(t["name"])
            cols = []
            for c in t["columns"]:
                cl = label_column(c["name"], tl.label if tl else None, c["name"] in t["primary_key"], c.get("shape"))
                cols.append({"name": c["name"], "label": cl.label if cl else f"new:{c['name'].lower()}",
                             "confidence": 0.5 if cl else 0.3})
            out.append({"asset_id": t["asset_id"], "label": tl.label if tl else f"new:{t['name'].lower()}",
                        "alt": tl.alt if tl else [], "confidence": 0.75 if tl else 0.3, "role_hint": "entity",
                        "note": "fake", "columns": cols})
        return {"tables": out}

    def _match_l1(self, call: LLMCall) -> dict[str, Any]:
        from ps.core.text import containment
        out = []
        for sy in call.data["systems"]:
            text = f"{sy.get('category') or ''}{sy.get('statement') or ''}"
            best = max(call.data["sources"], key=lambda s: containment(s.get("system_hint") or s["name"], text), default=None)
            ok = best and containment(best.get("system_hint") or best["name"], text) >= 0.3
            out.append({"system_ref": sy["system_ref"], "source_id": best["source_id"] if ok else None,
                        "confidence": 0.6 if ok else 0.0, "reason": "fake"})
        return {"mappings": out}

    # ---- explain ----
    # ---- summarize / report（v0.7） ----
    def _understand_summary(self, call: LLMCall) -> dict[str, Any]:
        ev = call.data["evidence"]
        first = [ev[0]["id"]] if ev else []
        return {
            "one_liner": (ev[0].get("text") or "")[:40] if ev else "", "industry": "待判断",
            "offerings": {"what": "见原话", "evidence": first},
            "customers": {"who": "见原话", "evidence": _ev_with(ev, _TOPICS[0][2])[:2]},
            "revenue_model": {"how": "", "evidence": _ev_with(ev, _TOPICS[1][2])[:2]},
            "scale": {"note": "", "evidence": []},
            "business_lines": [{"name": "主营业务", "note": "", "evidence": first}],
            "value_chain": [{"stage": st, "evidence": _ev_with(ev, k)[:2]}
                            for st, k in (("采购", "采购|供应商"), ("销售", "订单|客户"), ("配送", "配送|发货"))],
            "systems_mentioned": [{"name": "Excel", "evidence": _ev_with(ev, "Excel|表格")[:1]}],
            "open_points": ["客户付款方式"],
            "challenge_questions": [
                {"dimension": "challenge", "text": "现在业务上最大的挑战是什么？", "why": "fake", "options": ["增长", "成本"]},
                {"dimension": "problem", "text": "日常碰到了哪些具体问题？", "why": "fake", "options": ["断货", "压货"]},
                {"dimension": "goal", "text": "最希望先解决什么？", "why": "fake", "options": ["少断货"]},
            ],
        }

    def _understand_report(self, call: LLMCall) -> dict[str, Any]:
        ev = call.data["evidence"]
        said = [e["id"] for e in ev if e["kind"] != "observation"][:2]
        obs = [e for e in ev if e["kind"] == "observation" and (e.get("observation") or {}).get("target") == "asset"]
        tables = [str(e["observation"]["locator"].get("asset")) for e in obs][:3]
        both = said + [e["id"] for e in obs[:1]]
        return {
            "overview": "fake 报告",
            "business_problems": [{"title": "经常断货", "statement": "热销品经常断货", "impact": "high",
                                   "basis": "said", "evidence": said},
                                  {"title": "无出处的问题", "statement": "x", "impact": "low", "basis": "said",
                                   "evidence": ["ev_not_exist"]}],
            "processes": [{"name": "补货", "steps": ["盘点", "下单"], "roles": ["采购"], "pain_points": ["靠经验"],
                           "data_support": "有订单记录", "basis": "both", "evidence": both}],
            "ontology": {
                "object_types": [{"name": "商品", "description": "卖的东西", "key_properties": ["库存"],
                                  "backed_by": tables + ["NotScanned"], "basis": "both", "evidence": both}],
                "link_types": [{"name": "订单包含商品", "from": "订单", "to": "商品", "cardinality": "多对多",
                                "evidence": both}],
                "action_types": [{"name": "补货", "on": "商品", "description": "下采购单", "evidence": both}],
            },
            "systems_to_build": [{"name": "库存看板", "purpose": "看库存", "solves": ["经常断货"], "priority": "P0",
                                  "basis": "data", "evidence": [e["id"] for e in obs[:1]] or said},
                                 {"name": "补货系统", "purpose": "补货", "solves": ["经常断货"], "priority": "P0",
                                  "basis": "said", "evidence": said}],
            "agents_to_build": [{"name": "补货建议 Agent", "goal": "提前提示断货", "trigger": "每天早上",
                                 "uses_objects": ["商品"], "actions": ["补货"], "human_check": "采购确认下单",
                                 "solves": ["经常断货"], "priority": "P0", "basis": "both", "evidence": both}],
            "open_points": ["供货周期"],
        }

    def _explain_explain(self, call: LLMCall) -> dict[str, Any]:
        ev = call.data["evidence"]
        quote = (ev[0].get("text") or "")[:40] if ev else ""
        return {"text": f"依据你说过的「{quote}」。" if quote else "证据还不充分，需要确认。"}

    # ---- evals ----
    def _eval_simulator(self, call: LLMCall) -> dict[str, Any]:
        facts: dict[str, str] = call.data["facts"]
        q = call.data["question"]
        best, score = None, 0.0
        for k, v in facts.items():
            s = max(similarity(q, k), similarity(q, v))
            if s > score:
                best, score = v, s
        return {"answer": best if best and score >= 0.12 else "这个不太清楚，得问问同事。"}

    def _eval_judge(self, call: LLMCall) -> dict[str, Any]:
        expected, got = call.data["expected"], call.data["candidates"]
        matches = []
        for e in expected:
            hit = max(got, key=lambda g: similarity(e, g), default=None)
            matches.append({"expected": e, "matched": hit if hit and similarity(e, hit) >= 0.2 else None})
        return {"matches": matches}

    # ---- refine（v1.0）：按中文说法里的关键词找列名；找不到时问题 / 属性判缺口，步骤判线下 ----
    _REFINE_KW = [
        (r"库存|存货", ["unitsinstock", "stock", "inventory", "qty_on_hand"]),
        (r"再订货|补货阈值|安全库存", ["reorderlevel", "reorder", "safety"]),
        (r"在途|已下单未到货|在订", ["unitsonorder", "onorder"]),
        (r"销量|数量", ["quantity", "qty"]),
        (r"单价|价格", ["unitprice", "price"]),
        (r"下单|接单|订单日期|下单时间", ["orderdate"]),
        (r"发货|出库", ["shippeddate", "shipdate"]),
        (r"承诺|要求到货|约定到货", ["requireddate"]),
        (r"承运|物流|运输", ["shipvia", "shipper"]),
        (r"供应商", ["supplierid"]),
        (r"客户", ["customerid", "companyname"]),
        (r"商品|产品", ["productid", "productname"]),
    ]

    def _refine_suggest(self, call: LLMCall) -> dict[str, Any]:
        cols = [(t["name"], c["name"]) for t in call.data["tables"] for c in t["columns"]]
        out = []
        for sl in call.data["slots"]:
            if sl.get("bound"):
                continue
            text = sl["label"] + " " + (sl.get("statement") or "")
            if sl["kind"] == "object":
                hit = [t["name"] for t in call.data["tables"] if t["name"].lower().rstrip("s") in text.lower()]
                if hit:
                    out.append({"slot_id": sl["id"], "action": "bind", "fields": [f"{hit[0]}.*"], "metric": None,
                                "why": "表名与对象说法一致", "confidence": 0.7})
                continue
            fields = []
            for kw, names in self._REFINE_KW:
                if re.search(kw, text):
                    fields = [f"{t}.{c}" for t, c in cols if c.lower() in names][:1]
                    if fields:
                        break
            if fields:
                metric = None
                if re.search(r"日销量|每日|按天|周期", text):
                    date = next((f"{t}.{c}" for t, c in cols if c.lower() == "orderdate"), None)
                    metric = {"grain": [g for g in [fields[0].split(".")[0] + ".ProductID", date] if g], "scope": "近 90 天"}
                out.append({"slot_id": sl["id"], "action": "bind", "fields": fields, "metric": metric,
                            "why": "列名含义与说法一致（演示用关键词匹配）", "confidence": 0.6})
            else:
                out.append({"slot_id": sl["id"], "action": "offline" if sl["kind"] == "step" else "gap", "fields": [],
                            "metric": None, "why": "已扫描的表里没有对应的列", "confidence": 0.4})
        return {"suggestions": out}

    # ---- revise（v1.1）：演示用——"X 是 Y"把含 X 的字段改成 Y，并把其他文字里旧说法的词换掉；删 / 去掉 → remove ----
    def _revise_revise(self, call: LLMCall) -> dict[str, Any]:
        text, targets = call.data["instruction"], call.data["targets"]
        if call.data["mode"] != "revise" and not re.search(r"修改|改成|改为|更正|不对|应该是|删掉|删除|去掉", text):
            return {"intent": "material", "understood": "", "clarify": None, "changes": [], "conflicts": [],
                    "suggest_regenerate": False}
        m = re.search(r"([一-龥A-Za-z]{1,6})是([^，。,.；;\s]+)", text)
        if not m:
            return {"intent": "unclear", "understood": "", "clarify": {"text": "要改哪一处？", "options": []},
                    "changes": [], "conflicts": [], "suggest_regenerate": False}
        key, new = m.group(1), m.group(2)
        field = {"客户": "customers", "行业": "industry", "产品": "offerings"}.get(key, key)
        direct = next((t for t in targets if field in t["path"]), None)
        if direct is None:
            return {"intent": "unclear", "understood": "", "clarify": {"text": f"「{key}」指的是哪一处？", "options": []},
                    "changes": [], "conflicts": [], "suggest_regenerate": False}
        changes = [{"target": direct["path"], "op": "update", "before": direct["text"], "after": new,
                    "why": f"FDE 更正：{key}是{new}", "role": "direct"}]
        olds = [w for w in re.split(r"[、，,和与及等\s]+", direct["text"]) if len(w) >= 2]
        for t in targets:
            if t is direct:
                continue
            hit = next((w for w in olds if w in t["text"]), None)
            if hit:
                changes.append({"target": t["path"], "op": "update", "before": t["text"], "after": t["text"].replace(hit, new),
                                "why": f"与「{key}」更正相关：原文写的是{hit}", "role": "ripple"})
        conflicts = [{"evidence": e["id"], "quote": w, "note": "原话与本次更正不一致，以更正为准"}
                     for e in call.data["evidence"] for w in olds[:1] if w in (e.get("text") or "")]
        return {"intent": "revise", "understood": f"把{key}更正为「{new}」", "clarify": None, "changes": changes,
                "conflicts": conflicts, "suggest_regenerate": False}
