# A8 广告目录与报表队列验收（2026-09-30）

本记录对应 A8 队列、公平性、冻结路由和恢复实现。未部署、未开启 `ADS_SYNC_ENABLED`，未调用 TikTok API 或 MCP。

## 已验证合同

- `ads.sync_step` 固定投递 `ads-directory`，`reporting.sync_step` 固定投递 `ads-reporting`；`reporting.scan_due` 只在 `control` 队列运行，并由 `ADS_SYNC_ENABLED` 门控。
- Compose、systemd 模板声明目录/报表独立消费者，默认并发均为 1；`queued_dispatches` 索引包含新队列，旧资源消息仍按正式 task route 交接。
- A7 续页、异步 task_id/check/download 和 WAIT 状态在同一 PostgreSQL 事务写入 successor outbox。运行 lease 过期后递增 `claim_generation`，旧 worker 的 route、暂存和发布均被拒绝。
- Redis admission Lua 保留原六个桶；可选 `reports.task_create` 账户 500/小时桶与其余桶一次性原子检查/写入，释放租约不退小时计数。

## 证据边界

新增队列/恢复测试使用隔离 PostgreSQL 与 Redis，TikTok transport 仅在适配器边界替身；不把模拟吞吐、定时器间隔或 worker 启动当作实际采集周期或平台联调证据。非空 `TIKTOK_CALL_POLICIES` 是外部请求的部署前提，模板默认仍保持关闭。

待后续部署验收：API、两个广告 Worker、Beat 的实际环境值一致性；真实数据库/Redis 恢复演练；当前 BC 授权下的 API 与 MCP 只读报告读取；平台异步报告创建额度与返回文件合同。上述项目未在本任务执行。
