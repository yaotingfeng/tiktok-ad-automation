# 2026-09-15 新加坡数字剧目 ID 命名发布验收

## 范围与版本

- 用户要求推送并发布新加坡测试环境。功能提交 `4230f8f12871890a6b8d3e567bbd23f74896d665` 已推送 `feat/platform-implementation`，服务器从 `c6044077e63e7a4c117c6aea9b4c1723788fba54` 切换到该版本；生产服务器尚未建立。
- 广告名称模板 `{drama_id}` 改用版权方展示数字编号；版权方取链接口继续使用原长技术 ID。缺少正 ASCII 数字编号时不生成预览，不回退内部 UUID、长 ID 或本地占位 ID。
- 新迁移 `preview_display_drama_id` 只增加新预览的冻结字段，不回填历史快照或改写历史 Campaign 名。功能开关、私有配置、systemd 单元和依赖均无语义变化。

## 验证与完整备份

- 本地独立 PostgreSQL/Redis 回归163项通过；Alembic 升级与 `alembic check`、Ruff/格式和 mypy通过。服务器切换前 Linux 满批/并发门禁4项、MID发现专项24项通过，真实平台调用均由传输边界替身隔离。
- 完整备份 `/var/backups/tt-ada-staging/20260915T125915Z/` 包含 `postgres.dump`、`redis.rdb`、`config.tar.gz`、`project.tar.gz`、`runtime-config.tar.gz`；五份 SHA-256 校验通过。项目归档包含实际前端构建，排除可重建依赖与缓存；未配置异地副本。
- PostgreSQL dump 在独立临时库恢复并从 `build_batching` 升至 `preview_display_drama_id`；独立 Redis 恢复后 PING/DBSIZE 正常；项目及运行配置隔离解压比对通过；恢复库 Linux 队列隔离2项通过，24份加密素材响应均可解密且长度、摘要一致。临时恢复库和解压目录验收后已清理，备份本体与旧 release 保留。

## 切换后验收

- `/opt/tt-ada-staging/current` 及 API、resources/builds/control Worker、Beat 的实际进程工作目录均指向 `4230f8f`；五服务 active、`NRestarts=0`，备份 timer active。数据库 current/head 均为 `preview_display_drama_id`。
- 素材导入、自动清理及调用准入租约保持发布前值。三个 Celery 节点全部 pong、活动任务为空；平台管理员与租户管理员登录、profile、租户访问、平台权限隔离、MCP configuration READY、HTTPS 页面/82个构建文件、回调错误边界和 API 404 均通过。发布后五服务无 warning 级日志。
- 真实数据库目录只读验证：`The General's Wrath` 的展示编号 `31096` 生成名称 `{b31096/s345652/c1}-The General's Wrath-31096-TEST`，名称中不含其 `6a98…` 技术 ID。既有8个 BuildUnit 与5条历史 PreviewDrama 均未改写；历史长 ID 名称按原事实保留。
- 本次未发起 TikTok 授权、版权方写入、素材上传或广告创建。测试环境系统盘发布后约5.1 GiB可用；备份仍仅在同机保存。
