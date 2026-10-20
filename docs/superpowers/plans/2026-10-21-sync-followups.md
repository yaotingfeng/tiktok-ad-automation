# Sync Followups Implementation Plan

> 按用户明确指定的执行方式：主工作区 main 顺序实施四阶段，不建分支或工作树；无需重复设计审批。

**Goal:** 减少重复同步、提前规则数据、补昨日收尾并区分可修复的素材缺口。

**Architecture:** 复用 BC 时钟、规则账户范围、日报成功水位、任务去重和关联负证据。保留现有授权冻结、租户/BC 边界以及事实发布合同，无 schema 变更。

**Tech Stack:** Python / SQLModel / PostgreSQL / Redis / pytest。

**Spec:** [设计](../specs/2026-10-d)

## Global Constraints

- 不推送、不发布、不调用真实 TikTok、不连接线上数据库；全部既有未跟踪文件不修改。
- 中文注释说明业务原因，不保留被替换的旧行为；只暂存本阶段明确文件。
- 每阶段 Ruff、ty、改动 Python 格式通过；mypy 对比上一提交无新增；三个模块完整回归。

## Review Focus

- 素材时钟错开、热报失败与筛选退避必须区分；不得把未到期当失败补拉。
- 启停规则、延迟多轮、跨 BC 所有权不改变范围或漂移相位。
- 昨日无事实、混合时区、未完成/已完成任务以及每日覆盖不漏数或挤占今日。
- 零金额关联可能支撑规则或页面；先审计再决定是否实施。
- 负证据必须属于当前缺口、日期及完整扫描，过期/签名变化不可掩盖异常。

## 阶段 1：双时钟

**Files:** job_tasks.py、test_job_tasks.py、本设计/计划、implementation-progress.md。
**Interface:** _plan_one 冻结进入轮次的到期状态，按热报相位统一推进双时钟。

- [x] 补错开周期重复规划和失败兜底回归，真实 PG 先复现。
- [x] 限制兜底到热报确实到期；统一双时钟，保留外部筛选退避的原保护目的。
- [x] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 2：规则锚定

**Files:** planner.py、job_tasks.py、test_planner.py / test_job_tasks.py、implementation-progress.md。
**Interface:** 复用 rule_accounts 判定；RULE_SYNC_LEAD=3 分钟。

- [ ] 测规则覆盖/未覆盖、启停、跨租户/BC、重复延迟推进的相位。
- [ ] 在计划维护中对齐规则 BC 时钟；无规则保持独立相位，解除规则锚定。
- [ ] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 3：昨日收尾

**Files:** planner.py、job_tasks.py、jobs.py / lanes.py（如优先级合同需要）、test_planner.py / test_jobs.py、implementation-progress.md。
**Interface:** 按账户昨日事实及 report_sync_state 水位规划单日收尾，复用 enqueue_jobs 去重。

- [ ] 测首轮、重复规划、无事实、混合时区、收尾发布及每日覆盖。
- [ ] 实现收尾入口；核验实时队列中今日优先，记录选择的优先级机制。
- [ ] 完整门禁、记录、检查状态与暂存差异、聚焦提交。

## 阶段 4：关联范围与健康分类

**Files:** material_links.py、health.py、相关 material/link/health 测试、implementation-progress.md。
**Interface:** 共用业务价值判定；复用 MaterialLinkScan 负证据，不更改 schema。

- [ ] 阅读 8bd35045、相关设计/验证及调用方，记录零金额关联原因和依赖结论；有依赖即停止。
- [ ] 无依赖时补金额选择、等待收敛、负证据有效/无效/过期与修复回归。
- [ ] 实施共用判定和平台缺口分类，保留真实未知映射。
- [ ] 完整门禁、记录、检查状态与暂存差异、聚焦提交；给出四阶段交接及偏离说明。
