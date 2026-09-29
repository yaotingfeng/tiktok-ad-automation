# 外部素材批量推送 Implementation Plan

> **For agentic workers:** Use native execution in this session. 用户已明确授权按会话方案实施，不重复申请方案审批；遵守项目按需使用 Superpowers 的偏好。

**Goal:** 素材工具按租户名称批量推送三个字段，自动完成校验入库和默认 BC 上传。

**Architecture:** 新增批次/外部素材修订及验签入口；流式检验后接入现有 URL 上传队列。外部原件独立标记、加密 URL，复用平台回执与分发，隔离自有存储清理。

**Tech Stack:** FastAPI、SQLModel/PostgreSQL、Alembic、Celery/Redis、urllib3、React/shadcn。

**Spec:** `docs/superpowers/specs/2026-09-29-material-push-design.md`

## Global Constraints

- 原文件名不改写；不复制第二份 R2 对象；不触发真实 TikTok/R2 写入。
- 仅本地实现和验证，不推送或发布；所有变更聚焦提交。
- 租户和 BC 权限贯穿入口、后台和平台操作；未知结果不重新发送。

## Review Focus

- 旧批次重试及晚完成不能覆盖较新素材修订。
- 来源 URL 包含签名，不能出现在错误、日志、任务消息和公开响应。
- 外部原件不能进入删除、multipart 或自有预算路径。
- 默认 BC 改变不能重定向已接收批次。
- 网络失败、进程中断和同名同内容仍须恢复且不重复上传。

## Task 1：持久契约、租户默认 BC 与鉴权

Files: `materials/push_models.py`、`push_schemas.py`、`push_auth.py`、`push_api.py`、`push_service.py`；tenant 模型/服务及迁移、配置、API 注册。

- [x] 测试签名、时间窗、精确租户名、默认 BC、批次事务/幂等及逐项状态查询。
- [x] 实现有界 DTO、持久批次/修订、冻结路由、管理员设置和独立接入配置。
- [x] 在独立测试库运行接口与迁移测试。

## Task 2：外部原件校验、版本和队列

Files: `materials/push_transport.py`、`push_worker.py`、`push_tasks.py`；现有 URL 签名、清理及预览入口。

- [x] 测试地址检查/固定 DNS、大小和视频校验、逐项失败、重复领取、内容复用和旧版本保留。
- [x] 实现 `inspect_external(url, allowed_hosts)` 和 `process_item(database_engine, context, item_id)`，HTTP 不持锁，持久租约和修复扫描。
- [x] 原件以 external 存储类型衔接 source URL 上传，封堵删除/分片/预算路径；测试实际 SDK/MCP 序列化边界。

## Task 3：默认 BC 页面、对接文档与完整验证

Files: `TenantAdminPage.tsx`、`TenantScope.tsx`、生成客户端、`docs/integrations/material-push.md`、配置/部署文档、实施进度。

- [x] 租户编辑可选择和清除默认 BC，初始工作区采用该默认值；验证权限和保存行为。
- [x] 文档提供准确签名算法、重试/更新语义、状态查询与配置样例。
- [x] 运行后端行为/迁移回归、Ruff/ty、前端构建和相关页面测试；复核差异、更新进度并聚焦提交（与本清单同一提交）。

验证范围和既有失败基线见 `docs/validation/2026-09-29-material-push-local.md`。真实接入配置、发布和平台联调不在本次本地实施范围内。
