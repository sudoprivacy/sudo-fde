"""统一契约（规格 §3）。所有对象 extra="forbid"：未知字段即 SCHEMA_VIOLATION。

阶段一实现的能力：interview · recommend · explain · learn，外加 exposure。
规格缺口 C-1（interview.answers）、C-2（exposure 接口）、C-3（form 前缀证据）按
fde-studio-stage1-design.md §4.3 的假设实现，需回写规格。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- 输入：证据 ----------

class Observation(Strict):
    """阶段二使用；阶段一收到也只做结构校验。只允许统计类事实，不含任何值。"""

    target: Literal["source", "asset", "column", "foreign_key"]
    locator: dict[str, str | None]
    facts: dict[str, int | float | str | bool | None] = Field(default_factory=dict)


class EvidenceRef(Strict):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["user_quote", "doc_snippet", "observation", "runtime_signal"]
    ref: str | None = None
    snapshot_id: str | None = None
    location: Literal["studio", "box"] = "studio"
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    text: str | None = None
    observation: Observation | None = None


class AssertionRef(Strict):
    id: str
    version: int = 1                                  # v0.4
    kind: str
    statement: str
    object: dict[str, Any] | None = None              # v0.4：match 需要节点正文
    status: Literal["confirmed", "proposed", "rejected"] | None = None   # v0.4
    reason_code: str | None = None
    reason_text: str | None = None


# ---------- 目录（阶段二，规格 v0.4 §3.5）：每个条目带 evidence_id；取值类字段一律不在 schema 内 ----------

class CatColumn(Strict):
    name: str
    evidence_id: str
    type: str
    nullable: bool | None = None
    has_default: bool | None = None
    comment: str | None = None
    null_rate: float | None = Field(default=None, ge=0, le=1)
    distinct: int | None = Field(default=None, ge=0)
    shape: Literal["date", "datetime", "integer", "decimal", "boolean", "code", "text_short", "text_long",
                   "json", "uuid"] | None = None
    looks_like: Literal["phone", "id_card", "email", "amount", "percent", "status_word"] | None = None
    low_cardinality: bool | None = None
    monotonic: bool | None = None
    date_span_days: int | None = Field(default=None, ge=0)
    sensitive_column: bool = False
    enum_values: list[str] | None = Field(default=None, max_length=20)


class CatForeignKey(Strict):
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    declared: bool = True
    evidence_id: str


class CatAsset(Strict):
    id: str
    evidence_id: str
    source: str
    kind: Literal["table", "view"] = "table"
    schema_: str | None = Field(default=None, alias="schema")
    name: str
    comment: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    stats_method: Literal["exact", "sampled", "none"] = "none"
    primary_key: list[str] = Field(default_factory=list)
    columns: list[CatColumn]
    foreign_keys: list[CatForeignKey] = Field(default_factory=list)
    referenced_by: int = 0

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CatSource(Strict):
    id: str
    kind: Literal["database", "ddl", "mcp"]       # mcp：v0.9，经 MCP Server 读取的结构
    dialect: str | None = None
    name: str
    system_hint: str | None = None
    evidence_id: str


class CatalogSummary(Strict):
    snapshot_id: str
    sources: list[CatSource]
    assets: list[CatAsset]


class ScopeSystem(Strict):
    system_ref: str | None = None          # 服务在 L1 里自己判定，Studio 可不填
    source_id: str
    status: Literal["scanned", "partial", "permission_denied", "not_scanned"]
    coverage: dict[str, int | None] = Field(default_factory=dict)
    stats_available: bool = True
    permission_errors: list[str] = Field(default_factory=list)
    evidence_id: str


class ScanScope(Strict):
    snapshot_id: str | None = None
    systems: list[ScopeSystem] = Field(default_factory=list)
    unmapped_recommended_systems: list[str] = Field(default_factory=list)
    waived_systems: list[str] = Field(default_factory=list)   # v0.5：FDE 标为"不需要接入"的推荐系统（产物 ID）


class Correction(Strict):
    """v1.1：FDE 已应用的修改意见。优先级高于更早的证据，所有生成类能力必须遵守。"""

    evidence_id: str
    understood: str = Field(max_length=300)
    applied_at: str | None = None


class Context(Strict):
    evidence: list[EvidenceRef] = Field(default_factory=list)
    corrections: list[Correction] = Field(default_factory=list, max_length=100)   # v1.1
    confirmed: list[AssertionRef] = Field(default_factory=list)
    proposed: list[AssertionRef] = Field(default_factory=list)    # v0.4：仅 match 使用（K58 E-7）
    rejected: list[AssertionRef] = Field(default_factory=list)
    catalog: CatalogSummary | None = None      # 阶段二
    scan_scope: ScanScope | None = None        # 阶段二


class Limits(Strict):
    max_items: int = Field(default=20, ge=1, le=100)
    min_confidence: float = Field(default=0.3, ge=0, le=1)


# ---------- 按能力判别的参数 ----------

class Answer(Strict):
    """缺口 C-1：用户回答 / 跳过了哪个追问。"""

    question_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    skipped: bool = False


class InterviewParams(Strict):
    max_questions: int | None = Field(default=None, ge=1, le=5)
    answers: list[Answer] = Field(default_factory=list)


class RecommendParams(Strict):
    want: list[Literal["process", "system", "question"]] = Field(
        default_factory=lambda: ["process", "system", "question"])
    samples: int | None = Field(default=None, ge=1, le=7)


class ArtifactRef(Strict):
    artifact_id: str
    version: int = 1


class ExplainParams(Strict):
    target: ArtifactRef


class _RequestBase(Strict):
    call_id: str = Field(min_length=8, max_length=64)
    workspace_id: str = Field(min_length=1, max_length=80)
    context: Context = Field(default_factory=Context)
    limits: Limits = Field(default_factory=Limits)


class InterviewRequest(_RequestBase):
    capability: Literal["interview"] = "interview"
    expected_state_version: int = Field(ge=0)
    params: InterviewParams = Field(default_factory=InterviewParams)


class RecommendRequest(_RequestBase):
    capability: Literal["recommend"] = "recommend"
    expected_state_version: int = Field(ge=0)
    params: RecommendParams = Field(default_factory=RecommendParams)


class MatchParams(Strict):
    layers: list[Literal["L1", "L2", "L3", "L4"]] = Field(default_factory=lambda: ["L1", "L2", "L3", "L4"])
    targets: list[ArtifactRef] | None = None


class MatchRequest(_RequestBase):
    capability: Literal["match"] = "match"
    params: MatchParams = Field(default_factory=MatchParams)


class SummarizeRequest(_RequestBase):
    """v0.7：第一步——读业务资料 / 原话，给出业务概要与第二步要问的挑战问题。不改假设状态。"""

    capability: Literal["summarize"] = "summarize"


class ReportParams(Strict):
    """v0.7：第五步的附加输入。match_digest 是对齐结果的摘要（节点说法 ＋ 覆盖度 ＋ 缺什么），不含任何值。"""

    match_digest: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


class ReportRequest(_RequestBase):
    """v0.7：第五步——综合原话、挑战、数据观察、已确认 / 待确认节点，给出业务理解与建设建议。"""

    capability: Literal["report"] = "report"
    params: ReportParams = Field(default_factory=ReportParams)


class RefineSlot(Strict):
    """v1.0：第 ⑥ 步一个待细化的槽位（问题的所需数据 / 流程步骤 / 对象的关键属性 / 对象 / 关系）。只有说法，没有值。"""

    id: str = Field(min_length=1, max_length=80)
    kind: Literal["need", "step", "prop", "object", "link"]
    label: str = Field(max_length=200)
    parent: str = Field(default="", max_length=300)        # 所属问题 / 流程 / 对象的说法
    statement: str = Field(default="", max_length=600)
    bound: list[str] = Field(default_factory=list, max_length=20)   # 已绑定的 "表.列"


class RefineColumn(Strict):
    name: str = Field(max_length=120)
    type: str | None = Field(default=None, max_length=40)
    pk: bool = False
    fk: str | None = Field(default=None, max_length=200)    # 外键指向 "表.列"


class RefineTable(Strict):
    name: str = Field(max_length=120)
    rows: int | None = None                                  # 行数（统计量，不是取值）
    columns: list[RefineColumn] = Field(max_length=200)


class RefineParams(Strict):
    slots: list[RefineSlot] = Field(min_length=1, max_length=60)
    tables: list[RefineTable] = Field(min_length=1, max_length=120)


class RefineRequest(_RequestBase):
    """v1.0：第 ⑥ 步细化 Agent——为每个槽位建议绑定字段、指标口径，或判为缺口 / 线下。只读结构元数据（K49）。"""

    capability: Literal["refine"] = "refine"
    params: RefineParams


class ReviseTarget(Strict):
    """当前结果的一处：路径 ＋ 文字 ＋ 所属文档 / 卡片的版本（乐观并发）。"""

    path: str = Field(max_length=200)
    text: str = Field(max_length=2000)
    version: int | None = None


class ReviseParams(Strict):
    instruction_evidence_id: str
    mode: Literal["auto", "revise"] = "auto"
    targets: list[ReviseTarget] = Field(max_length=800)
    max_changes: int = Field(default=20, ge=1, le=40)


class ReviseRequest(_RequestBase):
    """v1.1：把一句修改意见变成可预览的修订集（不直接改任何东西）。"""

    capability: Literal["revise"] = "revise"
    params: ReviseParams


class ExplainRequest(_RequestBase):
    capability: Literal["explain"] = "explain"
    params: ExplainParams


class LearnEvent(Strict):
    artifact_id: str
    artifact_version: int = Field(ge=1)
    decision: Literal["accepted", "edited", "rejected"]
    edited_body: dict[str, Any] | None = None
    reason_code: Literal["not_applicable", "wrong_mapping", "already_known", "too_generic", "other"] | None = None
    reason_text: str | None = None
    decided_at: str | None = None


class LearnRequest(Strict):
    capability: Literal["learn"] = "learn"
    call_id: str = Field(min_length=8, max_length=64)
    workspace_id: str
    events: list[LearnEvent] = Field(min_length=1, max_length=200)


class ExposureItem(Strict):
    artifact_id: str
    version: int = 1
    shown: bool
    rank: int | None = None
    truncated_by: Literal["min_confidence", "max_items"] | None = None


class ExposureRequest(Strict):
    """缺口 C-2：Studio 回报展示情况。"""

    capability: Literal["exposure"] = "exposure"
    call_id: str = Field(min_length=8, max_length=64)
    workspace_id: str
    items: list[ExposureItem] = Field(min_length=1, max_length=200)


# ---------- 输出 ----------

class Versions(Strict):
    pipeline: str
    prompts: dict[str, str] = Field(default_factory=dict)
    model_alias: str | None = None
    calibration: str | None = None
    snapshot: str | None = None


class Dropped(Strict):
    no_citation: int = 0
    generic: int = 0
    contradiction: int = 0
    schema_: int = Field(default=0, alias="schema")
    rejected_repeat: int = 0
    minority: int = 0

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InterviewQuestion(Strict):
    id: str
    text: str
    why: str
    resolves: str | None = None
    options: list[str] = Field(default_factory=list)
    allow_other: bool = True
    kind: Literal["ask", "request_document"] = "ask"
    dimension: str | None = None   # 首轮必填：五个开场维度之一


class StopDecision(Strict):
    should_stop: bool
    reason: str


class InterviewResult(Strict):
    round: int
    questions: list[InterviewQuestion]
    stop: StopDecision
    hypothesis_version: int


class ProposedAssertion(Strict):
    artifact_id: str
    version: int
    layer: Literal["business", "semantic", "binding"] = "business"
    kind: Literal["process", "system", "question", "business_model", "binding", "gap", "ontology"]   # ontology：v0.8
    subject: str
    predicate: str
    object: dict[str, Any]
    statement: str
    origin: Literal["inferred", "pattern"] = "inferred"
    review_status: Literal["proposed"] = "proposed"
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1)
    pattern_refs: list[str] = Field(default_factory=list)
    explain: str = ""


class AssertionBatch(Strict):
    items: list[ProposedAssertion]


class PlanOut(Strict):
    artifact_id: str
    kind: Literal["answer_plan", "fingerprint"]
    target: ArtifactRef
    body: dict[str, Any]


class NodeSummary(Strict):
    target: ArtifactRef
    kind: str
    statement: str
    coverage: float | None
    gate: Literal["pass", "fail", "undetermined"]
    answerability: Literal["answerable", "partial", "not_answerable", "undetermined"] | None = None
    undetermined_reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)   # 每个指纹条目一行，便于画矩阵


class MatchResult(Strict):
    items: list[ProposedAssertion]
    plans: list[PlanOut]
    labels_ref: dict[str, str]
    node_summary: list[NodeSummary]
    systems: list[dict[str, Any]] = Field(default_factory=list)       # L1 结果
    unclaimed: list[dict[str, Any]] = Field(default_factory=list)


class Explanation(Strict):
    artifact_id: str
    target: ArtifactRef
    text: str
    evidence: list[str]
    template_fallback: bool


class BusinessSummaryOut(Strict):
    artifact_id: str
    version: int
    summary: dict[str, Any]
    challenge_questions: list[InterviewQuestion]


class RefineSuggestionOut(Strict):
    artifact_id: str
    version: int
    slot_id: str
    action: Literal["bind", "gap", "offline"]
    fields: list[str] = Field(default_factory=list)          # "表.列"，都在请求的 tables 里
    metric: dict[str, Any] | None = None                     # {grain: ["表.列"], scope: str}
    why: str = ""
    confidence: float = 0.5


class ReviseChangeOut(Strict):
    artifact_id: str
    version: int
    target: str
    op: Literal["update", "add", "remove"]
    base_version: int | None = None
    before: str | None = None
    after: str | None = None
    why: str = ""
    role: Literal["direct", "ripple"] = "direct"
    evidence: list[str] = Field(default_factory=list)


class ReviseOut(Strict):
    artifact_id: str
    intent: Literal["revise", "material", "answer", "question", "unclear"]
    understood: str = ""
    clarify: dict[str, Any] | None = None
    changes: list[ReviseChangeOut] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    suggest_regenerate: bool = False
    dropped_invalid: int = 0


class RefineOut(Strict):
    suggestions: list[RefineSuggestionOut]
    dropped_invalid: int = 0


class BusinessReportOut(Strict):
    artifact_id: str
    version: int
    report: dict[str, Any]
    ontology_items: list[ProposedAssertion] = Field(default_factory=list)   # v0.8：业务本体逐条成为可裁决的断言
    dropped_uncited: int = 0


class LearnResult(Strict):
    accepted: int
    rejected: list[dict[str, Any]]
    new_versions: list[ArtifactRef]


class ExposureResult(Strict):
    updated: int


class Response(Strict):
    call_id: str
    capability: str
    versions: Versions
    state_version: int | None = None
    result: (InterviewResult | MatchResult | AssertionBatch | Explanation | LearnResult | ExposureResult
             | BusinessSummaryOut | BusinessReportOut | RefineOut | ReviseOut)
    dropped: Dropped = Field(default_factory=Dropped)
    warnings: list[str] = Field(default_factory=list)


class ErrorBody(Strict):
    code: Literal[
        "FORBIDDEN", "UNAUTHENTICATED", "INPUT_SENSITIVE", "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_IN_PROGRESS", "STATE_VERSION_CONFLICT", "CONTEXT_TOO_LARGE",
        "MODEL_UNAVAILABLE", "SCHEMA_VIOLATION", "NOT_FOUND", "CATALOG_INVALID",
    ]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
