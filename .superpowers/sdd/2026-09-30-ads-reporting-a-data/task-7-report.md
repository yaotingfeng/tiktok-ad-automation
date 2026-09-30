# A7 持久计划、回补与刷新合并

状态：实现完成，等待根端复核与 A8 队列整合。

本任务新增 `reporting.scheduling` 的冻结路由请求合同、确定性请求幂等、账户本地日期窗口、固定同步计划、到期计划推进、实际覆盖摘要和活跃/近期消耗成员选择；新增广告目录与报表的有界 Celery 步骤入口。请求只保存 `run_id` 与领取代数，worker 在物理请求前重新读取操作者、BC 绑定和账户授权。

固定计划覆盖目录 3 小时、活跃配置/余额 30 分钟、核心今日与前一日 30 分钟、近 7 日 3 小时、归因窗口加 7 天（窗口未知时 35 天）、每周 90 日和初始 30 日。路由通道、授权代数和绑定代数进入计划键；解绑停用后续计划，历史事实保留。

验证（专用 PostgreSQL/Redis 环境，未进行 TikTok/MCP 请求）：

- `uv run --frozen pytest tests/modules/reporting/test_scheduling.py -q`：7 passed
- `uv run --frozen pytest tests/modules/reporting tests/modules/ads -q`：44 passed
- `uv run --frozen ruff check app/modules/reporting/scheduling.py app/modules/reporting/tasks.py app/modules/ads/tasks.py tests/modules/reporting/test_scheduling.py`：通过
- `uv run --frozen ty check app/modules/reporting/scheduling.py app/modules/reporting/tasks.py app/modules/ads/tasks.py`：通过
- `uv run --frozen python -m py_compile app/modules/reporting/scheduling.py app/modules/reporting/tasks.py app/modules/ads/tasks.py`：通过

A8 需要把两个任务注册到独立 `ads-directory`/`ads-reporting` 消费者并接入 Beat/Outbox；本任务没有修改共享队列、配置或迁移。

## Review 修复

根据 `task-7-review.md`，补齐了以下合同边界：

- 终态 request occurrence 不再永久复用；进行中的分片仍按确定性请求 ID 合并。
- 所有已批准报告合同和 Smart+/Legacy Smart+ 类型均被播种；超过 30 日的基础报告按不重叠日期分片，实体引用按维度过滤。
- 归因计划固定在每日 13:00 UTC，初始计划只运行一次，已发布覆盖回写归因窗口并支持 +7 日扩展。
- 目录运行从持久 `next_page` 续跑，已发布/终态运行在打开 provider 前短路；targeted 仅创建匹配层级，balance 走独立余额观测。
- 余额运行在远端 task 建立前保持本地 `task_id` 为空；后台任务重读冻结身份/授权；计划播种使用 PostgreSQL advisory lock，既有 `next_due_at` 不被重置，计划键包含合同/指标族/广告类型身份。

复核验证（专用 PostgreSQL/Redis 环境，未调用 TikTok/MCP）：

- `.venv/bin/pytest -q backend/tests/modules/reporting backend/tests/modules/ads`：45 passed
- `.venv/bin/ruff check ...`：通过
- `.venv/bin/mypy backend/app/modules/reporting/scheduling.py backend/app/modules/reporting/tasks.py backend/app/modules/ads/tasks.py`：通过
- `python3 -m py_compile backend/app/modules/reporting/scheduling.py backend/app/modules/reporting/tasks.py backend/app/modules/ads/tasks.py`：通过

## Re-review 1 修复

- 报表 worker 从持久 query 读取 `ad_type` 并传入 typed gateway，A6 解码后的查询仍按 basic ad、Smart+ 和 material 合同执行；余额使用独立的本地 `task_status` 标记，远端 `task_id` 仍为空直到真正建 task。
- material breakdown 计划始终使用空实体过滤器，避免 A6 拒绝未经核验的 filtered partition。
- request occurrence 只有在同一请求的全部 report/directory shards 都进入终态时才生成新 UUID；混合终态与进行中分片会复用原 request ID。报告计划读取已知 attribution coverage，未知时才使用 35 日窗口。

复核验证（专用 PostgreSQL/Redis，未调用 TikTok/MCP）：

- `.venv/bin/pytest -q backend/tests/modules/reporting/test_scheduling.py backend/tests/modules/reporting backend/tests/modules/ads`：46 passed
- `ruff check`、`mypy`（3 个变更模块）、`python3 -m py_compile`：通过
