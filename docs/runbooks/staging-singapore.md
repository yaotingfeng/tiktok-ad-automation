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
- 后续升级先停止接收写入，停止 Beat 并正常排空 Worker，按通用发布手册备份数据库、Redis、项目文件/构建产物及私有配置（无迁移也必须备份）；迁移成功后切换同版本 API/Worker/Beat。不可通过直接改表或删除数据修复迁移。
- 验证前端构建、Alembic head、登录和受保护接口、入口检查、Redis/数据库、Worker ping、Beat/outbox；外部真实联调单独验收。

## 当前实例与操作

- 运行提交：`a69d0502b2c8dede8eb9069cc2c03913acee4bbb`；真实页面测试发现的接口定义修复、完整备份与当前待验项见 [端到端测试修复记录](../validation/2026-09-12-staging-e2e-schema-fix.md)。MCP 多 BC 更新及完整备份见 [多 BC 验收](../validation/2026-09-12-mcp-multiple-bcs.md)。最新 BC 分页修复及 Sun Browser 真实授权/BC 读取见 [分页验收](../validation/2026-09-12-staging-mcp-pagination.md)，此前 MCP 授权后修复见 [发布验收](../validation/2026-09-12-staging-mcp-auth-fix.md)，双通道更新、Linux 验证及客户端登记见 [首次双通道发布](../validation/2026-09-12-staging-mcp-release.md)。数据库 head 为 `mcp_multibc_runtime`，旧版本及发布前备份保留。
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

每次发版还须执行 [通用配置与完整备份规范](deployment.md#每次发版的配置与备份规范)。当前 `/usr/local/sbin/tt-ada-staging-backup` 的范围为数据库、Redis、私有配置/证书和 release 指针，**尚不包含独立项目归档**；脚本的 COMPLETE 只表示该范围完成。2026-09-12 发布保留了旧 release，但未生成独立项目文件归档，不将历史记录追记成已完成。

后续部署者必须在同一发布备份批次另行归档 `readlink -f /opt/tt-ada-staging/current` 对应目录，包含源码、锁文件、迁移和实际 `backend/app/frontend`（若保留 `frontend/dist` 也一并归档）；归档根应为真实 release 目录，不能仅打包 current 符号链接。同时核对配置归档是否覆盖本项目所有生效的 Nginx 站点（包括 sslip.io TLS 入口）、systemd 单元及备份脚本。将工具链版本、排除的依赖目录和校验和写入私有批次清单，隔离解压验证后记录完整发布备份结果；在自动脚本补齐前，这些是每次发布必须执行的人工步骤。归档前检查磁盘容量，禁止为腾空间直接删除现用/旧版本或未验收备份。


- 证书 `/etc/letsencrypt/live/137.220.150.31/`，Certbot 独立虚拟环境 `/opt/tt-ada-certbot`。IP 证书短期有效，必须保留 `tt-ada-staging-cert-renew.timer`：每小时检查一次，续期成功后通过 deploy hook 校验并 reload Nginx。80 端口的 `/.well-known/acme-challenge/` 必须持续公网可达。
- IP 证书方式依据 [Let's Encrypt / Certbot 官方说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。续期演练已通过；使用受信任 CA 不等于已完成 TikTok OAuth 验收。
- `tt-ada-staging-backup.timer` 每日 UTC 19:00（北京时间次日 03:00）加最多 5 分钟随机延迟执行 `/usr/local/sbin/tt-ada-staging-backup`。
- root 专用目录 `/var/backups/tt-ada-staging/<UTC 时间>/` 保存 PostgreSQL 自定义格式 dump、Redis RDB、私有配置/证书和运行版本指针；仅完整成功的目录有 `COMPLETE` 标记。备份使用独占锁，不复制运行中的 AOF 文件。
- 首次 PostgreSQL dump 已恢复至新建临时测试库验证，验证后只删除该临时库；首次部署时 Redis RDB 仅完成文件完整性校验；2026-09-12 发布已在独立 Redis 实例装载、PING 和读取验证通过，PostgreSQL 亦完成新库恢复，见当日发布验收。备份暂留本机，未配置异地复制或自动清理；需要监控磁盘占用，不能把同机备份视为主机损坏保护。
- 每次发版前（不限数据库升级）暂停 backup timer，等待已启动的 backup service 自然结束，停止新写入与 Beat、正常排空 Worker后再备份和迁移。完成或中止处理后恢复 timer。首次部署没有旧库或旧任务需要排空。
- 回滚时先冻结写入并排空服务。只有确认数据库兼容时才切回旧 `current`；需要还原时先用新库/新 Redis 实例验证备份，禁止覆盖仍带旧 AOF 的 Redis 数据目录。首次部署没有上一个应用版本。

验收结束后已将专用测试角色 `tt_ada_test` 设置为 `NOLOGIN NOCREATEDB`，测试配置收紧为 root 0600；后续重跑创建临时库的用例时，由运维按测试范围临时启用，结束后再次收回。

## Sites 转发已停用（2026-09-10）

用户明确要求删除 Sites 转发入口。转发源码与 UPSTREAM_ORIGIN / SITE_ORIGIN 环境变量已删除，发布停用版本 2（源码 `5a1e34fccfeb73a4ebf081c151301394938ddd4f`）。旧入口的 GET 首页、健康路径、POST 登录均已验证返回 HTTP 410，不再连接原站或重定向。

当前直接访问 `https://tk-ada.137-220-150-31.sslip.io`；健康检查通过。服务器、数据库、原 IP 入口、sslip.io 证书与续期均未修改。Sites 工具未提供整站删除接口，因此平台项目记录与既有访问控制保留，不能将停用转发描述为整个 Sites 项目已删除。

以下接入过程为历史记录，不代表 Sites 仍在转发。

### Sites 域名接入历史（2026-09-10）

用户指定的访问别名为 `https://ytf-server-gateway.defuelscoulter38963.chatgpt.site`，已发布服务器端 HTTPS 转发。Sites 项目 `appgprj_6aa29de565c881918beb43a148b6452c` 当前保持仅所有者访问；需要 Sites 登录后，再使用 TK-ADA 原管理员账号登录。原 IP HTTPS 入口继续可用，应用自身 FRONTEND_HOST 保留原 IP，Sites 作为访问别名。

Sites 独立源码在相邻 `server-sites-gateway/` 项目，发布源码 `148a0a7fce5fd9487366f7006852b5ff5458e29c`，版本 1。不能把该子域名当作可修改 A 记录的独立 DNS 域名。

由于 Cloudflare Workers 不支持直接向 IP 发起 fetch，回源使用 `https://tk-ada.137-220-150-31.sslip.io`。该 DNS 在服务器上已核实解析至 `137.220.150.31`；新建独立 Nginx TLS 主机，证书受信任且到期日 2026-12-09，由已有每小时 Certbot timer 一并续期，独立 dry-run 已通过。该回源依赖 sslip.io 公共 DNS，未来可替换为自有域名。Nginx 修改前副本为 `/root/tt-ada-staging-nginx-before-sites`，修改后已补做私有备份。

代理保留路径（包括尾斜杠）、查询、HTTP 方法/请求体、业务 JWT、状态码与静态资源；不把 Sites 登录 Cookie 或内部身份头传给原站，不记录请求体与授权查询，不缓存代理响应。当前应用使用 JWT，不以 Cookie 登录；新增 Cookie 登录机制时必须重新评估代理契约。原站绝对跳转改写为 Sites 地址。

验收：5 项代理边界测试、改动文件 lint、Sites 构建、线上健康/登录 HTML/JS 资源/回调/404/未登录 401、真实管理员登录和 profile 全部通过；无 Sites 身份访问仍被 401 拒绝。Sites 脚手架未使用的 UI 组件存在原有 lint 问题；本次改动文件 lint 无错误。未执行真实 TikTok/R2/版权方操作。

## 当前初始化账号（2026-09-10）

平台管理员 `admin` 密码已按用户要求更新；已创建租户 `junbo`，租户管理员为 `junbo`（非平台超级用户）。密码仅保存于上述服务器及本地私有凭据文件，不在文档中记录。两个账号重新登录、tenant_admin 成员关系和平台接口 403 隔离均验证通过；账号操作后已执行备份。

## 2026-09-12 MCP 授权前置配置

测试站点已完成官方客户端动态登记（HTTP 201），回调为 `https://tk-ada.137-220-150-31.sslip.io/api/integrations/tiktok/mcp/callback`。私有 app.env 设置 MCP_REDIRECT_URI 与 MCP_CLIENT_REGISTRATION_REF，后者指向 `/etc/tt-ada-staging/mcp-client.json`（root:tt-ada 0640）。备份脚本已包含整个私有配置目录。页面 MCP configuration 为 READY；租户登录授权、BC 绑定、配额/实际协议和素材广告联调尚未完成，READY 不证明这些能力。

服务器新增 `/swapfile-tt-ada-staging` 2 GiB swap。构建不依赖 Node：在 frontend 使用 Bun 执行 `../node_modules/typescript/bin/tsc -p tsconfig.build.json` 和 `../node_modules/vite/bin/vite.js build`。Python 虚拟环境应使用 `/var/lib/tt-ada-staging/python/` 下的共享解释器，避免指向 root 家目录。

## 2026-09-12 授权后调用限流配置

租户真实授权后须继续验证候选 BC 读取，configuration READY 不检查调用额度。测试环境已配置 TIKTOK_CALL_POLICIES：base 的 app_max_inflight=4、endpoint_max_inflight=2、tenant_max_inflight=4、advertiser_max_inflight=4、app_calls_per_window=10、endpoint_calls_per_window=3、window_ms=1000、lease_ms=960000；endpoints 为空。总量和并发是本地工程限制，并非官方提供的 App 配额；服务主体尚未核实时 MCP_SERVICE_QUOTA_SCOPE 保持未设置，让所有 MCP 连接共用保守配额域。官方工具频控依据 [自定义客户端指南](https://business-api.tiktok.com/portal/docs/how-to-connect-a-custom-agent-to-tiktok-for-business-mcp-server/v1.3)，上线其他环境须重新核实。

本次配置发布已额外生成项目归档并完成恢复校验，备份批次为 `/var/backups/tt-ada-staging/20260912T070256Z/`；日常备份脚本仍不自动打包项目，每次部署必须继续执行前述完整备份步骤。

### 每次部署必须执行的限流检查

完整含义、可复制配置及交付要求见 [调用额度配置与交付门槛](deployment.md#调用额度配置与交付门槛)。不要只参考上面的历史配置值；每次发布都核对 `/etc/tt-ada-staging/app.env` 和 API/Worker/Beat 单元的 EnvironmentFile。修改前完成完整备份，修改后按本手册排空并重启受影响服务，不能只改文件而不让进程重新加载。

以下在测试服务器以 root 执行，只校验实际私有配置，不输出配置值、不调用 TikTok：

```bash
set -a
. /etc/tt-ada-staging/app.env
set +a
cd /opt/tt-ada-staging/current/backend
runuser -u tt-ada -- /opt/tt-ada-staging/current/.venv/bin/python - <<'PYCODE'
from app.jobs.admission import admission_policy
for operation in ("protocol.initialize", "protocol.list_tools", "accounts.list_bcs", "auth_refresh", "materials.upload_video_file", "materials.upload_video_url"):
    policy = admission_policy(operation)
    minimum = 905000 if operation.startswith("materials.upload_video_") else 50000
    assert policy.lease_ms > minimum, "调用租约不足以覆盖请求期限"
print("PASS: 调用额度配置与租约校验")
PYCODE
```

检查通过后还须记录服务重启及配置加载结果；用户授权后验证候选 BC 列表、明确绑定及账户发现。没有用户授权时记录“部署及配置完成，真实读取待验”，不能写“整个 MCP 已可用”。本检查为手册中的人工发布步骤，尚未自动集成到服务器启动脚本。
