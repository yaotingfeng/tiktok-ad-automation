# 素材制作内嵌工作区 Implementation Plan

> 当前会话按用户修改请求直接实施，不额外启动子代理或重复申请执行批准。

**Goal:** 投放工具内下单与查进度，保留素材工作台免密按钮。
**Architecture:** 公司 Key 服务端代理；本地持久订单 + 既有 outbox/control 防重复提交；页面实时读取远端进度。
**Tech Stack:** FastAPI / SQLModel / PostgreSQL / Celery / HTTPX / React / TanStack / shadcn。
**Spec:** [设计](../specs/2026-10-21-material-production-workspace.md)。

## Global Constraints

- 保留三种片源的精确合同，素材库每次新建前读取能力。
- owk_ 仅在后端解密，不下发；租户 upload 权限贯穿读写与任务执行。
- startTime、请求体和幂等键在持久订单中冻结；重复/旧消息不能重复生产。
- 远端轮询 30 秒，达到 50 条提示截断；逐剧终态判断，失败不等于整单结束。
- 既有其他任务改动保留；不发起真实生产订单，无新增业务开关。

## Review Focus

- 请求超时后任务已存在：同订单原请求恢复；服务端保存 job.id，成功不重放。
- 素材库能力变化：全剧模式不传集数，区间模式一致计算数量。
- 跨租户请求和失效成员：本地/Worker/代理均检查授权，不使用另一公司的 Key。
- 一个剧失败另一个仍生产：轮询继续，未知状态不误判完成。
- 上游故障/空策略/无账号/未开户：保留表单、显示可操作说明，不生成不可恢复的重复订单。

## Task 1：任务 HTTP 合同

Files: `backend/app/modules/material_production/job_schemas.py`, `job_client.py`, `backend/tests/modules/material_production/test_jobs.py`。
- [ ] 编写并运行传输合同失败测试：公司 Key、三种 body、能力、错误脱敏、路径约束、逐剧完成。
- [ ] 实现独立任务 client 和严格请求/公开响应 DTO，忽略未知响应字段。
- [ ] 验证 4xx/5xx/超时与无 Cookie/禁止重定向。

## Task 2：可靠提交与 API

Files: 同模块 `models.py`, `orders.py`, `api.py`, `tasks.py`；Alembic 新迁移。
- [ ] 持久本地订单/冻结请求/外部 job.id；复用 outbox/control 和每分钟恢复。
- [ ] 增加策略、账号、能力、订单创建/查询/重试、远端列表/详情接口。
- [ ] PostgreSQL 验证幂等、并发、权限、消息重放、超时恢复和迁移；更新生成 SDK。

## Task 3：工作区页面与导航

Files: `frontend/src/features/material-production/` 新页面/表单/进度组件，租户路由、导航及 layout。
- [ ] 菜单进入内页；原免密 hook 迁到“打开素材工作台”按钮。
- [ ] 创建表单、多策略、条件片源字段、持久提交状态、历史列表和逐剧进度。
- [ ] 浏览器回归创建/轮询/部分失败/切租户/错误与未开通/保留工作台跳转。

## Task 4：验收与记录

- [ ] Ruff/ty/前端构建、模块与租户回归、真实 PostgreSQL/Redis 和浏览器检查。
- [ ] 记录本地与真实环境的明确边界，更新接入文档和进度；按文件聚焦提交。
