# 2026-10-04 测试环境官方 TikTok API 应用配置验收

## 结果

- 目标环境：新加坡 staging（测试环境），当前代码版本 `76c983cf6071eafae780862d2797d4d327ccfc13`。
- 已配置官方 API App：App ID 末四位 `6748`；Secret 已写入测试服务器私有配置，本文不记录 Secret。
- 回调地址：`https://tk-ada.137-220-150-31.sslip.io/api/integrations/tiktok/callback`。
- 授权入口：`https://business-api.tiktok.com/portal/auth`。
- 原有 MCP 注册、调用额度、数据库、Redis 和素材相关开关保持不变。

## 备份与重载

- 修改前完成完整备份：`/var/backups/tt-ada-staging/20261004T032437Z/`。
- 备份包含 PostgreSQL、Redis、私有配置/证书、当前项目归档及运行时配置归档；`SHA256SUMS` 校验通过，项目和配置归档隔离解压验证通过，`RELEASE_COMPLETE` 已生成。
- 修改后重载 API、资源/结果/构建/控制 Worker、Beat，以及已运行的广告目录、报表和广告管理 Worker；九个服务均 active，全部实际进程读取同一份新配置。
- 备份定时器恢复为 active，无 failed systemd unit。

## 验证

- Settings 解析、官方授权地址校验、App 配置完整性和 `TIKTOK_CALL_POLICIES` 六个关键操作租约校验通过。
- HTTPS 健康、登录、平台/租户隔离、官方 API/MCP configuration 均通过；`OFFICIAL_API` 返回 `configured=true`、`status=READY`。
- 官方回调无 state 时返回预期 `invalid_oauth_state`，未泄露参数；未知 API 路径返回 404。
- 官方授权入口从测试服务器探测返回 HTTP 200，带测试站点回调参数可达。
- Celery 7 个节点全部 `pong`，健康检查和构建登录页面检查通过。

本次验收没有代替用户完成 TikTok 授权同意、BC 绑定、账户发现、素材上传或广告写入；这些步骤需要在登录 TikTok 后按选定 BC 单独验收。
