# Sync Followups Implementation Plan

> 按用户明确指定的执行方式：主工作区 main 顺序实施四阶段，不建分支或工作树；无需重复设计审批。

**Goal:** 减少重复同步、提前规则数据、补昨日收尾并消除零金额帖子导致的无效补链与规则阻断。

**Architecture:** 复用 BC 时钟、规则账户范围、日报成功水位、任务去重和运行摘要。保留现有授权冻结、租户/BC 边界以及事实发布合同，无 schema 变更。

**Tech Stack:** Python / SQLModel / PostgreSQL / Redis / pytest。

**Spec:** [设计](../specs/2026-10-d)

## Global Constraints

- 用户 2026-10-21 追加授权完成后推送并发布生产，按手册核验精确版本和自然排空。不人工调用真实 TikTok；全部既有未跟踪文件不修改。
- 中文注释说明业务原因，不保留被替换的旧行为；只暂存本阶段明确文件。
- 每阶段 Ruff、ty、改动 Python 格式通过；mypy 对比上一提交无新增；三个模块完整回归。

## Review Focus

- 素材时钟错开、热报失败与筛选退避必须区分；不得把未到期当失败补拉。
- 启停规则、延迟多轮、跨 BC 所有权不改变范围或漂移相位。
- 昨日无事实、混合时区、未完成/已完成任务以及每日覆盖不漏数或挤占今日。
- 已审计规则依赖，用户批准五处共用窗口金额定义；页面独立行和真实映射不改变。
- 健康分类本轮取消：现有负证据不足以证明缺 VID，保留现状。

## 阶段 1：双时钟

**Files:** job_tasks.py、test_job_tasks.py、本设计/计划、implementation-progress.md。
**Interface:** _plan_one 冻结进入轮次的到期状态，按热报相位统一推进双时钟。

- [x] 补错开周期重复规划和失败兜底回归，真实 PG 先复现。
- [x] 限制兜底到热报确实到期；统一双时钟，保留外部筛选退避的原保护目的。
- [x] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 2：规则锚定

**Files:** planner.py、job_tasks.py、test_planner.py / test_job_tasks.py、implementation-progress.md。
**Interface:** 复用 rule_accounts 判定；RULE_SYNC_LEAD=3 分钟。

- [x] 测规则覆盖/未覆盖、启停、跨租户/BC、重复延迟推进的相位。
- [x] 在计划维护中对齐规则 BC 时钟；无规则保持独立相位，解除规则锚定。
- [x] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 3：昨日收尾

**Files:** planner.py、job_tasks.py、jobs.py、lanes.py、新增 test_day_closeout.py、扩展 test_job_tasks.py、implementation-progress.md。
**Interface:** 按账户昨日事实及 report_sync_state 水位规划单日收尾，复用 enqueue_jobs 去重。

- [x] 测首轮、重复规划、无事实、混合时区、收尾发布及每日覆盖。
- [x] 实现收尾入口；核验实时队列中今日优先，记录选择的优先级机制。
- [x] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 4：关联范围与规则缺数统一

**Files:** reporting/material_value.py、material_links.py、jobs.py、身份/目录候选；automation_rules/material_inputs.py、inputs.py、data_issues.py；运行记录文案及相关测试、实施/验收文档。
**Interface:** 共用窗口金额判定；现有 skipped_accounts 计数，不更改 schema/页面归并/健康分类。

- [x] 阅读 8bd35045 并复现依赖；审计提交 ff0bb7b5 后用户批准同时修复规则阻断。
- [x] 五处判定共用窗口跨日合计，关联执行候选同步过滤；等待任务正常复查结束。
- [x] 保留并扩展原测试：零金额、仅收入、正负抵消、范围隔离、规则命中恢复、忽略计数持久化、混合任务与前端文案。
- [x] 完整门禁、实施记录和暂存核验：1034项通过，Ruff/ty/15文件格式通过，mypy无新增；本计划随单独阶段4产品提交。
- [ ] 按追加授权推送并发布生产，记录实际 CI、版本、服务及业务观察结果。
