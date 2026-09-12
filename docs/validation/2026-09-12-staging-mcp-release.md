# 新加坡测试服务器双通道发布（2026-09-12）

用户明确授权提交、推送全部本地代码并部署测试服务器。目标为 `137.220.150.31:22211` 的既有无 Docker 环境；骏伯生产未变更。

## 固定版本与范围

- 运行源码：`0535f050dab73632a2ea4f4bb801dca0871de238`；分支 `feat/platform-implementation` 已推送。历史设计文档纳入 `46db70e`。仓库不存在 main，未创建分支或强推。
- 旧版本 `8b59aed363c35bef317f6cd0fc30da48c40c723a` 保留。新版本使用独立目录，API/Worker/Beat 同时切换；968 个跟踪文件 SHA-256 与提交逐个一致。
- 业务源码与此前已完成的 2203 后端 / 348 前端矩阵一致。本次新增修复只涉及三个 Linux 测试/夹具文件，没有改变线上业务行为或任务期限。

## 构建与 Linux 验证

- Python 3.14.2、uv 冻结安装与 Bun 1.4.2 冻结安装成功。服务器无 Node，使用 Bun 直接执行 `typescript/bin/tsc -p tsconfig.build.json` 和 `vite/bin/vite.js build`；TypeScript/Vite、后端 Ruff、显式指定发布虚拟环境的 ty 检查通过。
- 为约 2 GiB 内存的测试主机增加独立 2 GiB swap，并写入 fstab；未重启主机。最初错误地用包含测试的普通 tsconfig 检查出现 ES target 诊断，随后使用仓库规定的构建配置通过。
- Linux 独立 PostgreSQL 测试库与 Redis DB14/15，业务使用 DB0。9 个不同用例全部通过：源上传 API/MCP × 迟到/硬终止 4 passed（83.22 秒）；广告原连接恢复 API/MCP 2 passed（81.06 秒）；广告硬期限、原件校验两个用例共 3 项通过。8 项是此前 macOS 跳过的 Linux 用例，另 1 项是相关校验回归。
- 初次测试暴露夹具 Redis 库错配、双嵌入 Worker 的共享 Hub/停止标志、假 HTTP 服务一秒空闲连接复用竞争；先抓线程栈确认无 worker 线程后才中止无限等待的测试进程，子代理未被中断。后续均自然结束。
- 测试恢复场景去除过早的 1 秒软中断，广告恢复硬限设为 10 秒以允许 MCP 握手和逐请求鉴权；源上传硬限仍为 3 秒。保留实际收到请求后进程硬终止、只创建一次、原连接读取、撤权阻断、原件不可提前释放等断言。假服务回读金额使用精确文本，不用创建 JSON 浮点冒充精度证据。
- 源上传日志 `/root/tt-ada-source-four-close.log`，恢复日志 `/root/tt-ada-build-recovery-10s-final.log`；其他三项在 `/root/tt-ada-linux-build-validation-final.log` 中通过，该轮旧恢复用例失败的历史没有删除。测试角色恢复为 NOLOGIN/NOCREATEDB。

## 备份、迁移与切换

- 先在独立业务数据恢复副本演练迁移，成功到 `mcp_cover_evidence`。
- 实际发布暂停备份 timer、确认备份 service 已结束；停止 API 新写入与 Beat，正常排空 Worker后备份，没有清空队列、outbox、数据库或持久卷。
- 本次备份 `/var/backups/tt-ada-staging/20260912T040041Z/` 有 COMPLETE 标记。PostgreSQL dump 在新库实际恢复成功；Redis RDB 在独立无 TCP 端口实例装载、PING 与 DB0 读取成功，随后关闭该校验实例，没有覆盖原 Redis/AOF。
- 从 `r2_part_receipts` 经已提交 Alembic 迁移到唯一 head `mcp_cover_evidence`。切换 current 后 API/Worker/Beat 与备份 timer 均 active，Worker ping pong。没有执行降级迁移。
- 发布前后保留 2 个用户、1 个租户、1 条成员关系；平台管理员及租户管理员的实际登录/profile、租户读取、平台接口 403 隔离均通过。
- 两个 HTTPS 入口健康、登录 HTML/静态资源、API 404、无 state 的回调业务错误均通过；MCP 回调 no-store。最初验收脚本使用旧的 `/api/users/me/` 导致重定向断言失败，改为实际 `/api/users/me` 后通过，未改应用路由。

## MCP 配置与真实联调边界

- 官方 `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat/oauth/register` 实际登记返回 HTTP 201；校验 issuer/resource、none 客户端认证方式及返回 callback。
- callback 为 `https://tk-ada.137-220-150-31.sslip.io/api/integrations/tiktok/mcp/callback`。实际客户端 ID 与原始注册响应仅留服务器私有文件；受控注册材料 `/etc/tt-ada-staging/mcp-client.json` 为 root:tt-ada 0640，配置引用写入 app.env，备份脚本覆盖整个私有配置目录。
- 应用运行用户能加载真实注册；两个管理员会话读取 MCP configuration 均为 configured=true / READY。READY 在这里只证明授权前置配置可用。
- 未借用 Codex token，未代表租户完成 TikTok 登录同意、BC 绑定、账户查询、视频上传或广告创建。真实主体/scope、完整 tools/schema、配额配置和服务配额归属、视频服务策略仍须在授权后联调核实；未复制合成策略或启用素材导入/清理开关。
- 本次站点入口：`https://tk-ada.137-220-150-31.sslip.io`；IP 入口 `https://137.220.150.31` 也通过基础检查。

## 回退限制

保留旧版本及发布前备份。新数据库包含双通道和冻结证据，不能只切回旧代码或直接降级数据库；先核实 schema 兼容性，必要时在新库/新 Redis 实例验证恢复，按环境手册执行。不要覆盖原 AOF 或删除 UNKNOWN/历史证据。
