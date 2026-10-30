# Sudo FDE SaaS

将业务资料、访谈、数据结构和专业知识整理成可确认、可追溯的业务模型。平台提供组织租户、私有/共享工作区、授权知识库、异步 Agent、统一 SudoRouter 模型出口、用量投影与业务交付包。

总体开发方案见 [docs/development-plan.md](docs/development-plan.md)，生产产品形态见 [docs/product-design.md](docs/product-design.md)，原始需求保存在 [docs/requirements.md](docs/requirements.md)。真实 Router 接入按版本化合同完成，合同见 [docs/router-contract.md](docs/router-contract.md)。

## 本机验证

```bash
uv sync --extra dev
uv run fde dev
```

打开 `http://127.0.0.1:8800`。初始化管理员的随机密码保存在 `.data/admin-credentials.txt`（仅本机用户可读）。平台管理员通过 `/admin` 创建组织，再关联用户；新成员通过组织管理员邀请加入。登录页不提供公开注册或创建组织入口。`Ctrl+C` 停止本机服务。原项目的 8700/8701 服务不受影响。

本机启动器启动 BFF 8800、私网 Pattern 8801、Router 适配服务 8802、worker 和维护进程，自动初始化本地数据库。验证环境使用固定的替代模型响应；生产部署使用实际 Router 和模型提供商。可以用本机 Router 绑定码完成组织配置。

试用顺序：新建工作区 → 保存业务资料 → 业务概要/访谈追问 → 上传 SQL DDL 或扫描目录 → 业务推荐/数据对齐 → 接受、修改或驳回卡片 → 业务理解 → 细化字段 → 生成交付包。专业文档在知识库上传，索引完成后按工作区权限参与检索和生成。

生产入口始终是普通业务端 `/`；平台管理员使用独立的 `/admin` 控制台。个人模型在「我的模型」配置，填写提供商地址和 API Key 后可以读取可用模型目录并在选择弹窗中勾选，工作区任务可以选择组织 Router 模型或个人提供商模型。

## 已实现

- 邮箱密码登录、Argon2、服务端会话、CSRF、邀请加入、重置密码、限流、组织切换；组织仅由平台管理员创建。
- 组织 owner/admin/member/billing_viewer；工作区 owner/editor/viewer；即时撤销、归档与取消归档。
- 全平台组织名称唯一；普通账号仅访问所属组织；组织管理员管理本组织，平台管理员跨租户管理。
- PostgreSQL + Alembic + FORCE RLS，租户复合外键，裁决/审计/细化历史只追加；SQLite 限开发与测试。
- 原 Pattern 的 summarize/interview/recommend/match/report/revise/refine/learn/explain 流水线，私网 JWT 身份与持久化任务。
- Router 目录/余额/预占/执行/结算/释放/回执合同，稳定幂等子调用、故障恢复、周期性对账；账务权威始终属于 Router。
- 租户/工作区知识库、授权、文档版本与保留期、格式/敏感检查、异步切片与向量化、受控 RAG、页码/段落引用。
- 引用来源授权继承到生成结果与导出；撤销后不能通过旧片段、共享工作区或下载链接绕过。
- 业务裁决、修订预览/选择/应用/撤销、目录约束的字段细化、版本冲突检测。
- YAML、OWL（Turtle/RDF XML）、BPMN、细化 YAML/Markdown、R2RML 和带内容哈希的 ZIP 交付包。
- 中文浏览器工作台、知识库、组织成员、模型/用量、审计与运营指标。
- 容器镜像、开发 Compose、生产服务模板、备份/恢复工具、自动化测试与 CI。

## 测试

```bash
uv run pytest -q
sh scripts/test-postgres.sh
```

第二条命令创建独立的临时 PostgreSQL 容器，用 `NOSUPERUSER NOBYPASSRLS` 应用账号验证 RLS，并在结束后清理该测试容器。不会连接或清空生产数据库。已有独立迁移后的测试库也可通过 `FDE_TEST_DATABASE_URL` 指定。

```bash
uv run ruff check src/fde_server tests migrations
node --check src/fde_server/static/app.js
docker compose config --quiet
docker build -t sudo-fde-server:local .
```

检查命令的路径仅包含本项目新增代码；复制的原流水线和扫描器保留上游风格。测试报告与本轮验证情况见 [docs/test-report.md](docs/test-report.md)。

## 容器与生产

本机组合：`docker compose up --build`。生产使用独立的 `.env.production`、已测试的镜像摘要和 `compose.production.yaml`，详见 [docs/operations.md](docs/operations.md)。

生产会拒绝默认密钥、SQLite、模拟 Router、开发 embedding 和未启用 RLS 的数据库。必须配置真实的 Router 合同、对象存储、SMTP、内部 embedding 或 Router embedding，以及网络隔离；当前单机 HTTP 配置显式关闭病毒扫描；本仓库不会自动上线、充值或迁移原试用数据。

本次更新的数据库版本为 `0006`，升级已有 PostgreSQL 实例前先备份，再通过迁移角色执行 `alembic upgrade head`。迁移会规范重名组织并记录调整，成员、工作区与审计历史保留。

## 代码导航

| 目录 | 内容 |
|---|---|
| `src/fde_server/` | BFF、身份/权限、任务、Router、知识库、导出、后台维护 |
| `src/fde_server/static/` | 浏览器应用，无第三方 CDN 运行时依赖 |
| `src/ps/` | 复用的 Pattern 流水线、模型协议、提示词与语义词表 |
| `src/fdescan/` | 离线扫描器与元数据目录校验/转换 |
| `migrations/` | PostgreSQL 迁移、RLS、跨租户约束与不可变记录 |
| `tests/` | 隔离、鉴权、RAG、账务故障恢复、业务流程、导出和扫描回归 |
| `deploy/`、`scripts/` | 数据库权限初始化、TLS 反代、数据库验收与运维工具 |

上游来源及初始文件哈希在 `docs/upstream-manifest.json`。本项目没有运行时依赖原开发机器的绝对路径。复用内容的 SaaS 适配包括表名前缀、租户复合约束、移除直接 Provider/HMAC 入口、结构化引用的敏感检查、重试期间稳定的中间对象身份，以及扫描器不再输出完整本机文件路径作为数据源名称。
