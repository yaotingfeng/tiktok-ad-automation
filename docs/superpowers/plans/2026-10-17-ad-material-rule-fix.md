# 广告内素材规则修复 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; implement inline and request one independent final review.

**Goal:** 修复素材规则取数断链，逐条判断广告内素材，仅暂停命中的使用位置，并发布生产。

**Architecture:** 当前目录的广告内视频使用身份是执行目标；`material_post_link` 将素材日报的帖子归到该广告内视频，新旧帖子合并核算，操作始终指向最新完整且唯一的广告内身份。同一主素材归属多广告或无法准确关联时不使用共享合计代替单广告表现，明确跳过；显式 VIDEO 口径继续按视频合计，缺少成员数据时整组合计不执行。

**Tech Stack:** Python/SQLModel/PostgreSQL、React/shadcn、Playwright、现有 Jenkins/ACK 发布流程。

**Spec:** 本轮用户已批准的对话需求（2026-10-17），替代此前素材 EACH 按账户主素材合计的设计。

## Global Constraints

- 所有数据查询保留租户与可读账户/BC 范围；不调用外部广告写接口验收。
- EACH 身份按账户、广告、视频、素材类型稳定生成；执行 ID 重建不扩大到同视频其他广告。
- 不修改部署开关；现有四项实际值沿用 true；完整备份及隔离恢复验证成功后才能切换生产。
- 用户已授权修复、提交、推送及生产发布，直接在现有 main 完成，不创建新分支，不重复请求流程确认。

## Review Focus

- 同视频跨广告一好一差，只命中差的广告内位置。
- 新旧帖子/广告内 ID 重建合并金额但只操作当前唯一身份。
- 共享帖子、未关联、身份歧义和视频合计缺数明确跳过，不能部分合计误杀。
- 指定对象选择器与判断身份一致，租户及账户隔离保留。
- 试运行、历史运行、待确认及飞书能解释跳过原因，0.85 按 Decimal 原值判断。

### Task 1: 素材输入与执行隔离

**Files:** 新增 `backend/app/modules/automation_rules/material_inputs.py`；修改 `inputs.py`、`evaluate.py`、`pickers.py`；测试 `backend/tests/modules/automation_rules/test_material_inputs.py`、`test_inputs.py`、`test_pickers.py`。

**Interfaces:** 输出当前广告内素材 Entity、帖子 ID 对照、缺数原因；`load_inputs` 仍返回 Inputs；不新增数据库字段。

- [x] 先写真实 PostgreSQL 回归：目录主素材为空但关联表完整、广告一差一好、共享帖子不判断、重建只操作新 ID、缺关联跳过、选择器身份一致。
- [x] 运行并观察旧代码失败，再实现按广告内使用加载/核算；缺数原因以现有 `skipped_accounts` JSON 的 reason/count 分账户持久化，页面解释为部分素材未参与而非整个账户跳过。
- [x] 自动规则全模块及广告管理/报表相关回归通过，运行 Ruff/ty。

### Task 2: 页面与通知

**Files:** `frontend/src/features/automation-rules/{ruleForm,RuleEditorSheet,DryRunPanel,RunHistory,Confirmations}.tsx/ts`，`backend/app/modules/automation_rules/runner.py`，相关浏览器测试。

**Interfaces:** EACH 显示“每条广告内的素材”，各处使用同一份跳过原因文案；沿用现有 API 字段，无迁移。

- [x] 浏览器用例先验证新文案及关联缺失原因，再实现文案及共用提示组件。
- [x] 前端构建及规则浏览器用例通过；独立最终代码审查并修复重要问题。

### Task 3: 发布与生产只读验收

**Files:** `docs/implementation-progress.md`、`docs/validation/2026-10-17-ad-material-rule-fix.md`。

- [x] 聚焦提交并推送公司 main；跟踪 Jenkins 完整门禁、备份、隔离恢复、迁移/九角色验收。
- [x] 生产只读试运行用户同样条件，确认检查对象非零、命中目标均为单广告内使用，0.85 保持原值；不保存草稿/发送通知/暂停真实素材。
- [x] 记录版本、检查、备份、开关与业务验收证据并交付。

## 最终审查处理

独立审查提出四项 Important，均补失败回归后修复：素材缺数只将对应目标置未知；VIDEO 选中账户过期不按部分数据合计；规则日期窗口内完全未关联的报表帖子阻止不完整判断；逐次和汇总通知复用原因格式，汇总数量按同账户/原因的单次最大值而非重复累加。

Ruling: 不新增迁移，复用现有 skipped_accounts JSON 的 reason/count 存部分素材问题；所有消费方（反向冲突、逐次/汇总通知、试运行/运行记录/确认页）已同步适配并回归。无旧宽范围行为兼容分支。
