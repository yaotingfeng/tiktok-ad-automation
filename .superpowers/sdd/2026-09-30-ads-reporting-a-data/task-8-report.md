# A8 实施报告（2026-09-30）

## 结果

- `ads.sync_step`、`reporting.sync_step` 分别注册到 `ads-directory`、`ads-reporting`；Beat 的 `reporting.scan_due` 固定走 `control`，由 `ADS_SYNC_ENABLED` 默认关闭门控。
- 目录/报表 Worker 在 Compose、生产模板和新加坡 systemd 模板中独立部署，默认并发 1；Outbox 排队索引覆盖新队列。
- A7 续页和异步报告 WAIT/check/download 在页状态事务内创建稳定 successor dispatch。运行 lease 过期时 scanner 递增领取代数，迟到 worker 在路由验证、暂存和发布边界被旧代围栏阻止。
- admission policy/Lua 保留原六个桶，并支持 `reports.task_create` 可选账户 500/小时原子桶；释放租约不回退小时计数。空或非法策略仍拒绝发送。

## 验证

- `pytest tests/jobs/test_ads_reporting_queues.py tests/modules/reporting/test_sync_recovery.py -q`：5 passed。
- `pytest tests/jobs/test_admission.py tests/jobs/test_task_routes.py -q`：37 passed。
- `pytest tests/jobs/test_outbox.py -q`：26 passed。
- `pytest tests/modules/reporting tests/modules/ads -q`：48 passed。
- `pytest tests/modules/accounts/test_worker_route_authorization.py -q`：22 passed。
- `pytest tests/jobs -q`：107 passed，3 skipped。
- 变更范围 Ruff、ty 和 `compileall` 通过；没有部署、启用开关或调用 TikTok/MCP。

真实 PostgreSQL/Redis 用于既有回归和异步额度测试；provider transport 仅在适配器边界替身。实际 API/MCP 联调、部署开关及采集周期尚待单独验收。
