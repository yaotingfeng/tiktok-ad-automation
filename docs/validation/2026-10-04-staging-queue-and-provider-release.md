# 2026-10-04 测试环境账户发现队列与容量版权方发布验收

## 账户发现阻塞修复

最新 OAuth 换码已经成功，授权尝试进入候选凭据阶段；连接仍长时间显示 `DISCOVERING` 的直接原因是 `accounts.discover` 被投递到共享素材准备队列。当时该队列前方有约 850 个耗时素材任务，发现消息在 Redis `resources` 列表中等待消费。

`accounts.discover` 现统一投递到已有独立 `resource-results` 队列，保留专用执行槽，不清空、不重投其他素材消息。修复提交为 `cefce8c`（OAuth spawn 导入修复仍为 `db9a8d3`）。

## 容量版权方版本发布

- `a9437c2`（`providers: use libcurl for capacity sessions`）已发布到新加坡测试环境，当前指针为 `/opt/tt-ada-staging/releases/a9437c2`。
- 发布前完整备份为 `/var/backups/tt-ada-staging/20261004T093611Z/`；PostgreSQL、Redis、私有配置和项目归档 SHA-256 校验通过，项目归档可独立列出恢复。
- API、资源/结果、构建、控制、广告目录/报表/管理 Worker、Beat 及备份定时器均为 `active`，服务重启计数为 0。

## 验证

- 账户发现消息已用原 Celery 任务 ID 从共享素材队列移至结果队列；任务成功执行。对应 `DiscoveryRun` 为 `COMPLETE`、阶段 `FINALIZE`，连接为 `ACTIVE`，授权尝试为 `ACCEPTED`。
- 线上工作目录导入 `accounts.discover` 映射为 `resource-results`；Celery 7 个节点全部 `pong`；HTTPS 首页和 OpenAPI 入口返回 200。
- 容量版权方协议回归 4 项、Ruff、compileall 通过；服务器存在 `/usr/bin/curl`（libcurl 8.5.0），Provider 模块导入通过。
- 本轮未使用真实版权方凭据、未调用 TikTok 或版权方写接口，未创建广告；容量真实账号联调仍需单独授权后验证。
