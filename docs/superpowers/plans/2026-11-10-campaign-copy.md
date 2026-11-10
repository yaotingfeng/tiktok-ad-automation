# 批量复制 Implementation Plan

> **For agentic workers:** Use executing-plans or subagent-driven-development proportionately; explicit file ownership below. User has approved implementation; no additional layout approval is pending.

**Goal:** 按批准原型实现独立表格复制配置、来源冻结、预览及现有搭建执行接续。

**Architecture:** 新增campaign_copies聚合草稿与持久后台准备；一个源/目标/份次对应独立现有搭建子预览。复制编译入口保持来源实际结构，不经普通策略重建。

**Tech Stack:** Existing FastAPI/SQLModel/PostgreSQL/Redis/Celery; React/TanStack/shadcn/Bun.

**Spec:** `docs/superpowers/specs/2026-11-10-campaign-copy-design.md`

## Global Constraints
- 当前租户和BC隔离；100来源/100目标/1–20份/总计1000系列。
- 沿用当前实际配置与文案；缺失事实明确阻断；素材需目标账户真实映射。
- 不新增手填文案；使用已有AccountPicker、TargetingForm、DraftSchedule。
- 不发送真实广告创建/授权/版权方写请求；不推送、不发布。
- 保留工作区原有未提交文件，不创建/切换分支。

## Review Focus
- 当前目录缺字段时不得用旧冻结值补成当前设置。
- 同剧同账户多来源/多份必须产生独立新系列，不被普通组合唯一键合并。
- 目标素材、身份、小程序和连接代数不能从源账户直接挪用。
- PATCH/preview/submit竞态与重投不能覆盖已冻结意图或重复建广告。
- 取消账户/素材编辑不保存，重新打开回显已选值；只读角色不能发起复制。

### Task 1: 来源及复制预览编译
Files: 新建`backend/app/modules/builds/copy_source.py`、`copying.py`、`copy_compile.py`，按需修改执行编译边界；测试`backend/tests/modules/builds/test_campaign_copy*.py`。
Interfaces: `load_copy_source(session, *, context, bc_id, advertiser_id, campaign_id)->dict`；`create_copy_preview(session, *, context, bc_id, source, configuration, advertiser_id, ordinal, request_id)->UUID`。配置为target_advertiser_ids/copies/budget/target_roas/targeting/delivery_config/material_ids。来源输出name/currency/budget/target_roas/materials/targeting/budget_strategy/bid_strategy及不可变source正文、blockers。
- [x] 先写缺字段、当前覆盖、层级过滤、文案继承与账户隔离测试并确认失败。
- [x] 实现严格来源编译、独立副本冻结及copy执行分支，复用已有场景、素材、路由和执行器。
- [x] 运行聚焦行为测试及普通执行回归。

### Task 2: 聚合草稿/API/后台任务
Files: `backend/app/modules/campaign_copies/{models,schemas,service,api,tasks}.py`、迁移、模型注册、API/Celery注册与control修复入口。
Interfaces: `/tenants/{tenant_id}/campaign-copies` create；`/{draft_id}` get/patch；`/{draft_id}/preview`；`/{draft_id}/submit`。源选择使用已冻结selection_id。返回draft_id/bc_id/revision/status/rows/preview_digest/children/submission_ids/error_code。所有请求使用request_id，patch/preview使用expected_revision，submit再带preview_digest。
- [x] 测试真实PG下的租户BC授权、请求幂等、修订冲突、上限、后台重投与多份独立性。
- [x] 迁移、模型和持久派发；每个后台单元一个子预览；通过既有submit_preview提交子任务。
- [x] 实际运行PG/Redis聚焦测试；迁移图与任务注册检查。

### Task 3: 独立配置页与原有组件接入
Files: `frontend/src/features/campaign-copies/*`，新路由；AdsWorkspace/AdsTableToolbar入口；演示处理器与端到端测试。
- [x] API类型使用手写有限合同，沿用现有请求客户端认证；生成客户端由根协调统一处理。
- [x] 表格行内数值、原组件账户多选/定向/排期、素材侧栏，保存/取消/回显；不手填文案。
- [x] 源阻断、预览等待、来源修订失效、幂等提交和子任务跳转。
- [x] 运行浏览器功能测试、TypeScript、正式/演示构建。

### Task 4: 集成、复核与本地提交
- [x] 审查完整范围与迁移、权限及恢复合同，修复重要问题。
- [x] 记录已运行检查与未运行真实联调，更新实施进度；只暂存本轮明确路径，聚焦本地提交。

## 完成记录

本轮86项后端、11项新增页面、正式/体验版构建通过；扩展旧页面检查的基线失败与独立复核修正见`docs/validation/2026-11-10-campaign-copy.md`。本地提交，未推送/发布。
