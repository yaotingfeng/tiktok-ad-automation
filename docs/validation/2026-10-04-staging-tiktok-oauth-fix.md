# 2026-10-04 测试环境 TikTok OAuth 授权失败修复验收

## 根因

用户授权后的最新 `authorization_attempt` 为 `RESULT_UNKNOWN`，对应连接仍为 `PENDING_AUTH`。API 日志显示 OAuth 换 token 的 `multiprocessing spawn` 子进程直接导入 `app.integrations.tiktok.auth` 时发生循环导入：权限模块反向加载完整模型注册表，尚未完成时又回到权限模块。子进程未能启动 OAuth worker，父进程按设计将结果标记为 `RESULT_UNKNOWN`，因此授权失败。

## 修复

- 在 OAuth 模块加载 TikTok SDK/权限依赖前，先完成完整 SQLModel 注册，解除 spawn 子进程的导入顺序依赖。
- 新增回归测试，验证独立 Python 进程可以导入真实 OAuth worker 模块。
- 不重放旧授权码，不改写原失败尝试，不改变 token 加密、租户绑定、准入租约或 BC 路由规则。

修复提交：`db9a8d3764f4f35685228805811f8f975a332e18`。

## 测试环境发布

- 当前版本：`db9a8d3764f4f35685228805811f8f975a332e18`。
- 发布前完整备份：`/var/backups/tt-ada-staging/20261004T053152Z/`。
- PostgreSQL、Redis、私有配置/证书、项目归档和运行时配置归档均已校验；项目/配置隔离解压恢复通过，`RELEASE_COMPLETE` 已生成。
- API、资源/结果/构建/控制、广告目录/报表/管理 Worker 和 Beat 共九个服务 active，实际环境配置一致；调用额度和数据库 head 校验通过。

## 验证结果

- OAuth 进程超时回收、成功回执和 spawn 导入回归全部通过。
- 直接导入 `app.integrations.tiktok.auth` 通过。
- HTTPS 健康、登录、租户隔离、`OFFICIAL_API/MCP configuration`、无 state 回调边界通过。
- Celery 7 个节点全部 `pong`，无 failed systemd unit，备份定时器 active。

原授权码已经进入不可重放的 `RESULT_UNKNOWN`；下一步需要用户重新点击授权并完成 TikTok 同意页，才能验收真实 code 换 token 和后续账户发现。
