# 素材批量选择：新加坡测试环境发布

- 授权：用户明确要求“部署一下”，沿用当前新加坡测试服务器。
- 目标：`https://tk-ada.137-220-150-31.sslip.io`，`137.220.150.31:22211`，systemd 服务用户 `tt-ada`。
- 版本：`b618aa21caec89dd6a1f24e5b45c56281fbf5af8` → `e45decdc632b82f6df00caaf1c701fb15b0d772e`。固定 Git 归档加已通过构建的前端产物，经 SHA-256 校验后传输和解压；依赖锁文件一致，复用原虚拟环境。生产未变更，Git 未推送。

## 变更及验证

“调整素材 → 添加素材”支持多条勾选、全选本页、跨页/搜索保留选择、已有素材禁选、清空及统一确认。列表独立滚动，底部按钮保持可见。保留已有分组、统一保存及未知响应恢复机制；无后端代码和迁移变化。

本地发布前准备页 58 项 Playwright、TypeScript/Vite 构建、Biome 和差异检查通过。部署后直接加载线上前端，以模拟 API 数据完成 3 项批量选择测试，覆盖桌面/手机布局、跨页搜索、去重、统一保存、取消和清空，全部通过。未向实际业务 API 保存测试草稿或创建广告，不将模拟交互报告为真实广告联调。

## 备份和切换

暂停 backup timer，确认已有备份任务已结束；停止 API 和 Beat，使用 SIGTERM 与 960 秒停止期限正常排空 Worker。所有备份位于同一停写窗口：`/var/backups/tt-ada-staging/20260914T093726Z/`。

- PostgreSQL 自定义 dump、Redis RDB、独立项目归档、原配置归档及完整配置/证书归档均通过 SHA-256 检查。
- PostgreSQL 恢复至新建临时库并检查 head；最近一条响应归档使用原密钥解密，正文长度和 SHA-256 一致。
- Redis 通过独立 Unix socket、禁用 AOF 的临时实例恢复，PING 与读取成功；未覆盖业务 Redis。
- 项目及完整私有配置恢复至隔离目录，源码、实际前端入口、app.env 和 MCP 注册文件一致；独立归档保留锁文件，排除虚拟环境、node_modules 和缓存。
- 恢复库演练及业务库检查均为既有 head `provider_display_drama_id`，没有新迁移。原版本目录和完整备份保留，恢复临时数据库已清理、临时 Redis 已停止。
- 原子切换 `current` 后启动 API/Worker/Beat，并恢复 backup timer。

备份批次内有 `SHA256SUMS`、`release-manifest.txt` 和 `RELEASE_COMPLETE`；备份仍为同机存储，未配置异地副本。

## 运行验收

- API 1 个进程、Worker 3 个进程、Beat 1 个进程均从目标版本 backend 工作目录运行。
- 各进程 `MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true`，沿用既有授权值。环境文件摘要不变，数据库、Redis、加密密钥、MCP 注册、媒体主机与调用策略跨进程一致；每个进程实际环境的 Settings 和调用租约检查通过。
- HTTPS 健康返回正常，HTML 与搭建页 JS 和本地构建逐字节/摘要一致，包含新增多选功能。
- 平台管理员、租户管理员真实登录及受保护 profile 通过；未输出凭据。第一次脚本访问登录页未带 HTML Accept 而返回 404，按站点 SPA 请求约定补齐请求头后验证通过。
- Worker ping 返回 pong；backup 与证书续期 timer active；本次切换后的服务 error 级日志无记录。
