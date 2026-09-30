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
