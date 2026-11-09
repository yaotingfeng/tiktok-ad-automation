# 经营比例按时间段配置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 用户按明确时间段管理版权方抽成及返点，能预览覆盖、编辑及结束，历史核算可追溯。

**Architecture:** 用不可变 BusinessRateSchedule 日程快照替代旧费率选择；periods 覆盖首次触及日期到无限期，rate=null 明确待配置，之前默认0%。预览与提交复用纯时间轴变换，任务冻结完整日程。

**Tech Stack:** Python / SQLModel / PostgreSQL / Alembic；React / shadcn/ui / generated OpenAPI client / Playwright。

**Spec:** docs/superpowers/specs/2026-11-09-business-rate-periods-design.md

## Global Constraints

- tenant_id + kind + scope_id 隔离；权限沿用租户管理员；不改线上配置、不推送、不发布。
- 截止日期包含当天；账户当地业务日；明确0%与待配置不同。
- 旧记录只用于修改历史，迁移保留旧业务日费率；无旧选择器回退。
- 保留已有4个未跟踪前端生成文件。依项目仓库约束在现有main完成聚焦提交，不新建分支、不切换工作区。

## Review Focus

- 修改开始日期后空档不能恢复默认0%；Task 1 测移动后待配置。
- 结束并接续不能隐式覆盖未来安排；Task 3 测默认截止与显式持续覆盖。
- 并发保存不能覆盖已预览后的新配置；Task 2 真实PG双连接测试409。
- 缺失比例不能被汇总或情景核算按0处理；Task 2 测各金额/导出缺失传播。
- 迁移同日修订及乱序日期必须保持旧结果；Task 2 迁移测试。

### Task 1: 时间轴变换与明确空档

**Files:** 新建 backend/app/modules/business/rate_periods.py；测试 backend/tests/modules/business/test_rate_periods.py。

**Interfaces:** RatePeriod(start_date,end_date,rate), RateOperation(action,start_date,end_date,rate,target_start_date,successor_rate,successor_end_date,note)；transform_periods(periods, operation) -> list[RatePeriod]；period_rate(periods, day) -> Decimal|None。

- [x] 写覆盖、切分、编辑移动、结束/接续、0%、首尾及非法范围测试，运行确认失败。
- [x] 实现单一纯函数变换，填充显式空档、合并相邻同值，日期最大值安全处理。
- [x] 定向测试通过。

### Task 2: 日程存储、迁移、API与冻结核算

**Files:** business/{models,schemas,configuration,api,source,projection,publication,calculation}.py；新迁移；tests/modules/business/{test_configuration,test_api,test_projection,test_rate_schedules}.py。

**Interfaces:** /rates GET 返回 revision+periods；/rates/preview 与 POST /rates 接受同一 RateInput(expected_revision+operation)；/rates/history 分页只读；rate_on使用最新日程；rate_snapshot冻结到BusinessRun.inputs.rate_schedules。

- [x] 先写API预览不落库、提交修订冲突、租户/BC隔离、缺失传播及迁移等价测试并运行失败。
- [x] 新建快照模型与迁移；保留旧BusinessRate只读历史；锁父对象并核对expected_revision。
- [x] 统一解析器供辅助函数与投影使用；冻结快照进入source_signature和cohort_signature，提升计算合同版本并推进已有scope。
- [x] 更新原调用方/测试，补真实PG并发、迁移往返及既有经营套件；Ruff和类型检查。

### Task 3: 配置页面、预览与修改历史

**Files:** frontend/src/features/business/{RateSettings,RatePeriodTable,RateHistory,queries}.tsx/ts（按需要拆分）；生成客户端；frontend/tests/business-dashboard.spec.ts。

**Interfaces:** 复用Task 2接口；列表含待配置；表单支持新增/编辑/结束及接续范围；提交前只读预览，409保留表单并重新加载日程；历史分页。

- [x] 写真实页面操作测试并观察失败：区间预览、取消、编辑、截止后待配置、接续保留未来配置、并发刷新、管理员边界。
- [x] 生成客户端，使用现有shadcn组件；表单输入错误明确显示，历史版本不参与当前比例选择。
- [x] 测经营页面、双前端构建、格式/类型；检查页面渲染。

### Task 4: 集成审阅与交付

- [x] 运行相关经营后端回归、迁移往返、页面回归及构建；复用已通过检查，不无故重跑全仓库。
- [x] 按executing-plans要求独立审阅最终差异，修正实质问题并针对性验证。
- [x] 更新实施进度/验收记录，明确迁移及同版本发布边界；检查暂存差异后聚焦提交，不推送/发布。

实施结果：117项经营后端、13项页面、正式/体验版构建及相关静态检查通过；独立审阅问题已修复，详见[验收记录](../../validation/2026-11-09-business-rate-periods.md)。沿用现有main做单次聚焦提交，避免分开发布新费率API、页面和计算合同。未推送、未部署。
