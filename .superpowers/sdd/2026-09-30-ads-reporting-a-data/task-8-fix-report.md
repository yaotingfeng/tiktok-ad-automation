# A8 P1 修复报告（2026-09-30）

## 修复结果

- `reporting.sync_step` 的 `WAIT` 续接 dispatch 与 `ReportSyncRun.next_attempt_at` 在同一数据库事务中写入；`PendingDispatch.available_at` 至少等于持久化的下次尝试时间，因此 Outbox publisher 在退避窗口内不会投递。
- Worker 在领取 lease 前再次检查 `next_attempt_at`。旧 broker 消息、重复投递或绕过 publisher 的调用在到期前直接返回 `WAIT`，不领取 lease、不建立 TikTok gateway，也不消耗报告检查额度。
- Worker continuation 与 Beat `scan_due` 共用稳定 successor key，保持事务 successor 的幂等去重。终态、已发布版本、旧领取代数和路由重绑定仍在 provider 调用前安全短路；冻结路由不会被替换。

## 验证

- `pytest tests/modules/reporting/test_sync_recovery.py -q`：6 passed。
- `pytest tests/jobs/test_ads_reporting_queues.py tests/modules/reporting/test_sync_recovery.py tests/jobs/test_outbox.py tests/jobs/test_task_routes.py tests/jobs/test_admission.py -q`：71 passed。
- `pytest tests/modules/ads/test_directory_sync.py tests/modules/reporting/test_report_sync.py tests/modules/reporting/test_scheduling.py tests/jobs/test_worker_queue_isolation.py tests/modules/accounts/test_worker_route_authorization.py -q`：44 passed，2 skipped。
- 变更范围 Ruff、ty、`compileall` 通过；`git diff --check` 通过。

测试使用真实 PostgreSQL/Redis 验证 Outbox 到期门禁、报告 WAIT 续接和租约状态；provider transport 仅使用测试替身。没有部署、启用开关或调用 TikTok/MCP。
