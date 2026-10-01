# 广告管理与报表测试环境发布（2026-10-02）

## 发布范围

- 测试环境：新加坡 staging（`137.220.150.31` / `tk-ada.137-220-150-31.sslip.io`），不是生产环境。
- 固定运行版本：`5bf5b5506180b62b87e6720f5edf1b32bc6002ad`。
- 前端使用该版本重新构建并随 release 上传；API、Worker、Beat 使用同一 release 和共享虚拟环境。
- `ADS_SYNC_ENABLED=false` 保持关闭；未触发广告目录或报表主动拉取，未调用真实 TikTok API/MCP，未执行广告写入。

## 停写、备份和迁移

- 发布前停止 API、Beat 并正常排空资源、结果、搭建和控制 Worker；备份 timer 在停写窗口内暂停，完成后恢复。
- 完整发布备份：`/var/backups/tt-ada-staging/20261001T163955Z/`。
- 备份包含 PostgreSQL dump、Redis RDB、当前项目与构建产物、私有配置、systemd/Nginx/证书和备份脚本；`SHA256SUMS` 校验通过。
- 备份恢复演练使用独立 Redis 实例和临时 PostgreSQL 数据库，Alembic upgrade/check 通过，未覆盖运行中的 Redis 数据目录。
- 在线数据库迁移完成，当前 Alembic head 为 `ad_management_cancelled`。

## 发布后验收

- `/opt/tt-ada-staging/current` 指向固定版本 `5bf5b5506180b62b87e6720f5edf1b32bc6002ad`。
- API、resources、results、builds、control、Beat 和 `ad-management` 服务 active，Celery ping 全部返回 `pong`。
- `ads-directory` 和 `ads-reporting` 服务单元已安装但保持 inactive，等待真实通道只读联调及 `ADS_SYNC_ENABLED` 明确开启。
- `ad_management.execute`、`ads.sync_step`、`reporting.sync_step` 任务合同和 `ad-management` 队列已加载。
- `TIKTOK_CALL_POLICIES` 非空且 API/Worker/Beat 使用同一配置摘要；广告同步开关实际加载为 `false`。
- HTTPS health、构建登录页、回调业务错误和 API 边界检查通过；切换后最近 10 分钟服务 error 日志为 0。
- 磁盘剩余约 2.9 GiB，内存和 swap 保持测试机保守并发；未扩大外部调用并发。

## 未完成的外部验收

真实 API/MCP 只读广告目录、报表拉取、30 分钟/3 小时周期观察以及广告管理写入仍需在用户明确选择的 BC/账户范围内单独验收。本次发布只完成测试环境代码、数据库和服务部署，不把健康检查或模拟传输测试当作真实平台业务成功。
