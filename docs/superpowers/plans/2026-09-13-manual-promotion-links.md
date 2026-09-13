# 手动推广链接 Implementation Plan

**Goal:** 保持现有三步交互，支持其他版权方与手动推广链接。
**Architecture:** 输入行保存手动补充信息，准备时分流到本地链接记录或现有版权方任务；复用素材、Mini、预览和执行。
**Tech Stack:** FastAPI、SQLModel、Alembic、PostgreSQL、React、shadcn/ui、Playwright。
**Spec:** ../specs/2026-09-13-manual-promotion-links-design.md

## 约束

用户已批准本轮实施，当前功能分支直接开发，不创建分支，不推送/发布，不进行真实广告和版权方操作。沿用现有中文注释、视觉规范和租户/BC边界。

## 实施清单

- [x] 数据与准备：先新增 `backend/tests/modules/builds/test_manual_links.py`，覆盖本地版权方、URL、混合任务、缺链、幂等和权限；运行专用 PostgreSQL 测试库确认失败。添加 Alembic 迁移、`builds/manual_links.py`，调整 models/schemas/drafts/catalog/scene/previews 及版权方读写边界；运行新增及原有 builds/providers 回归。
- [x] 逐行补链：使用 `request_id + expected_revision`，补链成功只移除该行旧链接/素材绑定，其他分组保留；通过现有 mutation 回执查询恢复未知响应。测试 stale revision、重复请求及租户隔离。
- [x] 界面：先在 `frontend/tests/build-preparation.spec.ts` 添加行为用例，确认失败；复用 Dialog、Textarea、Field 和侧栏添加批量入口、other 名称、单行补链；草稿恢复包含手动信息。生成 OpenAPI 客户端，构建并运行相关 Playwright 回归。
- [x] 验证交付：检查桌面/窄屏截图、运行 lint/type/build/迁移验证，复查差异与调用链，更新 implementation-progress 及验证记录，明确本地验证范围，聚焦提交本轮文件。

验收细节见 `docs/validation/2026-09-13-manual-promotion-links.md`。聚焦提交：`builds: support other providers and manual promotion links`。
