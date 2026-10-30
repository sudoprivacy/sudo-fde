# 开发与测试交付记录

2026-10-30 的真实个人模型全流程复测、修复清单与交付物保存在本机 `docs/acceptance/2026-10-30-product/`。该目录包含单次运行的业务产出与资源标识，默认不提交到 Git；下文保留可随源码共享的首次交付记录。

日期：2026-10-29。目标目录：`sudo-fde-server`。本记录对应本次交付代码；没有执行生产上线、原项目数据迁移或真实模型扣费。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 应用、业务流程、权限、知识库、账务故障、平台管理与个人模型回归 | **70 passed，2 skipped** |
| 独立 PostgreSQL 16 迁移与实际运行账号隔离验收 | **1 passed**；运行账号无 superuser / BYPASSRLS |
| SaaS 应用代码行覆盖率 | **77%**（`fde_server`：约 2502 / 3252 行；不包含上游 Pattern/扫描器的覆盖率统计） |
| Ruff、JavaScript 语法、Compose 配置 | 通过 |
| Docker 镜像构建与安装后模块/提示词资源检查 | 通过 |
| 完整 Compose 跨进程 HTTP 链路 | 通过：BFF → worker → JWT 内部 Pattern → Router 模拟器 |
| Compose 知识入库、RAG 引用、任务重放、导出 | 通过 |
| Compose 用量对账 | **9 条回执核对一致，0 差异** |
| PostgreSQL 备份与隔离恢复 | 通过；恢复为 schema `0004`，关键私有表仍启用 FORCE RLS |
| 浏览器交互 | 已实际验证登录、新建工作区、保存业务资料、异步任务状态与概要展示 |

两项跳过分别是：默认测试进程中未启用的 PostgreSQL 测试（已用独立容器单独运行通过），以及真实 Router 外部验收（按用户确认后续接入）。测试工具还有一项 Starlette TestClient 的第三方弃用提示，不影响断言结果。

机器可读记录保存在本机 `docs/acceptance/summary.json`、`docs/acceptance/compose.json`。原始 JUnit 与覆盖率 JSON 在本机 `test-results/`；这些运行产物不作为源码提交内容。

新增平台管理、用户模型提供商、自动概要启动与生产工作流状态的回归覆盖，平台管理页面已在浏览器中实测。

## 重点覆盖

- 已知资源 ID 跨租户访问拒绝；工作区默认私有；viewer/editor/admin 边界；移除和停用立即生效。
- CSRF、Origin 校验、登录限流、密码重置一次性使用及会话撤销；组织切换后的资源边界。
- PostgreSQL 查询故意省略 tenant 条件时仍隔离；跨租户写入及复合外键失败；连接池复用不遗留租户上下文；审计删除被数据库拒绝。
- 余额不足时零模型执行；相同调用重放、并发重复提交、执行响应丢失、结算失败、运行对象重建和维护补偿。
- 独立推荐采样分别计量；多阶段访谈重试保留中间身份，不重复执行后续模型步骤；新部署拒绝使用不同流程版本接续旧任务。
- Router 文档 embedding、查询 embedding 与业务生成全部产生归因和回执，片段记录实际嵌入模型身份。
- 知识库 User/Role/Workspace 授权；跨工作区隔离；文档版本切换、撤销与引用读取；共享结果和导出不能绕过资料来源权限。
- restricted/敏感内容在模型与 embedding 之前拒绝；恶意文件、ClamAV 故障、对象存储路径与私有加密请求。
- 资料 → 概要 → 追问 → DDL → 推荐 → 匹配 → 报告 → 裁决 → 细化 → YAML/OWL/BPMN/ZIP 的完整回归。
- 修订选择、应用、撤销、过期版本拒绝；绑定字段必须来自目录；粒度和线下标记的约束。

## 验证中发现并修复的问题

1. 文档引用中的随机数字 ID 会触发原敏感规则，导致合法 RAG 请求被误拒。现在只豁免服务端构造的结构定位符，正文仍逐条检查，并有固定数字 ID 回归用例。
2. 扫描器将完整本机文件路径作为源名称，既可能超过目录 schema 限制，也带出机器路径。现在使用文件名和不包含凭据的稳定地址摘要。
3. 多阶段访谈在失败回滚后重新生成中间 ID，使第二阶段被 Router 识别成新调用。故障测试曾复现两步访谈发生三次模型执行；修复后稳定为两次执行、两次结算。
4. 共享工作区可能通过生成结果中的缓存引用或导出包绕过知识库授权。现在结果、片段和下载均重新检查来源权限，撤销后不会通过这些路径继续暴露正文。
5. Docker 环境对绑定挂载的初始化脚本存在执行限制；数据库初始化已改为 SQL 文件。开发 Compose 为 Web 增加单独入口网络，内部服务保持不发布端口。

## 重现方式

```bash
uv sync --frozen --extra dev
uv run pytest -q --cov=fde_server
sh scripts/test-postgres.sh
docker build -t sudo-fde-server:local .
docker compose -p sudo-fde-acceptance -f compose.yaml -f deploy/compose.acceptance.yaml up -d --no-build
docker compose -p sudo-fde-acceptance -f compose.yaml -f deploy/compose.acceptance.yaml --profile seed run --rm demo
mkdir -p test-results
docker compose -p sudo-fde-acceptance -f compose.yaml -f deploy/compose.acceptance.yaml cp web:/app/.data/admin-credentials.txt test-results/acceptance-admin-credentials.txt
FDE_ACCEPTANCE_CREDENTIALS_FILE=test-results/acceptance-admin-credentials.txt uv run python scripts/accept-stack.py
```

容器验收固定访问本机 `18800`，使用独立项目与合成资料；不会自动转向生产 URL。测试通过该独立环境的管理员账号创建组织，不依赖公开注册。`test-results/` 中的验收凭据文件不提交到 Git，使用后应清理。验收后的专属容器和测试卷可用相同 Compose 项目名执行 `down --volumes` 清理。

## 外部验收边界

真实 SudoRouter 接入已按用户确认延期。本次没有把模拟回执、模拟余额或模拟输出当作真实计费与模型质量结果。生产 S3、SMTP、ClamAV、内部 embedding、TLS/网络策略、代表性负载及真实模型质量仍须在部署环境完成验收；对应接口、配置、失败处理与运行手册已提供。企业 SSO 激活、平台公共知识运营、微调和旧 SQLite 数据导入属于文档明确保留的后续范围。
