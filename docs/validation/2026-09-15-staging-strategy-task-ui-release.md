# 2026-09-15 新加坡策略名称与任务详情发布验收

## 范围

- 用户授权把现有分支的全部本地提交推送，并部署到新加坡测试环境。
- 固定发布提交为 `cc662011f0429b65949224f883497f0f830172d2`，替换 `c79465cbce3d16f0a2831d6ab1c1d4f3bf580cae`。
- 本轮主要包含策略显示名称可编辑，以及任务详情精简、已提交预览只读。没有数据库迁移、功能开关或 systemd 单元变更，也没有真实 TikTok 写操作。

## 发布前验证

- 策略后端回归 69 项、策略页面 Playwright 40 项通过。
- 后端 Ruff、Ruff format、ty，前端 Biome、TypeScript 与 Vite 构建通过。
- 服务器切换前隔离 Linux 回归：批处理/并发 4 项通过，MID 发现 24 项通过。
- 固定提交在服务器构建出 82 个前端静态文件；发布前 API、Beat 停止接收新工作，三个 Worker 正常排空。

## 备份与恢复演练

- 完整备份：`/var/backups/tt-ada-staging/20260915T082018Z/`。
- `postgres.dump`、`redis.rdb`、`config.tar.gz`、`project.tar.gz`、`runtime-config.tar.gz` 的 SHA-256 校验全部通过。
- PostgreSQL、Redis、项目与运行配置隔离恢复通过；恢复库迁移到 `build_batching`，队列隔离 2 项通过，24 份加密响应归档可解密读取。
- 备份 timer 在发布后恢复 active，旧 release 保留，可按发布手册回退。

## 切换后验收

- `/opt/tt-ada-staging/current` 指向 `cc662011f0429b65949224f883497f0f830172d2`。
- API、resources Worker、builds Worker、control Worker、Beat 五个 systemd 服务均 active，重启次数为 0，进程工作目录全部指向新 release。
- Alembic current/head 均为 `build_batching`；素材导入/清理开关及准入租约配置与发布前一致。
- 平台管理员和租户管理员登录、个人信息、租户访问、平台权限隔离及各租户 MCP `READY` 状态通过；HTTPS 登录页、静态资源、私有 MCP callback 边界及 API 404 通过。
- `resources-singapore`、`builds-singapore`、`control-singapore` 三个 Celery 节点均返回 pong，活动任务为空；切换后五服务日志无 warning。
- 发布后系统盘剩余约 5.3 GiB，可用内存约 535 MiB；未触发 TikTok 授权、素材上传或广告创建。
