# 素材制作开户与免密跳转 Implementation Plan

> 按用户“做个计划开始开发”的授权，由当前会话直接逐项实施；不重复申请计划执行批准。

**Goal:** 新租户联合开户，并从“素材制作”菜单免密进入素材工具。

**Architecture:** 租户事务保存加密待开户请求和 outbox，control worker 幂等开户；免密链接按租户绑定即时申请、浏览器消费。复用租户权限、凭据加密、任务恢复及现有 shadcn 组件。

**Tech Stack:** FastAPI、SQLModel/PostgreSQL、Alembic、HTTPX、Celery、React、TanStack、shadcn/ui。

**Spec:** [设计](../specs/2026-10-20-material-production-design.md)。

## Global Constraints

- r2 + true；开户和免密都使用 owp_，只为同把开户密钥创建的用户签发。
- 不保存长期原始密码、网页登录 Cookie 或登录链接；不改成片回传。
- 当前 main 原地开发，保留现有未跟踪文件。初始范围仅本地提交；用户后续明确授权推送、生产发布及两个历史租户开户，执行结果见下文。

## Review Focus

- 超时后重放必须保留 body/幂等键，避免重复公司及并发轮换 Key。
- 用户切租户、撤权或停用时不能取得其他租户链接。
- 响应中的外域 URL、重复 token、过期时间、敏感错误不能直传浏览器。
- 进程中断和重复消息必须能恢复，READY 记录不能再次开户。
- 密钥配置缺失或被替换不能静默改用其他凭据。

## Task 1：开户传输与持久记录

Files: `backend/app/modules/material_production/{models,schemas,client,service,tasks,api}.py`；
`backend/app/alembic/versions/20261020_material_production.py`；
`backend/app/core/{config,errors}.py`；API、Celery、Alembic 注册位置。

- [x] 写 HTTP MockTransport 测试：正确请求、owp_ 认证、响应校验、相对链接、脱敏错误、超时。
- [x] 写 PostgreSQL 测试：联合创建事务/幂等、权限、绑定、密码加密与成功删除、失败重试、READY 防重放。
- [x] 实现 `provision_once(session, tenant_id)` 与 `recover_due(session)`，队列仅携带租户/记录标识。
- [x] 在专用本地测试库迁移，运行目标测试及迁移回退/重升级验证。

## Task 2：联合创建租户与免密 API

Files: `backend/app/modules/tenants/{schemas,router}.py`，上述 API 模块。

- [x] 创建租户请求改为 name/username/password/request_id，本地创建专属管理员并排队开户。
- [x] 增加 GET 状态、平台管理员 POST 重试、租户 POST 登录链接接口。
- [x] 测试跨租户、viewer、停用、重放请求变化、恶意上游链接及 Cookie/凭据不外泄。
- [x] 重新生成 OpenAPI 与 TypeScript SDK。

## Task 3：租户界面与菜单

Files: `frontend/src/features/tenants/TenantAdminPage.tsx`、
`frontend/src/components/Sidebar/{Main,navigation}.tsx/ts`、
`frontend/src/features/material-production/*`、`frontend/src/routes/_layout.tsx`。

- [x] 新建租户表单填写显示名、账号、初始密码；重试保留操作 UUID。
- [x] 租户详情展示开户状态、失败原因和管理员重试。
- [x] 菜单点击直接申请链接并跳转，viewer/无租户不显示；不预取链接。
- [x] 浏览器测试创建请求、状态恢复、点击一次签发、权限、错误反馈与无预取。
- [x] 执行前端 build、相关现有浏览器回归。

## Task 4：配置说明与交付

Files: `.env.example`、`docs/integrations/material-production.md`、
`docs/runbooks/deployment.md`、`docs/implementation-progress.md`。

- [x] 写入配置合同、恢复操作、密钥轮换限制和真实联调步骤。
- [x] Ruff/类型检查、后端目标及租户回归、前端测试通过；检查差异与敏感信息。
- [x] 更新计划与进度，明确真实联调未完成项，按路径暂存并本地提交。

## 验收结果（2026-10-20，本地）

- `pytest tests/modules/material_production tests/modules/tenants tests/jobs/test_outbox.py -q --tb=short`：102 通过；HTTP MockTransport + 专用 PostgreSQL 17，双 Worker 真实行锁只发一次开户请求，五次异常停止自动重试。
- `playwright test tests/tenants-accounts.spec.ts tests/workspace-shell.spec.ts --project=workspace --workers=2`：134 通过；覆盖联合开户、只在点击时签发、只读权限、切租户取消、重试不提交父表单。
- Ruff、全应用 `ty check app`、前端 Biome 与 `bun run build` 通过。
- 专用测试库 `0040 → 0039 → 0040` 回退/重升级通过。
- 未配置真实开户密钥，未执行真实开户、免密消费、推送或部署。后续按接入文档配置 `owp_` 并执行授权测试租户联调。

## 后续生产授权与结果

用户后续明确授权提交推送、生产发布和现有 xingyu/junbo 开通，并提供开户密钥及原密码。版本 `3b4f8c5e` 已完成完整生产迁移发布；xingyu 真实开户、绑定保存及菜单免密跳转已通过。junbo 返回已有账号冲突，且不属于当前开户密钥，等待上游处理或用户确定新用户名，不视为已完成。测试分项、恢复证据和真实结果见[生产验收](../../validation/2026-10-20-material-production.md)。
