"""业务流程 → BPMN 2.0（OMG / ISO 19510）XML，带图形信息（BPMN DI），joy 2026-10-26。

为什么是 BPMN：它之于流程，正如 RDF / OWL 之于本体——标准交换格式，Camunda Modeler、Flowable、jBPM、
Activiti、Bonita、Signavio 等都能直接打开继续编辑；页面上用开源的 bpmn-js 按标准图形展示。

生成规则（描述性模型，isExecutable="false"，交给流程引擎前由人补执行细节）：
- 开始事件 → 每个步骤一个用户任务 → 结束事件，顺序流依次相连；
- 泳道：步骤文字里能认出至少两个角色时才画，步骤归到它提到的角色，没提到的沿用上一步（启发式，需要人确认），
  空泳道不画；认不出时不画泳道，角色写进注释，免得把所有步骤都塞进第一条泳道；
- 待确认事项、数据支撑写成文本注释；状态、置信度、证据写进 documentation 与扩展属性 sudo:*；
- ID 由流程名和步骤序号算出，同一流程每次导出 ID 不变，便于在其他引擎里对照修改。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from xml.sax.saxutils import escape

NS_SUDO = "urn:sudo:fde:bpmn"
TASK_W, TASK_H, GAP = 130, 72, 46
EV = 36
LANE_H = 130
POOL_LABEL_W = 30
LANE_LABEL_W = 30


def _id(prefix: str, *parts: Any) -> str:
    key = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def _a(x: Any) -> str:
    return escape(str(x if x is not None else ""), {'"': "&quot;"})


def _lanes(steps: list[str], roles: list[str]) -> tuple[list[str], list[int]]:
    """返回（实际画出的泳道角色, 每个步骤归到哪条泳道）。认不出至少两个角色时不画泳道（全为 -1）。"""
    hits = [next((i for i, r in enumerate(roles) if r and r in s), None) for s in steps]
    if len({h for h in hits if h is not None}) < 2:
        return [], [-1] * len(steps)
    first = next(h for h in hits if h is not None)
    out, cur = [], first
    for h in hits:
        cur = h if h is not None else cur
        out.append(cur)
    used = sorted(set(out))                       # 空泳道不画
    remap = {old: new for new, old in enumerate(used)}
    return [roles[i] for i in used], [remap[i] for i in out]


def process_bpmn(p: dict[str, Any], ws_name: str = "") -> str:
    """p：{name, steps, roles, statement, status, confidence, evidence, id, open_points?, alignment?}"""
    name = p.get("name") or p.get("statement") or "业务流程"
    steps = [s for s in (p.get("steps") or []) if str(s).strip()] or [name]
    roles = [r for r in (p.get("roles") or []) if str(r).strip()]
    base = p.get("id") or name
    pid, defs_id = _id("Process", base), _id("Definitions", base)
    all_roles = roles
    roles, lane_of = _lanes(steps, all_roles)
    n_lanes = max(1, len(roles))
    use_lanes = bool(roles)

    # ---------- 坐标 ----------
    left = (POOL_LABEL_W + LANE_LABEL_W if use_lanes else 0) + 40
    xs, x = [], left + EV + GAP
    for _ in steps:
        xs.append(x)
        x += TASK_W + GAP
    end_x = x
    width = end_x + EV + 40
    height = (LANE_H * n_lanes) if use_lanes else TASK_H + 80

    def lane_cy(i: int) -> float:
        return (LANE_H * (i + 0.5)) if use_lanes else height / 2

    start_cy = lane_cy(lane_of[0] if lane_of[0] >= 0 else 0)
    end_cy = lane_cy(lane_of[-1] if lane_of[-1] >= 0 else 0)

    # ---------- 语义 ----------
    start_id, end_id = _id("StartEvent", base), _id("EndEvent", base)
    task_ids = [_id("Task", base, i) for i in range(len(steps))]
    chain = [start_id, *task_ids, end_id]
    flows = [(_id("Flow", base, i), chain[i], chain[i + 1]) for i in range(len(chain) - 1)]
    doc = "；".join(x for x in [
        p.get("statement"),
        f"状态：{p.get('status')}，置信度 {p.get('confidence')}" if p.get("status") else None,
        ("数据对齐：" + str((p.get("alignment") or {}).get("state")) +
         (f"，覆盖 {round(float(p['alignment']['coverage']) * 100)}%" if (p.get("alignment") or {}).get("coverage") is not None else ""))
        if p.get("alignment") else None,
        ("证据：" + "、".join(p.get("evidence") or [])) if p.get("evidence") else None,
    ] if x)
    notes = [("角色：" + "、".join(all_roles)) if all_roles and not use_lanes else None,
             ("待确认：" + "；".join(p["open_points"])) if p.get("open_points") else None,
             ("泳道按步骤文字里提到的角色推断，需要人确认" if use_lanes else None)]
    notes = [n for n in notes if n]

    sem: list[str] = []
    sem.append(f'  <bpmn:process id="{pid}" name="{_a(name)}" isExecutable="false" sudo:status="{_a(p.get("status"))}" '
               f'sudo:confidence="{_a(p.get("confidence"))}" sudo:artifactId="{_a(p.get("id"))}">')
    if doc:
        sem.append(f"    <bpmn:documentation>{escape(doc)}</bpmn:documentation>")
    if use_lanes:
        sem.append(f'    <bpmn:laneSet id="{_id("LaneSet", base)}">')
        for li, r in enumerate(roles):
            sem.append(f'      <bpmn:lane id="{_id("Lane", base, li)}" name="{_a(r)}">')
            members = []
            if li == (lane_of[0] if lane_of[0] >= 0 else 0):
                members.append(start_id)
            members += [task_ids[i] for i, l in enumerate(lane_of) if l == li]
            if li == (lane_of[-1] if lane_of[-1] >= 0 else 0):
                members.append(end_id)
            sem += [f"        <bpmn:flowNodeRef>{m}</bpmn:flowNodeRef>" for m in members]
            sem.append("      </bpmn:lane>")
        sem.append("    </bpmn:laneSet>")
    sem.append(f'    <bpmn:startEvent id="{start_id}" name="开始"><bpmn:outgoing>{flows[0][0]}</bpmn:outgoing></bpmn:startEvent>')
    traces = {t["step"]: t for t in p.get("traces") or []}     # 第 ⑥ 步细化结果：每一步的留痕字段
    for i, (tid, s) in enumerate(zip(task_ids, steps)):
        tr = traces.get(s)
        tdoc, tattr = "", ""
        if tr:
            tdoc = ("数据痕迹：" + "、".join(tr["fields"]) if tr["fields"] else
                    {"offline": "线下完成，不在数据里留痕", "gap": "数据里没有这一步的痕迹"}.get(tr["state"], "留痕待细化"))
            tdoc = f"<bpmn:documentation>{escape(tdoc)}</bpmn:documentation>"
            tattr = f' sudo:trace="{_a(tr["state"])}"' + (f' sudo:traceFields="{_a(" ".join(tr["fields"]))}"' if tr["fields"] else "")
        sem.append(f'    <bpmn:userTask id="{tid}" name="{_a(s)}"{tattr}>{tdoc}'
                   f"<bpmn:incoming>{flows[i][0]}</bpmn:incoming><bpmn:outgoing>{flows[i + 1][0]}</bpmn:outgoing></bpmn:userTask>")
    sem.append(f'    <bpmn:endEvent id="{end_id}" name="结束"><bpmn:incoming>{flows[-1][0]}</bpmn:incoming></bpmn:endEvent>')
    for fid, a, b in flows:
        sem.append(f'    <bpmn:sequenceFlow id="{fid}" sourceRef="{a}" targetRef="{b}" />')
    note_ids = []
    for k, text in enumerate(notes):
        nid, aid = _id("Note", base, k), _id("Assoc", base, k)
        note_ids.append(nid)
        sem.append(f'    <bpmn:textAnnotation id="{nid}"><bpmn:text>{escape(text)}</bpmn:text></bpmn:textAnnotation>')
        sem.append(f'    <bpmn:association id="{aid}" sourceRef="{task_ids[0]}" targetRef="{nid}" />')
    sem.append("  </bpmn:process>")

    collab = ""
    part_id = _id("Participant", base)
    if use_lanes:
        collab = (f'  <bpmn:collaboration id="{_id("Collaboration", base)}">\n'
                  f'    <bpmn:participant id="{part_id}" name="{_a(name)}" processRef="{pid}" />\n'
                  f"  </bpmn:collaboration>")

    # ---------- 图形（BPMN DI） ----------
    di: list[str] = []
    plane_ref = _id("Collaboration", base) if use_lanes else pid
    di.append(f'  <bpmndi:BPMNDiagram id="{_id("Diagram", base)}" name="{_a(name)}">')
    di.append(f'    <bpmndi:BPMNPlane id="{_id("Plane", base)}" bpmnElement="{plane_ref}">')
    if use_lanes:
        di.append(f'      <bpmndi:BPMNShape id="{part_id}_di" bpmnElement="{part_id}" isHorizontal="true">'
                  f'<dc:Bounds x="0" y="0" width="{width}" height="{height}" /></bpmndi:BPMNShape>')
        for li in range(len(roles)):
            lid = _id("Lane", base, li)
            di.append(f'      <bpmndi:BPMNShape id="{lid}_di" bpmnElement="{lid}" isHorizontal="true">'
                      f'<dc:Bounds x="{POOL_LABEL_W}" y="{LANE_H * li}" width="{width - POOL_LABEL_W}" height="{LANE_H}" /></bpmndi:BPMNShape>')

    def shape(eid: str, x: float, y: float, w: float, h: float, label: bool = False) -> None:
        lab = (f'<bpmndi:BPMNLabel><dc:Bounds x="{x - 10}" y="{y + h + 4}" width="{w + 20}" height="14" /></bpmndi:BPMNLabel>'
               if label else "")
        di.append(f'      <bpmndi:BPMNShape id="{eid}_di" bpmnElement="{eid}"><dc:Bounds x="{x}" y="{y}" width="{w}" height="{h}" />{lab}</bpmndi:BPMNShape>')

    shape(start_id, left, start_cy - EV / 2, EV, EV, True)
    centers = {start_id: (left + EV, start_cy, left)}
    for i, tid in enumerate(task_ids):
        cy = lane_cy(lane_of[i] if lane_of[i] >= 0 else 0)
        shape(tid, xs[i], cy - TASK_H / 2, TASK_W, TASK_H)
        centers[tid] = (xs[i] + TASK_W, cy, xs[i])
    shape(end_id, end_x, end_cy - EV / 2, EV, EV, True)
    centers[end_id] = (end_x + EV, end_cy, end_x)
    for fid, a, b in flows:
        ax, ay, _ = centers[a]
        _, by, bx = centers[b]
        pts = [(ax, ay), (bx, by)] if ay == by else [(ax, ay), ((ax + bx) / 2, ay), ((ax + bx) / 2, by), (bx, by)]
        wps = "".join(f'<di:waypoint x="{x_}" y="{y_}" />' for x_, y_ in pts)
        di.append(f'      <bpmndi:BPMNEdge id="{fid}_di" bpmnElement="{fid}">{wps}</bpmndi:BPMNEdge>')
    for k, nid in enumerate(note_ids):
        nx, ny = xs[0] + 40 + k * 230, -80            # 注释放在图上方
        di.append(f'      <bpmndi:BPMNShape id="{nid}_di" bpmnElement="{nid}"><dc:Bounds x="{nx}" y="{ny}" width="210" height="56" /></bpmndi:BPMNShape>')
        tx, ty, _ = centers[task_ids[0]]
        di.append(f'      <bpmndi:BPMNEdge id="{_id("Assoc", base, k)}_di" bpmnElement="{_id("Assoc", base, k)}">'
                  f'<di:waypoint x="{tx - TASK_W / 2}" y="{ty - TASK_H / 2}" /><di:waypoint x="{nx + 30}" y="{ny + 56}" /></bpmndi:BPMNEdge>')
    di.append("    </bpmndi:BPMNPlane>")
    di.append("  </bpmndi:BPMNDiagram>")

    head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<!-- {escape(ws_name)} · 业务流程「{escape(name)}」· 由 FDE Studio 生成的描述性 BPMN 2.0 模型，可用 Camunda Modeler / Flowable / jBPM 等继续编辑 -->\n'
            f'<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" '
            'xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" '
            f'xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:sudo="{NS_SUDO}" id="{defs_id}" '
            f'targetNamespace="urn:sudo:fde:bpmn" exporter="FDE Studio" exporterVersion="0.1">')
    body = "\n".join(x for x in [collab, *sem, *di] if x)
    return f"{head}\n{body}\n</bpmn:definitions>\n"


def bpmn_filename(p: dict[str, Any]) -> str:
    return f"processes/{_id('p', p.get('id') or p.get('name'))}.bpmn"


def safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text or "process").strip("_")[:40] or "process"
