"""Resolve audit display metadata without changing append-only event records."""

from collections import defaultdict

from sqlalchemy import select

from . import models as m
from .security import utc

ROLES = {
    "tenant_owner": "组织所有者", "tenant_admin": "组织管理员", "fde_member": "FDE 成员",
    "billing_viewer": "账务查看者", "workspace_editor": "工作区编辑者",
    "workspace_viewer": "工作区查看者", "platform_admin": "平台管理员",
}
CAPABILITIES = {
    "summarize": "业务概要", "interview": "访谈追问", "recommend": "业务推荐", "match": "数据对齐",
    "report": "业务理解", "revise": "结果修订", "refine": "业务细化", "learn": "反馈回传",
    "ingest": "资料索引", "ingestion": "资料索引", "process": "业务流程", "system": "系统建议",
    "question": "业务问题", "ontology": "业务本体", "binding": "数据绑定", "gap": "数据缺口",
}
# Each event explicitly identifies its resource type; IDs are never guessed across tables.
ACTIONS = {
    "organization.created": ("创建组织", "tenant"),
    "organization.renamed": ("修改组织名称", "tenant"),
    "organization.name_normalized": ("规范组织名称", "tenant"),
    "acceptance.session_created": ("创建测试会话", "account"),
    "platform.tenant_created": ("创建租户", "tenant"),
    "platform.tenant_updated": ("修改租户信息", "tenant"),
    "platform.tenant_deleted": ("删除租户", "tenant"),
    "platform.tenant_suspended": ("停用租户", "tenant"),
    "platform.tenant_restored": ("恢复租户", "tenant"),
    "platform.user_status_changed": ("调整用户状态", "user"),
    "platform.membership_assigned": ("关联用户到租户", "user"),
    "platform.membership_updated": ("调整用户租户关联", "user"),
    "platform.membership_removed": ("移除用户租户关联", "user"),
    "platform.admin_granted": ("授予平台管理员权限", "user"),
    "auth.login": ("登录账号", "account"), "auth.logout": ("退出登录", "account"),
    "member.invited": ("邀请组织成员", "invitation"), "member.joined": ("加入组织", "user"),
    "member.changed": ("调整组织成员", "user"),
    "workspace.created": ("创建工作区", "workspace"), "workspace.viewed": ("访问工作区", "workspace"),
    "workspace.renamed": ("重命名工作区", "workspace"), "workspace.deleted": ("归档工作区", "workspace"),
    "workspace.restored": ("取消归档工作区", "workspace"),
    "workspace.member_changed": ("设置工作区协作权限", "user"),
    "workspace.member_revoked": ("撤销工作区协作权限", "user"),
    "workflow.objective_set": ("设置业务目标", "workspace"),
    "model_provider.created": ("添加模型提供商", "provider"),
    "model_provider.updated": ("修改模型提供商", "provider"),
    "model_provider.verified": ("验证模型连接", "provider"),
    "model_provider.deleted": ("删除模型提供商", "provider"),
    "evidence.added": ("保存业务资料", "evidence"), "catalog.uploaded": ("上传数据目录", "catalog"),
    "job.queued": ("提交任务", "job"), "job.succeeded": ("任务完成", "job"),
    "job.failed": ("任务失败", "job"), "job.retry": ("任务等待重试", "job"),
    "job.requeued": ("重新提交任务", "job"), "job.start_rejected": ("任务未能启动", "job"),
    "artifact.decided": ("审核业务产出物", "artifact"),
    "revision.applied": ("应用业务修订", "revision"), "revision.undone": ("撤销业务修订", "revision"),
    "refinement.changed": ("调整字段绑定", "refinement"),
    "knowledge.created": ("创建知识库", "knowledge"), "knowledge.revoked": ("停用知识库", "knowledge"),
    "knowledge.retrieved": ("检索专业资料", "retrieval"),
    "knowledge.granted": ("授予知识库访问权限", "grant"),
    "knowledge.grant_revoked": ("撤销知识库访问权限", "grant"),
    "document.uploaded": ("上传专业资料", "document"), "document.downloaded": ("下载专业资料", "document"),
    "document.revoked": ("撤销专业资料", "document"), "document.purged": ("清理已撤销资料", "document"),
    "router.bound": ("绑定组织模型账户", "tenant"), "router.policy_changed": ("修改组织模型策略", "tenant"),
    "router.reconciled": ("模型费用对账一致", "call"), "router.mismatch": ("模型费用对账不一致", "call"),
    "maintenance.reconcile_failed": ("自动对账失败", "maintenance"),
    "export.created": ("生成交付包", "export"), "export.downloaded": ("下载交付包", "export"),
}
KINDS = {
    "tenant": "租户", "user": "用户", "account": "账号", "invitation": "成员邀请", "workspace": "工作区",
    "provider": "模型提供商", "evidence": "业务资料", "catalog": "数据目录", "job": "任务",
    "artifact": "业务产出物", "revision": "业务修订", "refinement": "字段绑定", "knowledge": "知识库",
    "retrieval": "专业资料检索", "grant": "知识库授权", "document": "专业资料", "call": "模型调用",
    "maintenance": "系统维护", "export": "交付包", "resource": "资源",
}
# Select only the fields needed for display, never credentials, document text or model input.
SOURCES = {
    "workspace": (m.Workspace, ("name",)), "invitation": (m.Invitation, ("email",)),
    "provider": (m.ModelProvider, ("display_name",)), "catalog": (m.Catalog, ("name", "workspace_id")),
    "knowledge": (m.KnowledgeBase, ("name", "workspace_id")),
    "document": (m.Document, ("name", "workspace_id")), "export": (m.Export, ("name", "workspace_id")),
    "job": (m.Job, ("capability", "workspace_id")), "artifact": (m.Artifact, ("kind", "version", "workspace_id")),
    "evidence": (m.Evidence, ("workspace_id",)), "revision": (m.Revision, ("workspace_id",)),
    "grant": (m.KnowledgeGrant, ("kb_id",)), "call": (m.RouterCall, ("model", "capability", "workspace_id")),
}
ERRORS = {
    "PROVIDER_TIMEOUT": "模型生成超时", "PROVIDER_AUTH_FAILED": "模型密钥无效或模型未获授权",
    "PROVIDER_REQUEST_FAILED": "模型服务拒绝请求", "PROVIDER_RESPONSE_INVALID": "模型返回内容格式无效",
    "PROVIDER_OUTPUT_LIMIT": "模型输出达到长度上限", "MODEL_CAPABILITY_UNSUPPORTED": "模型不支持业务分析",
    "MODEL_NOT_CONFIGURED": "尚未配置可用模型", "PROVIDER_NOT_FOUND": "模型提供商已停用或无权访问",
    "MODEL_NOT_ALLOWED": "模型未获授权", "QUOTA_EXHAUSTED": "模型额度不足",
    "ROUTER_NOT_BOUND": "组织模型账户未绑定", "ROUTER_UNAVAILABLE": "模型网关暂时不可用",
    "PATTERN_UNAVAILABLE": "业务服务暂时不可用", "WORKSPACE_CHANGED": "工作区资料已变化",
    "ACCESS_REVOKED": "访问权限已撤销", "SOURCE_REVOKED": "引用资料已撤销或更新",
    "INPUT_SENSITIVE": "资料包含需脱敏的信息", "CONTEXT_TOO_LARGE": "资料超过上下文限制",
    "CAPABILITY_BUDGET_EXCEEDED": "任务达到预算上限", "PIPELINE_CHANGED": "任务流程版本已变化",
    "INTERNAL_ERROR": "服务内部错误", "FORBIDDEN": "没有操作权限",
}
DETAIL_LABELS = {
    "role": "角色", "active": "状态", "tenant_id": "关联租户", "owner_user_id": "组织所有者",
    "subject_type": "授权对象类型", "subject_id": "授权对象", "provider_id": "模型提供商",
    "capability": "业务能力", "decision": "审核结论", "version": "资料版本", "count": "检索结果数",
    "changes": "修改项数", "budget_micros": "额度上限（微单位）", "allowed_models": "允许使用的模型",
    "matched": "对账结果", "code": "原因", "name": "名称", "previous_name": "原名称",
}
DECISIONS = {"accept": "已确认", "edit": "修改后确认", "reject": "已驳回"}


def present_audits(session, rows):
    """Enrich one authorized page, with bounded batch queries and tenant-keyed lookups."""
    rows = list(rows)
    if not rows:
        return []
    tenant_ids = {row.tenant_id for row in rows}
    tenant_names = dict(session.execute(select(m.Tenant.id, m.Tenant.name).where(m.Tenant.id.in_(tenant_ids))).all())
    wanted = defaultdict(set)
    user_ids = {row.user_id for row in rows if row.user_id}
    for row in rows:
        kind = ACTIONS.get(row.action, ("其他操作", "resource"))[1]
        if row.resource_id:
            wanted[kind].add(row.resource_id)
            if kind == "user":
                user_ids.add(row.resource_id)
        if row.workspace_id:
            wanted["workspace"].add(row.workspace_id)
        details = row.details or {}
        if details.get("owner_user_id"):
            user_ids.add(details["owner_user_id"])
        if details.get("subject_type") == "user" and details.get("subject_id"):
            user_ids.add(details["subject_id"])
        if details.get("subject_type") == "workspace" and details.get("subject_id"):
            wanted["workspace"].add(details["subject_id"])
    users = {
        row.id: {"name": row.name or row.email, "email": row.email}
        for row in session.execute(select(m.User.id, m.User.name, m.User.email).where(m.User.id.in_(user_ids)))
    }
    memberships = set(session.execute(select(m.Membership.tenant_id, m.Membership.user_id).where(
        m.Membership.tenant_id.in_(tenant_ids), m.Membership.user_id.in_(user_ids)
    )))
    resources = {}

    def load(kind):
        model, columns = SOURCES[kind]
        key = model.operation_id if kind == "call" else model.id
        if not wanted[kind]:
            return
        records = session.execute(select(model.tenant_id, key.label("lookup_id"),
                                         *(getattr(model, name) for name in columns)).where(
            model.tenant_id.in_(tenant_ids), key.in_(wanted[kind])
        ))
        for record in records:
            data = dict(record._mapping)
            resources[kind, record.tenant_id, record.lookup_id] = data
            if data.get("workspace_id"):
                wanted["workspace"].add(data["workspace_id"])
            if data.get("kb_id"):
                wanted["knowledge"].add(data["kb_id"])

    for kind in SOURCES:
        if kind not in {"workspace", "knowledge"}:
            load(kind)
    load("knowledge")
    load("workspace")

    def user_name(user_id, tenant_id):
        if session.info.get("platform_admin") or (tenant_id, user_id) in memberships:
            return users.get(user_id, {}).get("name", "历史用户（名称不可用）")
        return "历史用户（名称不可用）"

    def name(kind, tenant_id, resource_id):
        if kind == "tenant":
            return tenant_names.get(resource_id, "历史租户（名称不可用）") if resource_id == tenant_id else "历史租户（名称不可用）"
        if kind == "user":
            return user_name(resource_id, tenant_id)
        resource = resources.get((kind, tenant_id, resource_id))
        if not resource:
            return KINDS[kind] + "（名称不可用）"
        if kind == "job":
            return CAPABILITIES.get(resource["capability"], "业务处理") + "任务"
        if kind == "artifact":
            return f'{CAPABILITIES.get(resource["kind"], "业务产出物")} · 第 {resource["version"]} 版'
        if kind == "call":
            return f'{CAPABILITIES.get(resource["capability"], "业务处理")} · {resource["model"]}'
        if kind == "grant":
            return name("knowledge", tenant_id, resource["kb_id"])
        return resource.get("name") or resource.get("display_name") or resource.get("email") or KINDS[kind]

    def detail_items(row, resource_name):
        result = []
        data = row.details or {}

        def add(key, value):
            if key == "changes" and isinstance(value, dict):
                for field, changed in value.items():
                    add(field, changed)
                return
            if key not in DETAIL_LABELS:
                return
            if key == "role":
                value = ROLES.get(value, "其他角色")
            elif key == "active":
                value = "启用" if value else "停用"
            elif key == "tenant_id":
                value = name("tenant", row.tenant_id, value)
            elif key == "owner_user_id":
                value = user_name(value, row.tenant_id) if value else "未指定"
            elif key == "subject_type":
                value = {"user": "用户", "workspace": "工作区", "role": "组织角色"}.get(value, "其他对象")
            elif key == "subject_id":
                subject_type = data.get("subject_type")
                value = (ROLES.get(value, "其他角色") if subject_type == "role" else
                         name(subject_type, row.tenant_id, value) if subject_type in {"user", "workspace"} else "其他对象")
            elif key == "provider_id":
                value = resource_name
            elif key == "capability":
                value = CAPABILITIES.get(value, "业务处理")
            elif key == "decision":
                value = DECISIONS.get(value, "已记录审核结论")
            elif key == "code":
                value = ERRORS.get(value, "处理异常，可展开技术详情查看原因码")
            elif key == "matched":
                value = "一致" if value else "不一致"
            elif key == "allowed_models":
                value = "、".join(str(item) for item in value) if value else "未限制"
            elif isinstance(value, bool):
                value = "是" if value else "否"
            elif isinstance(value, (dict, list)):
                value = "已记录变更，详见技术详情"
            elif value is None:
                value = "未设置"
            result.append({"label": DETAIL_LABELS[key], "value": str(value)})

        for key, value in data.items():
            add(key, value)
        return result

    output = []
    for row in rows:
        action_label, kind = ACTIONS.get(row.action, ("其他操作（详见技术详情）", "resource"))
        actor = users.get(row.user_id, {"name": "历史用户（名称不可用）", "email": ""}) if row.user_id else {
            "name": "系统自动执行", "email": "",
        }
        resource_name = name(kind, row.tenant_id, row.resource_id) if row.resource_id else KINDS[kind]
        if kind == "account":
            resource_name = actor["name"]
        if kind == "refinement":
            resource_name = "业务字段绑定"
        resource = resources.get((kind, row.tenant_id, row.resource_id), {})
        workspace_id = row.workspace_id or resource.get("workspace_id")
        workspace_name = name("workspace", row.tenant_id, workspace_id) if workspace_id else ""
        output.append({
            "id": row.id, "created_at": utc(row.created_at).isoformat(), "tenant_id": row.tenant_id,
            "user_id": row.user_id, "workspace_id": row.workspace_id, "action": row.action,
            "resource_id": row.resource_id, "details": row.details,
            "display": {
                "tenant": tenant_names.get(row.tenant_id, "历史租户（名称不可用）"),
                "action": action_label, "actor": actor,
                "resource": {"type": KINDS[kind], "name": resource_name,
                             "workspace": workspace_name if kind != "workspace" else ""},
                "details": detail_items(row, resource_name),
            },
        })
    return output
