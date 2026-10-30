# FDE SaaS 化开发需求（评审草案）

> 版本：v0.1-draft  
> 日期：2026-10-29  
> 状态：**Proposed / 待产品、SudoRouter 与部署负责人确认**  
> 性质：面向同事评审与后续拆分的开发需求，不是任务卡，不构成上线、迁移、扣费或外部接口改造授权。

---

## 1. 背景与结论

当前 FDE Studio + FDE Pattern Service 已能在浏览器中完成业务资料收集、追问、数据结构对齐、推荐、人工裁决和结果导出；但其定位仍是本地/单部署试用原型，不能直接作为多人 SaaS 服务上线。

本需求将其升级为：

> **组织租户内、按用户登录和工作区协作、只通过 SudoRouter 消耗模型额度、可引用受权限控制专业资料的 FDE SaaS Agent 产品。**

首期**不做模型微调**。建议优先交付多租户、用户级工作区、SudoRouter 统一模型出口及计费、知识库 RAG；模型微调在积累了授权数据、评测集和治理能力后再决定。

```text
浏览器
  │
  ▼
FDE Studio SaaS
  ├─ 用户、组织、工作区、知识库、权限、导出
  ├─ 业务证据、人工裁决、审计记录
  │
  ├─ 内网调用
  ▼
FDE Pattern Service
  ├─ 业务理解 Agent 流水线
  ├─ 受控知识上下文
  └─ 用量归因与调用日志
  │
  ▼
SudoRouter（当前主要对应 new-api）
  ├─ 唯一模型 Provider 出口
  ├─ 充值、余额、扣费、账务
  └─ 路由到允许的模型
```

---

## 2. 范围

### 2.1 本期目标

1. 将现有浏览器工作台升级为面向客户组织的 B/S SaaS 服务；
2. 实现 Tenant/组织、用户、成员角色和工作区级授权；
3. 强制所有生产模型调用仅通过 SudoRouter；
4. 以 SudoRouter 的充值余额和扣费流水作为模型消耗的权威来源；
5. 实现租户级和工作区级知识库，采用 RAG 提升垂类沟通和问题生成质量；
6. 保留现有 FDE 的业务理解、追问、对齐、报告、修订与细化能力。

### 2.2 明确不在首期范围

- 用户自行填写第三方 Provider URL、API Key 或模型密钥；
- 自建第二套充值余额、扣费或退款账本；
- 直接将客户资料用于模型微调；
- 无授权的跨租户/跨工作区资料共享；
- SaaS 场景将 `restricted` 级资料发送给外部模型或外部 Embedding 服务；
- 未经过数据源安全评审的在线直连数据库/MCP 能力默认开放给公网 SaaS 用户。

---

## 3. 当前实现基线与差距

以下状态来自当前代码检查；`Confirmed-Code` 表示代码已存在，不代表已部署到生产。

| 能力 | 当前状态 | 结论/差距 |
|---|---|---|
| 浏览器工作台 | **Confirmed-Code**：FastAPI + Jinja/HTMX，已有登录页 | 可复用，但仍是试用版 |
| 用户登录 | **Confirmed-Code**：用户邮箱/密码、Argon2 校验 | 缺注册、邀请、重置、组织成员和生产会话治理 |
| 多租户 | **Confirmed-Code（部分）**：Pattern Service 私有记录带 `tenant_id` | Studio 为单实例配置租户，不是真正多租户 |
| 用户工作区 | **Confirmed-Code（部分）**：工作区有 `created_by` | 读取和列表未按用户/成员过滤，需要权限改造 |
| Provider 管理 | **Confirmed-Code**：当前模型配置只在服务端环境变量 | 可被指向任意 OpenAI-compatible 地址，尚未强制 SudoRouter |
| Router 充值/扣费 | **Unknown**：当前工作区无 Router 对接 | 依赖冻结 Router 认证、余额、预占、结算契约 |
| 文件资料 | **Confirmed-Code（基础）**：支持 txt/md/pdf/docx 提取、切片 | 仅作当前工作区证据，无可复用知识库/RAG |
| 模型微调 | **Confirmed-Code：未实现** | 不建议作为首期能力 |

相关现有实现：

- Studio 单部署租户配置：[`studio/config.py`](../fde-studio/studio/config.py)
- Studio 用户、工作区和本地数据模型：[`studio/db/__init__.py`](../fde-studio/studio/db/__init__.py)
- Studio 当前工作区逻辑：[`studio/domain/__init__.py`](../fde-studio/studio/domain/__init__.py)
- Pattern Service 身份校验：[`ps/core/auth.py`](../pattern-service/ps/core/auth.py)
- Pattern Service 租户存储：[`ps/db.py`](../pattern-service/ps/db.py)
- 当前可配置 OpenAI-compatible 调用器：[`ps/core/llm.py`](../pattern-service/ps/core/llm.py)
- 当前文件解析/切片能力：[`studio/files/__init__.py`](../fde-studio/studio/files/__init__.py)

---

## 4. 业务对象与权限模型

### 4.1 核心定义

| 对象 | 定义 |
|---|---|
| **Tenant / 租户** | 一个付费客户组织；是数据隔离、SudoRouter 计费归属和管理边界。 |
| **Organization / 组织** | 首期与 Tenant 一对一，避免不必要的层级复杂度。 |
| **User / 用户** | 可登录的自然人；未来可属于多个 Tenant。 |
| **Membership / 成员关系** | 用户在 Tenant 中的角色、状态与权限。 |
| **Workspace / 工作区** | 一次业务发现、数据对齐与交付的最小协作单元；必属于一个 Tenant。 |
| **Workspace Membership** | 用户被邀请进入某个工作区后的角色。 |
| **Knowledge Base / 知识库** | 有范围、权限、版本、资料来源和生命周期的专业资料集合。 |
| **SudoRouter Account** | 模型充值、余额、扣费和账务权威来源。 |

### 4.2 角色

| 角色 | 主要权限 |
|---|---|
| `tenant_owner` | 管理组织、成员、SudoRouter 绑定、知识库策略和审计 |
| `tenant_admin` | 管理成员、共享知识库和租户内工作区 |
| `fde_member` | 创建个人工作区、使用授权知识库、被邀请进入共享工作区 |
| `workspace_editor` | 编辑工作区、上传资料、调用 Agent、裁决结论 |
| `workspace_viewer` | 查看工作区和已授权导出物 |
| `billing_viewer` | 查看余额、用量与账单，不接触业务正文 |

### 4.3 工作区默认规则

- 新建工作区默认为创建者私有；
- 创建者、Tenant 管理员和被显式邀请成员才可访问；
- 已被移除、停用或离开 Tenant 的用户立即失去访问；
- 工作区的证据、文档、扫描目录、断言、裁决、导出物、知识检索和模型调用均继承工作区权限；
- 不能因为用户猜到或得到 `ws_xxx` 形式的 ID 就访问资源。

服务端对任意 Workspace 请求至少必须验证：

```text
当前登录用户
  ∈ 当前 Tenant 成员
  ∧ 当前用户为 workspace owner / member / tenant admin
  ∧ 工作区未被软删除
```

---

## 5. 功能需求

## FR-1：登录、组织和成员管理

1. 用户可通过浏览器安全登录 FDE Studio；
2. Tenant Owner/Admin 可以邀请用户、设置角色、停用成员；
3. 首期支持邮箱密码登录，预留企业 OIDC/SSO 接口；
4. 用户只能看到自己有成员关系的 Tenant；
5. 用户切换 Tenant 后，界面、工作区、知识库和用量均切换到对应数据边界；
6. 登录、邀请、角色变更、停用、关键导出和模型调用均生成审计事件；
7. 浏览器不持有 Pattern Service 静态服务 Token、共享 HMAC secret 或模型 Provider 密钥。

安全要求：Argon2 密码哈希；`HttpOnly`/`Secure` 会话 Cookie；CSRF 防护；登录和邀请限流；生产环境禁用开发 Token 与默认密钥。

## FR-2：多租户与用户级工作区

### 数据隔离

以下记录必须带可信的 `tenant_id`，并在所有读写路径中校验：

- Tenant、User、Membership、Workspace、Workspace Membership；
- Evidence、Document、Catalog Snapshot、Data Source；
- Assertion、Decision、Understanding、Export；
- Knowledge Base、Document Version、Chunk、Embedding、Grant；
- Pattern Service 的状态、产物、反馈、调用日志；
- 模型用量、Router 对账和审计记录。

### 持久化要求

- 生产采用 PostgreSQL + Alembic；
- 对私有表启用 PostgreSQL Row-Level Security 或同级数据库侧隔离；
- 应用层鉴权与 RLS 双重存在，不能只依赖应用查询条件；
- 业务唯一约束、外键和查询尽可能包含 Tenant 边界；
- SQLite 仅保留本地开发和测试用途。

### 工作区功能

- 创建、改名、软删除、恢复个人工作区；
- 邀请成员、撤销成员、调整 `editor/viewer`；
- 工作区成员和访问记录；
- 只允许授权成员调用 Agent、查看证据、下载导出物；
- 所有工作区导出物按 Tenant/Workspace 边界存放并受下载授权保护。

## FR-3：SudoRouter 为唯一模型 Provider 与计费出口

### 强制规则

1. 用户、Tenant Admin 和浏览器均不可配置第三方 Provider URL/API Key；
2. Pattern Service 生产环境只能经内部 `SudoRouterClient` 调模型；
3. 不能继续将任意 `PS_LLM_BASE_URL` 作为生产模型出口；
4. 如需用户选择模型，候选只能来自 SudoRouter 已授权模型目录；
5. 每次模型调用必须带 Tenant、User、Workspace、能力名与调用 ID；
6. 调用前必须检查余额/额度，余额不足时不得发出模型请求；
7. 调用后记录模型、Token、金额、状态和 Router 请求 ID；
8. 重试、幂等和服务重启不得造成重复扣费。

### 推荐调用顺序

```text
Pattern Service 收到已授权调用
  │
  ├─ 计算该能力预算上限
  ├─ 向 SudoRouter 请求额度预占/授权
  │   └─ 失败：返回 QUOTA_EXHAUSTED，不调用模型
  ├─ 通过 SudoRouter 调用 Chat 或 Embedding
  ├─ 依据实际用量完成结算或释放未用预占
  └─ 写入 FDE 调用/用量投影和审计日志
```

### 账务语义边界

| 对象 | Semantic owner | Authoritative writer / store |
|---|---|---|
| 充值、余额、扣费、退款、账单流水 | SudoRouter | SudoRouter |
| FDE 调用能力、Workspace 归因、限额策略 | FDE SaaS | FDE PostgreSQL |
| 实际 Token/金额 | SudoRouter 回执 | SudoRouter；FDE 仅保存 projection |
| 允许模型策略 | FDE Tenant Policy + Router 模型目录 | FDE 策略，Router 执行能力目录 |

### Router 合同前置条件

当前 Router/new-api 实现不在本工作区，以下能力状态为 **Unknown**，必须在开发前冻结兼容基线：

- 服务身份认证；
- Tenant/User 到 Router Account 的映射；
- 余额查询；
- 额度预占、确认、释放；
- 调用幂等键；
- Token/金额/模型回执字段；
- 余额不足、限流、模型不可用、退款错误码；
- 对账和补偿机制；
- Chat、structured output、embedding 能力声明。

## FR-4：FDE Agent 流程 SaaS 化

继续复用 Pattern Service 当前能力：

- `summarize`：业务概要；
- `interview`：追问与工作假设；
- `recommend`：流程、系统、问题、本体推荐；
- `match`：业务与数据结构对齐；
- `report`：业务理解与建议；
- `revise`：修改意见和修订集；
- `refine`：业务细化建议；
- `learn`：人工接受/修改/驳回反馈。

公开调用路径应调整为：

```text
Browser → Studio/BFF → internal Pattern Service → SudoRouter
```

Studio 完成登录、Tenant、Workspace 权限验证；Pattern Service 仅接收服务间短期签名身份和已授权上下文。不得让浏览器直接调用 Pattern Service。

## FR-5：租户级和工作区级知识库（RAG）

### 为什么先做 RAG

专业资料更适合通过有出处、可撤销、可更新的检索增强方式注入。知识库能随时更新、显示引用、按用户授权；微调无法解决资料版本、权限和引用追溯问题。

### 知识库层级

```text
平台公共知识库（可选）
        ↓
Tenant 知识库（组织管理员维护）
        ↓
Workspace 知识库（项目专属资料）
```

检索时按范围从窄到宽：当前 Workspace → 当前 Tenant 授权资料 → 平台公共资料。只将有限、授权、相关的片段作为可引用证据送给 Pattern Service。

### 建议数据对象

| 对象 | 关键字段 |
|---|---|
| `knowledge_base` | `tenant_id`、scope、名称、描述、owner、状态、允许角色 |
| `knowledge_document` | 所属知识库、原文件、来源、标签、版本、分类、状态 |
| `knowledge_chunk` | 文档版本、顺序、正文、摘要、页码/段落定位、内容哈希 |
| `embedding` | chunk、嵌入模型版本、向量、索引状态、创建时间 |
| `knowledge_grant` | User/Role/Workspace 对知识库的访问授权 |
| `ingestion_job` | 解析、敏感检查、切片、嵌入、索引及失败状态 |
| `retrieval_event` | 用户、工作区、能力、查询摘要/哈希、命中片段、时间 |

### 知识库流程

1. 上传资料；
2. 病毒/格式校验、文本提取、敏感检测；
3. 异步切片、向量化、索引；
4. 用户在获授权的 Workspace 中发起 FDE 调用；
5. 系统按用户/Tenant/Workspace 权限过滤后检索；
6. 将有限命中片段及来源定位作为证据注入 Pattern Service；
7. 输出展示可回链的资料引用；
8. 删除/撤销文档后，后续检索不得命中该文档。

### 安全规则

- 检索资料是外部数据，不是系统指令；模型提示词必须明确隔离其中的命令文本；
- SaaS 首期不允许 `restricted` 内容进入外部模型或外部嵌入服务；
- 资料范围、标签、版本、保留期限和删除状态必须在检索前过滤；
- 不允许把整个知识库无选择地塞进模型上下文；
- 如“只能使用 SudoRouter”也涵盖 Embedding，优先使用 Router embedding；若 Router 未支持，则使用平台自管内部 embedding 服务，用户不可配置第三方密钥。

---

## 6. 模型微调决策

### 首期不做的理由

微调需要的不只是行业文档，还需要：

- 明确授权、可删除的训练数据；
- 高质量标注的输入/输出样本；
- 垂类问题生成、追问质量和错误率的评测集；
- 脱敏、数据保留、训练退出、模型版本和回滚机制；
- 训练/部署/隔离/计费能力；
- 防止某个 Tenant 数据影响其他 Tenant 的治理体系。

当前 `learn` 事件是人工接受/修改/驳回反馈，适合分析和后续校准，但不是可直接训练的高质量数据集。

### 推荐演进路线

| 阶段 | 做法 | 目标 |
|---|---|---|
| v1.0 | 租户/工作区知识库 RAG | 让 Agent 使用可引用专业资料沟通和提问 |
| v1.1 | 行业提示词模板、词表、问题模板、评测集 | 提升专业表达和追问质量 |
| v1.2 | 离线分析人工裁决和知识库效果 | 判断领域差异和收益 |
| v2.0+ | 经授权、评测、隔离的受控微调 | 仅在明确收益成立后实施 |

---

## 7. 非功能与安全要求

### 数据与部署

- 生产使用 PostgreSQL、对象存储、迁移工具、备份恢复和密钥管理；
- 原始文件、扫描目录、导出包和向量索引按 Tenant/Workspace 分区；
- 对象下载使用短期签名 URL 或授权代理；
- 禁止租户混用队列、日志、缓存和向量命名空间；
- 对现有 SQLite 试用数据提供受控迁移或明确不迁移策略。

### 费用与可靠性

- 每个调用具有唯一 `call_id`；
- 为不同能力设置并发、上下文、生成 Token 和预算上限；
- 余额不足、Router 故障、结算失败、模型超时均返回可理解、可恢复的状态；
- 定期对账 FDE 用量 projection 与 Router 权威账单；
- 不允许重试导致重复收费。

### 审计与观测

至少提供：

- Tenant 维度：活跃用户、工作区、调用量、Token、金额、错误率；
- Workspace 维度：资料上传、检索、生成、匹配、报告、导出；
- Router 维度：余额校验、预占、结算、失败、对账差异；
- 知识库维度：解析、索引、失败、命中和引用率；
- 安全维度：认证失败、跨租户拒绝、敏感输入拦截、权限变更。

---

## 8. 分期与依赖

### Phase 0：合同和架构冻结

1. 冻结 Tenant/User/Workspace/Knowledge Base/Usage 的语义；
2. 冻结 SudoRouter 认证、余额、预占、扣费、幂等和模型目录合同；
3. 确定 PostgreSQL、对象存储、队列、向量索引和密钥管理方案；
4. 明确 SaaS 与未来 Private/Box 的数据分类边界；
5. 定义跨服务身份、错误码、审计字段和可观测性事件。

### Phase 1：Studio 身份、租户和工作区隔离

主要影响 `fde-studio`：

- 组织、成员、角色、登录会话；
- 用户级/共享工作区；
- 所有 Studio 记录添加 Tenant 边界；
- PostgreSQL + Alembic + RLS；
- 管理后台与基础审计；
- 移除仅凭 Workspace ID 访问资源的路径。

### Phase 2：Pattern Service 身份与 SudoRouter 接入

主要影响 `pattern-service`，依赖 SudoRouter 合同：

- 服务间短期身份令牌；
- `SudoRouterClient`；
- 额度预占、结算、释放、重试与对账；
- Tenant 级模型策略和预算限制；
- 禁止任意第三方 Provider 配置。

### Phase 3：知识库 RAG

主要由 Studio 持有资料与权限，Pattern Service 消费受控片段：

- Tenant/Workspace Knowledge Base；
- 文档异步解析、切片、索引、版本和删除；
- 检索授权、引用展示、审计；
- embedding 路径及质量评测。

### Phase 4：上线治理

- 监控、告警、备份恢复、用量对账；
- 跨租户权限测试和故障注入；
- 模型质量评测与提示词版本治理；
- 限流、配额和异常运营流程。

---

## 9. 首期验收标准

1. **租户隔离**：Tenant A 的用户用任意已知 ID 访问 Tenant B 的 Workspace、Document、Export 或 Knowledge Base，均返回 `403/404`，且无正文泄露。
2. **用户级工作区**：新工作区默认仅创建者可见；编辑者能修改；查看者仅可读；移除成员后立即失去访问。
3. **唯一模型出口**：浏览器、Tenant 管理页和生产配置均无第三方 Provider/API Key 输入；生产调用日志均有 SudoRouter 请求 ID。
4. **余额控制**：余额不足时，模型调用之前返回明确额度不足错误，Router 没有收到模型请求，也无 Token 扣费。
5. **幂等扣费**：网络重试、客户端重放或服务重启后，同一调用 ID 只结算一次；FDE 和 Router 可以按调用 ID 对账。
6. **知识库授权**：用户只能检索有权资料；生成结果可显示文件/页码/段落引用；撤销文档后新检索不再命中。
7. **核心流程回归**：完成“资料 → 追问 → 数据对齐 → 推荐 → 人工裁决 → 报告 → YAML/OWL/BPMN 导出”的端到端流程。

---

## 10. 待评审决策

1. Tenant 是否严格等同于付费客户组织；首期推荐“是”；
2. 个人工作区是否允许后续邀请共享；首期推荐“允许，但默认私有”；
3. SudoRouter 是否已提供余额预占/结算/embedding 合同；当前 **Unknown**；
4. SaaS 对敏感资料的策略：首期推荐“拒绝外送，未来走 Private/Box”；
5. 平台公共知识库是否首期开放；首期推荐“数据模型预留，运营能力后置”；
6. 知识库 embedding 是由 Router 提供还是平台自管；依赖 Router 能力基线；
7. 微调何时启动：推荐以明确评测收益、客户授权和数据治理为门槛，而非日期驱动。

---

## 11. 最终建议

> 先将 FDE 建成一个“组织隔离、用户工作区、SudoRouter 统一计费、知识可引用”的 SaaS Agent 产品；在真实用户反馈、评测和治理条件成熟后，再评估受控模型微调。
