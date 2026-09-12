# MCP 多 BC 实施计划

> 使用 superpowers:subagent-driven-development 执行。用户已批准设计与实施，不重复申请执行许可。

**目标：** 一次 MCP 授权接入多个 BC，分别同步、重试、解绑和切换，保留已有授权与冻结任务。

**架构：** 复用 TikTokConnection 作为共享授权，BCConnectionBinding 保存独立接入。共享凭据发布与每 BC 目录发布拆开，连接级统一刷新，任务固定 BC 绑定代数。

**技术：** FastAPI / SQLModel / Alembic / PostgreSQL / Redis / Celery / 官方 MCP Python SDK / React / TanStack / shadcn。

**设计：** [已批准设计](../specs/2026-09-12-mcp-multiple-bcs-design.md)。基准 81bbe0f，当前已有 feat/platform-implementation，沿用干净工作区，按文件所有权并行，不自动创建分支。主代理统一模型、迁移、接口、生成客户端、提交及发布。

## 全局约束

- 各代理使用 gpt-6-astra high，不因时间长打断；工作代理不生成子代理。
- 凭据只一份，不跨租户、主体或通道合并；实际广告/素材写能力不因读取成功开放。
- 不修改旧迁移、不删除历史目录或任务。当前 BC 切换不改变已冻结任务路由。
- 每个 BC 独立任务、错误与账户清理；解绑只提高自身代数，普通同步不改共享授权版本和绑定代数。

## Task 1：共享模型、迁移与冻结路由（主代理）

文件：accounts/connection_models.py、models.py、routing.py；contracts/context.py；持久化 BuildRouteContext、SceneJob、CapabilityJob 及其路由序列化；新增 mcp_multi_bc Alembic 迁移及 test_mcp_multibc_migration.py、路由回归。

- [x] 增加绑定 status/authorization_revision/revision/last_error_code，运行 bc_id/authorization_revision/binding_revision；活动索引按 BC 隔离，旧 API 连接级活动索引保留。
- [x] FrozenTikTokRoute 与持久化父路由新增 binding_revision 默认 0；freeze 填当前代数、verify 要求当前有效绑定及相同代数。
- [x] 迁移回填旧授权版本；旧候选未完成 run 标 ERROR，不改已完成记录；验证升降级保护、元数据与索引。
- [x] 检查 material/build/capability 的 route 序列化、历史恢复和每次发送检查，确保代数不丢失。

## Task 2：授权管理与接入服务（lifecycle 代理）

文件：mcp_auth/bootstrap.py、management.py(新增)、service.py；accounts/connections.py；test_mcp_binding.py、test_mcp_multibc_management.py。

接口：`bind_candidate_bcs(*,database_engine,redis_client,context,attempt_id,bc_ids,task_deadline)` 与 `add_connection_bcs(...,connection_id,...)` 返回 `(connection_id, [{bc_id,discovery_run_id,status}])`；`sync_connection_bc(session,*,context,connection_id,bc_id)->UUID`；`disable_connection_bc(...)->None`。

- [x] 先以两 BC 测试验证旧单 BC 限制/共享候选消费缺陷。
- [x] `open_management_accounts(*,database_engine,redis_client,context,connection_id,task_deadline,authorization_revision=None,run_id=None)` 复用当前凭据及统一 refresh，严格只读和逐请求核验。
- [x] `connection_business_centers(...,refresh=False)` 完整读取/缓存当前版本目录；候选列表补真实主体事实，缓存绑定版本及五分钟期限。
- [x] 批量候选接入单次发布凭据/授权版本、幂等重放；每 BC 建 run 和 outbox。追加/同步/解绑复用同授权且影响限定 BC。
- [x] 验证伪 BC、跨租户、失权、陈旧目录、并发重复提交、token 刷新与重新授权。

## Task 3：每 BC 发现和原子发布（discovery 代理）

文件：accounts/mcp_discovery.py、mcp_discovery_tasks.py；test_mcp_directory_publish.py、test_mcp_multibc_discovery.py。

- [x] 先构造共享授权两 BC，一成功一失败，证明旧全连接清理会污染其他 BC。
- [x] 新 run 不依赖候选；通过 Task 2 管理工厂执行原六阶段读取，按共享授权版本及绑定代数核验。
- [x] 发布只改目标 BC，成功使 binding ACTIVE，失败仅目标 ERROR；不改共享凭据/revision，不消费候选。
- [x] FINALIZE/错误回写使用原 claim/revision 围栏；解绑、重新授权、旧消息和重复派发不能产生迟到发布。
- [x] 真 PG/Redis 测试并发、部分失败、token 轮换、任务恢复与目录边界。

## Task 4：HTTP 合同和多选管理界面（主代理 + surface 代理）

主代理：schemas.py、mcp_router.py、router.py、connection_views.py、生成 client；surface 代理：McpAuthorizationSheet.tsx、ConnectionsPage.tsx、presentation.tsx、tenants-accounts.spec.ts。

- [x] 候选 binding 使用 bc_ids；新增 available-bcs、bindings、sync、DELETE 端点。批量输入验证 1..1000、每 ID 1..128 非空、禁止重复。
- [x] 回传每 BC 状态/错误/时间，连接聚合 pending_binding_count；BC 页面过滤已停用绑定，保留本地历史。
- [x] 生成 AccountsService.availableBcs/addBindings/syncMcpBc/unbindMcpBc 和新 binding 类型。
- [x] 单项自动选、多项全选/跨页选择、添加 BC 不 OAuth、每 BC 同步/解绑/默认选择；持续轮询全部进行中任务。
- [x] 前端对应行为测试、TypeScript/Vite 构建，错误提示使用固定脱敏文本。

## Task 5：集成复核、文档、发布与验收（主代理）

- [x] 运行相关后端回归（账户/refresh/routes/material/build），隔离 PG/Redis，不使用业务库。
- [x] 独立代理进行规格与代码审查；修复后做对应复验，不重复无关测试。
- [x] 更新实施进度、部署规范/验收和已批准旧设计的替代关系；提交推送固定版本。
- [x] 按测试手册排空/完整备份/恢复校验、迁移、同版本三个服务发布；Sun Browser 验证原绑定仍可见及当前授权刷新目录，无真实广告写入。

## 执行记录

- 设计批准来源：用户“你按这个方案实施一下”。沿用先前测试服务器提交/推送/部署授权，不涉及生产。
- 三名代理实现及独立交叉审查已完成；主代理整合共享合同、迁移和历史输入保全，并完成相关回归修订。固定版本 `f9d74257` 已推送并发布测试环境；完整备份、恢复校验、迁移和三个服务验收通过。
- Sun Browser 使用原授权刷新目录成功；窗口后续不可读取，最终通过正式 HTTPS 同步与持久回执确认 COMPLETE、42 个有效账户。真实授权仅一个 BC，多 BC 并发由隔离测试覆盖，详见[验收记录](../../validation/2026-09-12-mcp-multiple-bcs.md)。
