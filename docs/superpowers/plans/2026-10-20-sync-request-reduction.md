# 同步请求精简实施计划（2026-10-20）

设计：`docs/superpowers/specs/2026-10-20-sync-request-reduction-design.md`。

在当前 `main` 上实施（仓库规则禁止自动建分支）。每个阶段一次聚焦提交，只暂存本阶段文件；不推送、不发布。工作区已有无关未跟踪文件（`.claude-ads/`、`frontend/*.tsbuildinfo`、`frontend/vite.config.*`），不得暂存或删除。

## 阶段 1：实时 15 分钟 + 规则最小 15 分钟（D1、D2）

改动：
- `backend/app/modules/reporting/planner.py`：`HOT_INTERVAL`、`MATERIAL_INTERVAL` = 15 分钟；更新相关注释。
- `backend/app/modules/reporting/job_tasks.py`：核对热刷新/素材刷新时钟、`rule_accounts` 并入逻辑与注释，与 15 分钟一致。
- `backend/app/modules/automation_rules/schemas.py`、`catalog.py`：频率枚举 `5M` → `15M`；`PAUSE` 最小 15。
- Alembic 数据迁移：所有存放规则定义的 JSONB 中 `schedule.frequency = "5M"` 改为 `"15M"`（先 grep 全部 `RuleDefinition.model_validate` 的数据来源，至少含 `automation_rule.definition`、`automation_rule_run.definition`）。下行迁移改回 `5M`。
- 前端 `frontend/src/features/automation-rules/ruleForm.ts`、`RuleEditorSheet.tsx`：选项、文案“每 15 分钟”、暂停默认值、排序；重新生成 `frontend/src/client/types.gen.ts`（按仓库现有生成方式）。

测试：
- `test_planner.py`、`test_job_tasks.py`、`test_realtime_sync.py` 中 2 分钟断言改为 15 分钟。
- `automation_rules/test_definition.py`、`test_schedule.py`：`15M` 合法、`5M` 拒绝、暂停低于 15 分钟拒绝；15 分钟排程时刻正确。
- 迁移测试：含 `5M` 的规则与运行快照升级后可被 `RuleDefinition` 解析。

## 阶段 2：目录按账户、按轮次合并刷新（D3）

改动：
- `backend/app/modules/reporting/directory_refresh.py`：重写 `refresh_report_directory`，按设计 D3 表格实现；常量 `STATUS_INTERVAL=30m`、`PARENT_INTERVAL=60m`、余量 5m，规则账户三层阈值 = `planner.HOT_INTERVAL`。删除 `DIRECTORY_FRESHNESS`、`GENERAL_DIRECTORY_REFRESH_INTERVAL` 旧常量及其引用。
  - 选对象：`ad_report_day` 中本任务账户、当地今日、`spend > 0` 的行（保持 `sync_job_id` 或改为当日全部有消耗行，以能覆盖账户全部有消耗广告为准，需在实现说明中写明取舍）。
  - 账户级在途约束：同租户、BC、actor、冻结路由、账户、广告类型、层级存在 `workload='live'` 且状态属于 `_ACTIVE` 的运行，跳过该层。
  - 仍复用 `request_directory`（按 100 ID 分片），不新建并行的目录请求路径。
  - 保留 `pg_advisory_xact_lock` 串行规划，避免并发热报重复规划同一账户。
- `backend/app/modules/reporting/health.py`：目录过期判定使用周期加 `REFRESH_MARGIN` 执行余量（广告层 35 分钟、上级 65 分钟、规则账户三层 20 分钟）。热报发布后目录完成仍需时间，精确周期会误判并触发额外报表补拉；刷新规划仍提前 5 分钟，不变。
- 中文注释写清“按需选对象、按账户合并、账户级在途约束”的业务原因。

测试（真实 PostgreSQL）：
- 同账户多个有消耗广告到期时间不同 → 一次规划读出该账户全部有消耗广告，单个运行（≤100 ID）。
- 缺失对象立即补读；无消耗广告不读。
- 已有在途运行 → 不新建。
- 上级层只在 60 分钟阈值或缺失时读取；规则账户 15 分钟。
- 连续两轮规划（模拟 15 分钟后）不产生重复运行；30 分钟后再次整批刷新。
- `test_directory_refresh.py`、`test_health.py` 现有用例按新语义更新。

## 阶段 3：删除目录空字段（D4）

- 删除 `main_material_id/main_material_type` 及约束：模型、Alembic 结构迁移（含下行）、合同 `contracts/ads.py`、`adapters/ads_read.py`（解析、校验与合并比较）、`modules/ads/sync.py`（两处）、相关测试。
- grep 全仓确认无其他引用（报表素材主素材 ID 属于 `material_report_day`/`material_post_link`，不要误删）。

## 每阶段验证

在 `backend/`：
```bash
uv run pytest tests/modules/reporting tests/modules/automation_rules tests/modules/ads -q
bash scripts/lint.sh
```
阶段 1 另跑前端：`bun run lint`、`bun run --filter frontend build`。全部通过后再提交；失败如实记录。

更新 `docs/implementation-progress.md`：完成项、测试结果、决定与提交号。

## 验收（由 Claude 线上只读核查，发布后）

| 指标 | 通过标准 |
| --- | --- |
| 广告日报任务 | ≤ 1,000/小时 |
| 素材日报任务 | ≤ 3,000/小时 |
| 实时目录运行 | ≤ 3,800/小时，预期约 2,500；平均每运行 ID 数明显上升 |
| STALE（superseded）目录运行 | 接近 0 |
| 排队最久任务 | < 15 分钟 |
| 有消耗广告的目录广告层年龄 | p95 ≤ 35 分钟 |
| 实时报表状态最近成功时间 | ≤ 20 分钟 |
| 规则 | 已存 5M 规则显示为 15M，可正常编辑保存 |

## 发布

不由执行者发布。阶段 1、2 含数据迁移、阶段 3 含结构迁移，按 AGENTS.md“新增数据库结构迁移等独立高影响操作仍按其具体授权处理”，由用户确认后发布。
