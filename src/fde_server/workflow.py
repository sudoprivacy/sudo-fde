from sqlalchemy import select

from .models import Artifact, Catalog, Evidence, Job, WorkflowRun

STAGES = [
    ("intro", "业务概要", "先收集公司介绍、目标和关键资料"),
    ("challenge", "挑战与目标", "确认当前挑战、业务问题和期望结果"),
    ("data", "接入业务资料", "上传数据目录、DDL 与专业资料"),
    ("interview", "访谈与对齐", "围绕业务说法和结构化证据追问"),
    ("report", "理解与本体", "生成流程、系统、问题和业务本体"),
    ("refine", "细化与交付", "绑定字段、确认缺口并生成交付包"),
]


def state(s, ws, user_id):
    evidence = list(
        s.scalars(
            select(Evidence).where(
                Evidence.tenant_id == ws.tenant_id, Evidence.workspace_id == ws.id, Evidence.active.is_(True)
            )
        )
    )
    catalogs = list(
        s.scalars(select(Catalog).where(Catalog.tenant_id == ws.tenant_id, Catalog.workspace_id == ws.id))
    )
    artifacts = list(
        s.scalars(
            select(Artifact).where(
                Artifact.tenant_id == ws.tenant_id,
                Artifact.workspace_id == ws.id,
                Artifact.status.notin_(["superseded", "rejected"]),
            )
        )
    )
    jobs = list(
        s.scalars(
            select(Job)
            .where(Job.tenant_id == ws.tenant_id, Job.workspace_id == ws.id)
            .order_by(Job.created_at.desc())
        )
    )
    current = next((i for i, item in enumerate(STAGES) if item[0] == ws.phase), 0)
    has = {a.kind for a in artifacts}
    checks = {
        "intro": bool(evidence),
        "challenge": "summarize" in has,
        "data": bool(catalogs),
        "interview": "interview" in has or "match" in has,
        "report": "report" in has,
        "refine": ws.phase == "refine" or bool(artifacts and ws.version > 1),
    }
    active = next((j for j in jobs if j.status in {"queued", "running", "retry_wait"}), None)
    return {
        "current": ws.phase,
        "current_index": current,
        "stages": [
            {
                "id": sid,
                "name": name,
                "description": desc,
                "completed": checks[sid],
                "active": sid == ws.phase,
                "available": i <= current + 1,
            }
            for i, (sid, name, desc) in enumerate(STAGES)
        ],
        "next_action": ws.next_action,
        "objective": ws.objective,
        "evidence_count": len(evidence),
        "catalog_count": len(catalogs),
        "artifact_count": len(artifacts),
        "active_job": {"id": active.id, "capability": active.capability, "status": active.status}
        if active
        else None,
    }


def start_run(s, ws, user_id, stage):
    existing = s.scalar(
        select(WorkflowRun).where(
            WorkflowRun.tenant_id == ws.tenant_id,
            WorkflowRun.workspace_id == ws.id,
            WorkflowRun.stage == stage,
            WorkflowRun.status == "active",
        )
    )
    if existing:
        return existing
    row = WorkflowRun(
        tenant_id=ws.tenant_id, workspace_id=ws.id, stage=stage, started_by=user_id, metadata_={}
    )
    s.add(row)
    return row
