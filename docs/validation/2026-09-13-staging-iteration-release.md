# 新加坡测试环境迭代发布（2026-09-13）

用户明确授权推送最新迭代并部署到 137.220.150.31（SSH 22211）。本次沿用既有 `feat/platform-implementation` 分支；仓库不存在 main，未创建分支或强制推送。

## 版本与范围

- 原版本：`9c23a4c639b5a474373d0e6ba7231f404d3571fc`。
- 已推送并运行：`7b2ee95431816330f08eb594a80d940e30cd77da`。
- 包含准备进度、统一剧目确认页、草稿箱、其他版权方及批量/逐行手动推广链接。
- 数据库由 `automatic_mini_targets` 迁移至唯一 head `manual_promotion_links`。没有新增功能开关；导入与自动原件清理均保持已授权的 true。
- 入口：https://tk-ada.137-220-150-31.sslip.io 。独立 systemd 测试实例，服务用户 tt-ada，API 监听 loopback 18000，原配置和证书保留。

## 发布前验证与备份

- TypeScript/Vite 构建通过；页面回归 90 passed；专用本地 PostgreSQL 测试库与 Redis DB 13 的后端专项 31 passed，包含手动链接、迁移、草稿目录、准备进度与完整离线创建链。外部传输由合成夹具处理。
- 暂停备份 timer，停止 API/Beat，Worker 正常停止后建立同一停写窗口备份：`/var/backups/tt-ada-staging/20260913T094820Z/`，约 6.4 MiB。
- PostgreSQL dump、Redis RDB、独立 project.tar.gz、config.tar.gz 和补齐全部 Nginx/服务/私有配置/证书的 config-full.tar.gz 均通过 SHA-256 校验。
- 项目归档包含源码、迁移、锁文件与实际前端构建；排除 venv、node_modules 和缓存。沿用锁文件完全相同的已有虚拟环境；Python 3.14.2、uv 0.9.26，前端由本地锁定依赖构建后传输。
- 在临时 PostgreSQL 库恢复实际备份，验证归档响应可用原密钥解密且长度/摘要一致；在恢复库成功演练本次迁移后才迁移业务库。临时库已删除。
- RDB 在独立 Unix socket Redis 实例加载，PING/DBSIZE 通过；未覆盖业务 Redis/AOF。项目与配置独立解压，源码/构建入口/私有配置逐项比较通过；1044 个跟踪文件摘要与固定发布包一致。
- 旧版本、备份和恢复证据保留；本批次有 RELEASE_COMPLETE 标记。备份仍为同机私有副本，未配置异地复制。新增手动数据后禁止强制 downgrade。

## 线上验收

- API 1、Worker 3（主进程及两个 prefork）、Beat 1，均运行上述 SHA，使用同一 app.env；逐进程以应用用户和实际环境验证 Settings 开关及准入策略/租约。数据库、Redis、加密密钥、MCP 注册路径与媒体主机配置一致。
- `MATERIAL_INGEST_ENABLED`: true → true；`MATERIAL_CLEANUP_ENABLED`: true → true。沿用既有授权，不覆盖配置。
- 调用额度保持原策略，上传与共享租约均大于 905000ms。归档表仍有 24 行。
- HTTPS、登录 HTML/静态资源、管理员及租户管理员登录、租户访问和平台管理 403 隔离通过；OAuth 回调状态验证与 API 404 边界通过。
- `check-bootstrap.py` 通过，Celery inspect ping 返回 pong；API/Worker/Beat、备份 timer 与证书续期 timer 正常。
- 本次只验部署和只读访问，MCP configuration READY 不代表重新完成 OAuth 或真实工具调用。未发起新的版权方、TikTok 素材或广告操作；历史真实广告联调证据见既有验收记录。
