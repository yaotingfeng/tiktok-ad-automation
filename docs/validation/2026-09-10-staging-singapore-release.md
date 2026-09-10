# 新加坡测试环境首次部署验收 — 2026-09-10

## 范围

用户授权连接 `137.220.150.31:22211`、安装缺失环境、无 Docker 部署；SSH 主机密钥变更已由用户确认更新。仅操作该新测试服务器，未操作骏伯生产服务器。环境规则见 [staging-singapore.md](../runbooks/staging-singapore.md)。

运行代码固定为 `995f89569880df330b02314bfff5e1df33256b1a`，由 `git archive` 传输 Git 跟踪文件；未传输工作区未跟踪的 `docs/design-history/`、本地环境变量、数据库、素材或集成凭据。本次只新增运行文档，无应用代码或迁移修改。

## 实际结果

| 检查 | 结果 |
| --- | --- |
| 公网入口 | `https://137.220.150.31`，公网 curl 在正常证书校验下健康检查返回 `true` |
| TLS | Let's Encrypt 受信任 IP 证书，首次证书到期日 2026-09-17；80 HTTP 重定向 HTTPS，ACME 路径保留 |
| 证书续期 | Certbot 5.8.0，`renew --dry-run --no-random-sleep-on-renew` 成功；每小时 timer 已启用，成功续期后 reload Nginx |
| 原生运行环境 | Ubuntu 24.04.1 x86_64；Python 3.14.2、uv 0.9.26、Bun 1.4.2、PostgreSQL 18.6、Redis 8.10.1；Nginx、FFmpeg 已安装；无 Docker |
| 依赖与构建 | `uv sync --frozen --package app`、`bun install --frozen-lockfile`、`bun run --filter frontend build` 成功 |
| 数据库 | 全新独立库 Alembic upgrade 成功，current/heads 均为 `r2_part_receipts`，初始管理员成功创建 |
| 服务 | API、Worker、Beat、Nginx、PostgreSQL、Redis 均 active，应用服务开机自启；单 Worker 节点、prefork concurrency=2、唯一 Beat |
| 网络边界 | PostgreSQL 5432、Redis 6379、API 18000 仅 loopback；对外 SSH 22211 与 Nginx 80/443 |
| 入口脚本 | HTTPS health、静态 login、未配置 App 的回调业务错误、未知 API 404 全部通过 |
| 登录 | 实际 HTTPS 管理员登录 200；受保护 profile 验证管理员身份；不带 token 请求拒绝 401；凭据和 token 未写日志或 Git |
| 浏览器 | Chrome 实际公网登录页显示 TK-ADA / 广告投放工具 / 登录，账号密码表单正常，无浏览器 console error；未将浏览器检查扩展为真实业务操作 |
| 队列 | Worker inspect ping 成功，节点 `staging-singapore@C20260910195878`；持久化 no-op `jobs.probe` 经 Beat 发布，attempts=1，Worker 日志 received/succeeded |
| 回归 | 独立 PostgreSQL 测试库、独立 Redis DB 14/15，bootstrap surface、login 和 jobs 合计 **72 passed**（18.21s） |
| 备份 | 每日 timer 已启用并实际执行成功；PostgreSQL dump 恢复到新临时库后 head 与管理员数量 1 一致；Redis RDB 文件检查通过 |
| 资源 | 验收时约 886 MiB available、14 GiB 可用磁盘，未配置 swap；适用于基础测试，未做生产容量承诺 |

队列诊断任务 ID：`6ec49c9d-9f60-4c83-aaaf-3a415b11ab81`。这是内部无外部操作的探针，不代表广告任务或外部授权成功。

首次回归使用无 CREATEDB 权限的应用角色时，64 项通过、1 failed / 7 errors 均为测试夹具创建临时数据库被拒绝。随后使用独立测试角色重跑全部 72 项通过；应用数据库角色未提升。测试保留上游 Starlette TestClient 的 httpx 弃用提示，不修改锁定依赖绕过它。

## 交付与限制

- 登录凭据由服务器随机生成，私有文件 `/root/tt-ada-staging-login.txt`；本地 `.runtime/singapore-staging/login.txt` 为 0600 且 Git 忽略。应用配置 `/etc/tt-ada-staging/app.env` 为 0640、root:tt-ada。
- 未配置 TikTok App/BC、R2/S3 或版权方，未进行真实 OAuth、上传、取链或广告创建。素材导入/清理保持关闭；上线基础检查不替代真实外部联调。
- 备份在同机 root 私有目录，未配置异地副本或自动过期删除；Redis 仅文件完整性检查，未做新实例恢复验收。后续需监控磁盘并根据数据量配置异地备份策略。
- 首次无旧运行版本；后续升级须按环境手册排空、备份、迁移和切换，不能直接覆盖运行目录。
- 本轮运行文档在当前既有分支聚焦提交，不推送；文档提交不改变服务器运行 SHA。

验收结束后已将专用测试角色 `tt_ada_test` 设置为 `NOLOGIN NOCREATEDB`，测试配置收紧为 root 0600；后续重跑创建临时库的用例时，由运维按测试范围临时启用，结束后再次收回。
