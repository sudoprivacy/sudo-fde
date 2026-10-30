"""各 LLM 步骤的输出 JSON Schema（结构化输出约束）。"""

from __future__ import annotations

_STR = {"type": "string"}
_IDS = {"type": "array", "items": _STR}
_P = {"type": "number", "minimum": 0, "maximum": 1}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


HYPOTHESIS = _obj({
    "profile": _obj({
        "one_liner": _STR,
        "business_type_candidates": {"type": "array", "items": _obj({"label": _STR, "p": _P, "evidence": _IDS})},
        "customers": _obj({"who": _STR, "evidence": _IDS}),
        "offerings": _obj({"what": _STR, "evidence": _IDS}),
        "revenue_model": _obj({"how": _STR, "evidence": _IDS}),
    }),
    "value_chain_draft": {"type": "array", "items": _obj({"stage": _STR, "evidence": _IDS})},
    "known": {"type": "array", "items": _obj({"fact": _STR, "evidence": _IDS})},
    "unknowns": {"type": "array", "items": _obj({
        "id": {"type": ["string", "null"]},
        "topic": _STR,
        "impact": {"type": "string", "enum": ["high", "medium", "low"]},
        "affects": _IDS,
        "merges": _IDS,
    })},
    "contradictions": {"type": "array", "items": _obj({
        "id": {"type": ["string", "null"]}, "note": _STR, "between": _IDS})},
    "pains": {"type": "array", "items": _obj({"text": _STR, "evidence": _IDS})},
})

QUESTIONS = _obj({
    "questions": {"type": "array", "items": _obj({
        "text": _STR,
        "why": _STR,
        "resolves": {"type": ["string", "null"]},
        "options": {"type": "array", "items": _STR},
        "allow_other": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["ask", "request_document"]},
        "dimension": {"type": ["string", "null"]},
    })},
    "stop_reason": _STR,
})

_PROCESS = _obj({
    "temp_id": _STR, "name": _STR, "statement": _STR, "steps": {"type": "array", "items": _STR},
    "roles": {"type": "array", "items": _STR}, "evidence": _IDS, "confidence": _P,
    "open_points": {"type": "array", "items": _STR},
})
_SYSTEM = _obj({
    "temp_id": _STR, "category": _STR, "statement": _STR,
    "candidates_named_by_user": {"type": "array", "items": _STR}, "evidence": _IDS, "confidence": _P,
    "open_points": {"type": "array", "items": _STR},
})
_QUESTION = _obj({
    "temp_id": _STR, "text": _STR, "asked_by": _STR, "why_valuable": _STR,
    "data_needs": {"type": "array", "items": _STR}, "related_processes": {"type": "array", "items": _STR},
    "evidence": _IDS, "confidence": _P,
})

RECOMMEND = _obj({
    "processes": {"type": "array", "items": _PROCESS},
    "systems": {"type": "array", "items": _SYSTEM},
    "questions": {"type": "array", "items": _QUESTION},
})

CRITIC = _obj({
    "verdicts": {"type": "array", "items": _obj({
        "temp_id": _STR, "generic": {"type": "boolean"}, "citation_ok": {"type": "boolean"},
        "contradiction": {"type": "boolean"}, "answerable": {"type": "boolean"},
        "jargon": {"type": "boolean"}, "note": _STR,
    })},
})

EXPLAIN = _obj({"text": _STR})

MERGE = _obj({"groups": {"type": "array", "items": _obj({"members": {"type": "array", "items": _STR}})}})

# ---------- 阶段二 ----------
_STRN = {"type": ["string", "null"]}
_INTN = {"type": ["integer", "null"]}

ANSWER_PLAN = _obj({
    "output": _STR, "time_window": _STR,
    "method": {"type": "string", "enum": ["lookup", "aggregate", "rule", "forecast"]},
    "steps": {"type": "array", "items": _STR},
    "inputs": {"type": "array", "items": _obj({
        "need": _STR, "semantic": _STR,
        "temporal": {"type": "string", "enum": ["snapshot", "event", "history"]},
        "min_history_days": _INTN})},
})

FINGERPRINT = _obj({
    "requires": {"type": "array", "items": _obj({
        "id": _STR,
        "kind": {"type": "string", "enum": ["entity_table", "event_table", "column_semantic", "fk_chain",
                                             "status_column", "event_time"]},
        "semantic": _STRN, "on": _STRN, "path": {"type": "array", "items": _STR},
        "needs_columns": {"type": "array", "items": _STR}, "min_history_days": _INTN,
        "weight": {"type": "integer", "minimum": 1, "maximum": 3}, "must": {"type": "boolean"}})},
})

LABELS = _obj({
    "tables": {"type": "array", "items": _obj({
        "asset_id": _STR, "label": _STR, "alt": {"type": "array", "items": _STR}, "confidence": _P,
        "role_hint": {"type": "string", "enum": ["entity", "detail", "event", "dictionary", "log"]}, "note": _STR,
        "columns": {"type": "array", "items": _obj({"name": _STR, "label": _STR, "confidence": _P})}})},
})

L1 = _obj({"mappings": {"type": "array", "items": _obj({
    "system_ref": _STR, "source_id": _STRN, "confidence": _P, "reason": _STR})}})

# ---------- v0.7：业务概要（第一步）与业务理解报告（第五步） ----------
_EV = {"evidence": _IDS}
_BASIS = {"type": "string", "enum": ["said", "data", "both"]}

SUMMARY = _obj({
    "one_liner": _STR,
    "industry": _STR,
    "offerings": _obj({"what": _STR, **_EV}),
    "customers": _obj({"who": _STR, **_EV}),
    "revenue_model": _obj({"how": _STR, **_EV}),
    "scale": _obj({"note": _STR, **_EV}),
    "business_lines": {"type": "array", "items": _obj({"name": _STR, "note": _STR, **_EV})},
    "value_chain": {"type": "array", "items": _obj({"stage": _STR, **_EV})},
    "systems_mentioned": {"type": "array", "items": _obj({"name": _STR, **_EV})},
    "open_points": {"type": "array", "items": _STR},
    "challenge_questions": {"type": "array", "items": _obj({
        "dimension": {"type": "string", "enum": ["challenge", "problem", "goal"]},
        "text": _STR, "why": _STR, "options": {"type": "array", "items": _STR}})},
})

REPORT = _obj({
    "overview": _STR,
    "business_problems": {"type": "array", "items": _obj({
        "title": _STR, "statement": _STR, "impact": {"type": "string", "enum": ["high", "medium", "low"]},
        "basis": _BASIS, **_EV})},
    "processes": {"type": "array", "items": _obj({
        "name": _STR, "steps": {"type": "array", "items": _STR}, "roles": {"type": "array", "items": _STR},
        "pain_points": {"type": "array", "items": _STR}, "data_support": _STR, "basis": _BASIS, **_EV})},
    "ontology": _obj({
        "object_types": {"type": "array", "items": _obj({
            "name": _STR, "description": _STR, "key_properties": {"type": "array", "items": _STR},
            "backed_by": {"type": "array", "items": _STR}, "basis": _BASIS, **_EV})},
        "link_types": {"type": "array", "items": _obj({
            "name": _STR, "from": _STR, "to": _STR, "cardinality": _STR, **_EV})},
        "action_types": {"type": "array", "items": _obj({
            "name": _STR, "on": _STR, "description": _STR, **_EV})},
    }),
    "systems_to_build": {"type": "array", "items": _obj({
        "name": _STR, "purpose": _STR, "solves": {"type": "array", "items": _STR},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]}, "basis": _BASIS, **_EV})},
    "agents_to_build": {"type": "array", "items": _obj({
        "name": _STR, "goal": _STR, "trigger": _STR, "uses_objects": {"type": "array", "items": _STR},
        "actions": {"type": "array", "items": _STR}, "human_check": _STR, "solves": {"type": "array", "items": _STR},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]}, "basis": _BASIS, **_EV})},
    "open_points": {"type": "array", "items": _STR},
})


REFINE = _obj({
    "suggestions": {"type": "array", "items": _obj({
        "slot_id": _STR, "action": {"type": "string", "enum": ["bind", "gap", "offline"]},
        "fields": {"type": "array", "items": _STR},
        "metric": {"type": ["object", "null"], "properties": {"grain": {"type": "array", "items": _STR}, "scope": _STR}},
        "why": _STR, "confidence": _P,
    })},
})


REVISE = _obj({
    "intent": {"type": "string", "enum": ["revise", "material", "answer", "question", "unclear"]},
    "understood": _STR,
    "clarify": {"type": ["object", "null"], "properties": {"text": _STR, "options": {"type": "array", "items": _STR}}},
    "changes": {"type": "array", "items": _obj({
        "target": _STR, "op": {"type": "string", "enum": ["update", "add", "remove"]},
        "before": {"type": ["string", "null"]}, "after": {"type": ["string", "null"]},
        "why": _STR, "role": {"type": "string", "enum": ["direct", "ripple"]},
    })},
    "conflicts": {"type": "array", "items": _obj({"evidence": _STR, "quote": _STR, "note": _STR})},
    "suggest_regenerate": {"type": "boolean"},
})
