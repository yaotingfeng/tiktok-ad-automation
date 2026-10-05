# 2026-10-05 测试环境 CF8H 搭建明细查询修复

- 修复提交 `3f228a7aaf5eb8553b488696140973e4b8c7381b` 已推送到 `origin/main` 并发布到新加坡测试环境，当前指针为 `/opt/tt-ada-staging/releases/3f228a7`。
- 根因是搭建任务组合查询的 `materials` CTE 只按 `u.id` 分组，却引用了 `u.tenant_id`、`u.preview_id`、`u.drama_id`，PostgreSQL 报 `GroupingError`，前端因此显示“请求未完成”。现已补齐分组字段。
- 发布前停写、停止 Beat 并正常停止各 Worker；备份批次 `/var/backups/tt-ada-staging/20261005T061319Z/` 已生成。旧运行项目归档为 `project-current.tar.gz`，SHA-256 为 `2d3fa9bfe70c2253acfa5f69f29777363070348e88ef55422d40f4909cdec28d`，备份目录含 `COMPLETE`。
- 发布后 API、资源/结果、构建、控制 Worker、Beat 和备份 timer 均 `active`；Celery 4 个节点 `pong`，健康接口返回 HTTP 200，OpenAPI 返回 HTTP 200。
- 使用测试租户管理员只读请求 CF8H（submission `454cfb90-d81d-4c81-b94c-9abbd5e00b42`）的 `/units?limit=1`，返回 HTTP 200 且含 1 条结果；发布后 API 日志未再出现 `GroupingError`。
- 本轮未调用 TikTok/版权方写接口，未修改广告或任务数据。`check-bootstrap.py` 对公网 HTTPS 入口仍因既有入口断言失败，未将其记为通过；本地完整 PostgreSQL 回归仍缺专用测试数据库。
