# 新加坡测试环境（无 Docker）

## 目标与授权

- 用户于 2026-09-10 授权在 `137.220.150.31` 安装缺失依赖并部署项目，明确不使用 Docker；SSH 端口 `22211`，主机密钥更新已获确认。
- 本环境是独立测试环境，不是骏伯生产环境。首次只读检查：Ubuntu 24.04.1 x86_64、约 2 GiB 内存、20 GiB 系统盘（17 GiB 可用）；只有 SSH 和系统服务，无已有业务与数据库。
- SSH 密码、应用密钥、管理员密码及运行环境文件不进 Git。部署仅传输固定提交的跟踪文件，不复制本地 `.env`、数据库、素材或真实集成凭据。

## 部署方案

- 目录 `/opt/tt-ada-staging/releases/<Git SHA>`；`current` 指向正在运行的版本。
- 私有配置 `/etc/tt-ada-staging/app.env`；应用服务使用独立 `tt-ada` 系统用户。
- 安装 Python 3.14 / uv、Bun 1.4.2、PostgreSQL 18、Redis 8、Nginx；依赖按仓库锁文件冻结安装。
- PostgreSQL 和 Redis 只监听 loopback；Nginx 提供测试入口，API 仅监听 `127.0.0.1:18000`。域名/TLS 状态以本次验收记录为准。
- systemd 管理 API、Linux prefork Worker（小内存主机先使用 2 个进程）与唯一 Beat；禁止 API/代理访问日志采集授权参数。Beat 状态保存在 `/var/lib/tt-ada-staging`。
- 全新空库通过 Alembic 迁移到固定提交的 head，再初始化管理员。缺少 TikTok/R2/版权方配置时保持未配置，素材导入/清理开关关闭。
- 后续升级先停止接收写入，停止 Beat 并正常排空 Worker，备份数据库与 Redis；迁移成功后切换同版本 API/Worker/Beat。不可通过直接改表或删除数据修复迁移。
- 验证前端构建、Alembic head、登录和受保护接口、入口检查、Redis/数据库、Worker ping、Beat/outbox；外部真实联调单独验收。

## 当前实例与操作

- 运行提交：`995f89569880df330b02314bfff5e1df33256b1a`；首次部署记录见 [验收记录](../validation/2026-09-10-staging-singapore-release.md)。
- 入口：`https://137.220.150.31`，80 跳转 443。已签发受信任 IP 证书，非自签名证书；无需域名即可访问本次测试入口。真实 TikTok 回调/App 接入另行配置并验收。
- Python `3.14.2` / uv `0.9.26` / Bun `1.4.2` / PostgreSQL `18.6` / Redis `8.10.1` / Certbot `5.8.0`。另已安装 Nginx、FFmpeg 和基础编译依赖；未安装 Docker。
- 数据库 `tt_ada_staging`，角色 `tt_ada`；应用角色无 CREATEDB 或超级用户权限。回归测试使用单独测试角色与 `tt_ada_acceptance_test`、Redis DB 14/15，业务使用 Redis DB 0。
- `/etc/tt-ada-staging/app.env` 为 root:tt-ada、0640；管理员登录信息位于服务器 `/root/tt-ada-staging-login.txt`。本地私有副本 `.runtime/singapore-staging/login.txt` 为 0600，已被 Git 忽略。
- API/Worker/Beat 单元名依次为 `tt-ada-staging-api`、`tt-ada-staging-worker`、`tt-ada-staging-beat`。服务使用 `ProtectSystem=strict`、独立临时目录及 960 秒正常停止期限。Worker 节点名中 Celery 的 `%h` 在 systemd 文件里必须写为 `%%h`，避免被 systemd 展开为 root 家目录。
- Nginx 独立站点 `/etc/nginx/sites-available/tt-ada-staging`。Redis 开启 AOF，淘汰策略 `noeviction`；PostgreSQL、Redis、API 均仅本机监听。

```bash
systemctl status tt-ada-staging-api tt-ada-staging-worker tt-ada-staging-beat
cd /opt/tt-ada-staging/current/backend
runuser -u tt-ada -- /opt/tt-ada-staging/current/.venv/bin/python ../scripts/check-bootstrap.py https://137.220.150.31
runuser -u tt-ada -- /opt/tt-ada-staging/current/.venv/bin/celery -A app.jobs.celery_app:celery_app inspect ping --timeout 10
```

## 证书与备份

- 证书 `/etc/letsencrypt/live/137.220.150.31/`，Certbot 独立虚拟环境 `/opt/tt-ada-certbot`。IP 证书短期有效，必须保留 `tt-ada-staging-cert-renew.timer`：每小时检查一次，续期成功后通过 deploy hook 校验并 reload Nginx。80 端口的 `/.well-known/acme-challenge/` 必须持续公网可达。
- IP 证书方式依据 [Let's Encrypt / Certbot 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。续期演练已通过；使用受信任 CA 不等于已完成 TikTok OAuth 验收。
- `tt-ada-staging-backup.timer` 每日 UTC 19:00（北京时间次日 03:00）加最多 5 分钟随机延迟执行 `/usr/local/sbin/tt-ada-staging-backup`。
- root 专用目录 `/var/backups/tt-ada-staging/<UTC 时间>/` 保存 PostgreSQL 自定义格式 dump、Redis RDB、私有配置/证书和运行版本指针；仅完整成功的目录有 `COMPLETE` 标记。备份使用独占锁，不复制运行中的 AOF 文件。
- 首次 PostgreSQL dump 已恢复至新建临时测试库验证，验证后只删除该临时库；Redis RDB 已通过文件完整性校验，未进行 Redis 新实例恢复验收。备份暂留本机，未配置异地复制或自动清理；需要监控磁盘占用，不能把同机备份视为主机损坏保护。
- 数据库升级前暂停 backup timer，等待已启动的 backup service 自然结束，停止新写入与 Beat、正常排空 Worker后再备份和迁移。完成或中止处理后恢复 timer。首次部署没有旧库或旧任务需要排空。
- 回滚时先冻结写入并排空服务。只有确认数据库兼容时才切回旧 `current`；需要还原时先用新库/新 Redis 实例验证备份，禁止覆盖仍带旧 AOF 的 Redis 数据目录。首次部署没有上一个应用版本。

验收结束后已将专用测试角色 `tt_ada_test` 设置为 `NOLOGIN NOCREATEDB`，测试配置收紧为 root 0600；后续重跑创建临时库的用例时，由运维按测试范围临时启用，结束后再次收回。
