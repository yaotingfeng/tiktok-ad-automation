# 新加坡测试环境（无 Docker）

配置文件的统一命名、初始化与定位见 [环境配置入口](../../config/README.md)。项目内私有准备文件为 `.env.staging`，服务器仍加载 `/etc/tt-ada-staging/app.env`；复制文件不代表进程已重载。

## 目标与授权

- 用户于 2026-09-10 授权在 `137.220.150.31` 安装缺失依赖并部署项目，明确不使用 Docker；SSH 端口 `22211`，主机密钥更新已获确认。
- 本环境是独立测试环境，不是骏伯生产环境。首次只读检查：Ubuntu 24.04.1 x86_64、约 2 GiB 内存、20 GiB 系统盘（17 GiB 可用）；只有 SSH 和系统服务，无已有业务与数据库。
- SSH 密码、应用密钥、管理员密码及运行环境文件不进 Git。部署仅传输固定提交的跟踪文件，不复制本地 `.env`、数据库、素材或真实集成凭据。

## 部署方案

- 首次部署、新增开关及普通升级都执行[功能开关清单与发布确认](deployment.md#功能开关清单与发布确认)。首次/新增项先准备具体值和影响请用户确认；已有确认沿用。检查服务器配置及 API/Worker/Beat 实际值，不能由模板 false 重置已启用功能。

- 目录 `/opt/tt-ada-staging/releases/<Git SHA>`；`current` 指向正在运行的版本。
- 私有配置 `/etc/tt-ada-staging/app.env`；应用服务使用独立 `tt-ada` 系统用户。
- 安装 Python 3.14 / uv、Bun 1.4.2、PostgreSQL 18、Redis 8、Nginx；依赖按仓库锁文件冻结安装。
- PostgreSQL 和 Redis 只监听 loopback；Nginx 提供测试入口，API 仅监听 `127.0.0.1:18000`。域名/TLS 状态以本次验收记录为准。
- systemd 管理 API、Linux prefork Worker（小内存主机先使用 2 个进程）与唯一 Beat；禁止 API/代理访问日志采集授权参数。Beat 状态保存在 `/var/lib/tt-ada-staging`。
- 有界流水线版本使用 `deploy/staging-worker.service`（resources，prefork 1）、`deploy/staging-results.service`（resource-results，prefork 1）、`deploy/staging-builds.service`（builds，prefork 1）及 `deploy/staging-control.service`（control，prefork 1）。准备与结果有各自保留的执行槽，素材活跃槽合计仍为2；四个 Worker 加 API、Beat 共六服务十进程，必须统一版本/私有配置并全部覆盖排空、备份、恢复、启动和 ping。首次安装 results 单元前先按旧五服务正常排空。旧队列中的 prepare_cover 由正式消费者按原 ID/参数重试到 resources，不执行运维队列搬移。单提交最多10个合格组合活跃，完成或明确阻断自动补位；不扩大共享上游额度。发布后核对内存/交换区，并分别验准备、核验和广告回读。
- 全新空库通过 Alembic 迁移到固定提交的 head，再初始化管理员。缺少 TikTok/R2/版权方配置时保持未配置，素材导入/清理开关关闭。
- 后续升级先停止接收写入，停止 Beat 并正常排空 Worker，按通用发布手册备份数据库、Redis、项目文件/构建产物及私有配置（无迁移也必须备份）；迁移成功后切换同版本 API/Worker/Beat。不可通过直接改表或删除数据修复迁移。
- 验证前端构建、Alembic head、登录和受保护接口、入口检查、Redis/数据库、Worker ping、Beat/outbox；外部真实联调单独验收。

## 当前实例与操作

- 2026-09-18 当前运行 `b9d6982f08cc6be6da90fd6d4e9a5fca8375c1b3`：共享后/来源自身目标MID正向回读。备份 `20260917T183303Z` 五归档/1259文件/21表/998响应独立恢复一致；六服务十进程/双身份隔离及服务器5项回归23.34秒通过，测试角色恢复。配置/额度/head/前端不变；18:35UTC仍2/180、MATERIAL239、BUILD封面509 READY，不能称整批完成。以下为历史记录。
- `c115492f5a754bd32aab5cfe4e6bff7bce220c6b`：封面重叠分页换边界补齐、失败步骤接回已恢复依赖。备份 `20260917T181213Z` 五归档/1259文件/21表/986响应独立恢复一致；六服务十进程/双身份隔离及服务器8项回归通过。原配置/额度/head/前端不变；18:16UTC仍2/180，封面开始继续完成，非整批创建成功。

- 2026-09-18 当前运行 `59686b7c8d6529d71beb072245441460a5860635`：历史未发送封面批次整体准入与自动唤醒。备份 `20260917T175307Z` 五归档/1259文件/21表/973响应恢复一致；六服务十进程、四Worker、双身份隔离及服务器5项回归通过。原配置/额度/head/前端不变；17:55UTC仍2/180，持续验收未完成。以下为历史记录。

- 2026-09-18 当前运行 `7b0234010ab9eb81097ac02fad7961f894b4ef8f`：完整封面候选查询物化并集合反连接，实际执行16.664ms、无JIT。备份 `20260917T172954Z` 五归档/1258文件/21表/960响应独立恢复一致，六服务十进程/四Worker/双身份隔离及服务器两项回归通过。配置/额度/head/前端不变；原批次17:45UTC仍2/180，继续修复历史未发送批次绕过准入，不宣称整批完成。以下为历史记录。

- 2026-09-18 最新运行 `23e87209cb357d64b41ad000f3b2ab7a5f81baba`：显式RETRY包括等待步骤关联的确定未发送BLOCKED封面。完整备份 `20260917T171644Z` 五归档/1258文件/21表/954响应恢复一致；六服务十进程实际版本与原配置通过，新六项恢复回归通过，原产品恢复完成调度416项，不代表180组合完成。下一步正在处理完整候选SQL的JIT耗时，见持续验证。以下为历史记录。

- 2026-09-18 最新运行 `84d578e6daeb8d4688911d6d455dbfc1a27f9c6b`：10组合有界并行、封面规划/领取统一准入、准备与结果各1槽。完整备份 `20260917T170246Z` 五归档/1252文件/21表/951历史响应独立恢复一致；六服务十进程、四Worker及双身份隔离验收通过，原配置/额度/head/前端不变。原提交通过产品恢复API，RETRY完成调度1项、RECONCILE完成调度13项；17:06 UTC业务仍2/180，不以调度完成冒充创建成功。服务器Linux隔离回归进行中。以下为历史记录。

- 2026-09-18 最新运行 `6fd9c5e2897e11dab8078e830ad816856d54c0d7`：变化封面图库最多两次从首页重新核查，持续变化仍阻断且不重发已发送批次。完整备份 `20260917T162520Z` 五归档/1252 文件/21 表/950 历史独立恢复；五服务九进程、三 Worker、双身份隔离和服务器 7 项回归通过。配置、额度、并发、head 和前端不变；16:26 UTC 整批仍 2/180，持续验收未完成。以下为历史记录。

- 2026-09-18 最新运行 `2467c7f8dd5a3b371c55fd05b37372f3b9ed9961`：来源封面持久等待和未来未发送封面准入。完整备份 `20260917T161519Z` 五归档/1251 文件/21 表/950 历史独立恢复一致；五服务九进程、三 Worker、服务器隔离 22 项回归通过。配置、额度、head、并发及前端不变；16:18 UTC 完整仍 2/180，继续业务验收，不能当成整批完成。以下为历史记录。

- 2026-09-17 最新运行 `f0698e38b763353ebf0824896f060780a7f4a808`：正式素材调度窗口、依赖落定唤醒和旧消息提前退出。完整备份 `20260917T154301Z` 的五归档/1248 文件/21 表/950 加密响应独立恢复一致；五服务九进程、三 Worker、双身份隔离通过，配置、额度、head、并发、新鲜度和前端不变。15:47 UTC 完整仍 2/180，正在持续验收，不代表整批完成。见 [记录](../validation/2026-09-17-build-pipeline-liveness.md)。以下为历史发布记录。

- 2026-09-17 最新运行 `5c16ce11c84b37cd3f86df9a31451a4bdde396b1`：跨共享批次核验/收口统一锁序。完整备份 `20260917T144302Z`、五归档/1247文件/21表/949响应恢复一致；五服务九进程、三Worker、双身份隔离、索引1项/Linux3项/服务器并发2项通过；配置、额度、head和前端不变。14:51 UTC完整2/180、AD4/360，素材未知结果、封面分页与积压未解决，非整批验收成功。以下为历史发布记录。

- 2026-09-17 运行 `2ddeff79d53764ce6c971459038097e6d56b94ab`：广告准备统一素材锁序。完整备份 `20260917T142416Z`、五归档/21表/949响应独立恢复、五服务九进程、双身份登录隔离、索引1项/Linux3项及真实28素材组锁序回滚验证通过。14:31 UTC原批次完整2/180、AD4/360，整批尚未完成。

- 2026-09-17 运行 `3ff86a3b6c285a03b90e962944aec37d23307c44`：queued已有VID复核纳入50项批读，队列短索引扫描预算3秒。完整备份 `20260917T132835Z`、五归档/21表/948归档独立恢复、五服务九进程/三Worker/双身份隔离、服务器索引1项及Linux队列3项通过；配置、额度、head和前端不变。13:41 UTC原批次完整仍1/180，MATERIAL146，持续恢复与监控，非整批验收成功。

- 2026-09-17 最新运行 `5d5b6550975e0cc81cc2617d371458b0f14e26f5`：封面规划限定20素材×10账户窗口，重叠分页最多三轮补齐唯一库存后才允许负向结论。完整备份 `20260917T131020Z`、21表/948加密归档独立恢复、五服务九进程/双身份登录隔离、服务器隔离回归3项通过；配置、额度、前端及数据库head不变。原批次完整单元仍1/180，不能称为业务恢复完成，见[持续验收](../validation/2026-09-17-material-queue-recovery.md)。以下为历史发布记录。

- 2026-09-17 最新运行 `b6462b09b8a783755c6bb1573fb0b0a66ad7480a`：已知封面核验只在每次短事务复用共同授权，保留逐项身份及每次 HTTP 的重新检查。备份 `20260917T124524Z`、21 表/948 归档独立恢复及五服务九进程/Web 隔离通过；服务器隔离回归 5 项、Linux 队列 3 项通过。没有配置/额度/迁移/前端变化，原整批仍在恢复，不把一个单元成功当成整批完成，见[持续验收](../validation/2026-09-17-material-queue-recovery.md)。

- 2026-09-17 当前运行版本 `218c64f5a650027c596495a08614545860eca8b1`：修复深队列快照漏扫导致的重复补投，并允许多个单项原生共享合并核实。完整备份 `20260917T121241Z`、独立恢复、五服务九进程/三消费者及 Linux 3 项、登录隔离等验收通过；配置/额度/前端不变。原批次恢复进行中，尚未整批创建完成，见[持续验收](../validation/2026-09-17-material-queue-recovery.md)。以下为历史记录。

- 2026-09-17 当前运行版本为 `d22bde343ce909d6142b554eec393aaf6d192563`：排队消息防重复、素材结果独立队列、批量核验提速及恢复查询索引/数据库期限。完整备份 `20260917T091246Z` 独立恢复、五服务九进程/三个 Worker/Linux 队列及登录隔离通过。配置、数据库 head 和前端不变；素材及封面开始持续推进，整批广告尚待完成。详见[本轮验收](../validation/2026-09-17-material-queue-recovery.md)。以下为历史记录。

- 2026-09-17 当前运行版本为 `c85cca1cfd4905e0430ecbf530c2602672269ffd`：共享后 MID 查询按平台 20 条上限分批，保留此前跨 BC 来源绑定修复。55 项回归、完整备份 `20260917T080601Z` 及独立恢复、1,240 文件/21 表/946 份响应核验、五服务九进程及三 Worker 验收通过；数据库 head、原配置、额度、并发及前端不变。CTA 180/180，但素材与广告尚未整批完成；持续观察见[修复与恢复记录](../validation/2026-09-17-cross-bc-material-route.md)。以下为历史记录。

- 2026-09-17 先前版本 `9599fd7b2f8915b3d24125a1af6dbbf6733c62e5` 修复跨 BC 素材来源连接被目标 BC 绑定误拦截。完整备份 `20260917T065027Z`、独立 PG/Redis/项目/配置恢复、21 表及 944 份加密响应历史核验通过；head 为 `material_route_scope`。五服务九进程、三 Worker、登录隔离与前端资源验收通过，原配置及开关保持。已登记原批次幂等 RETRY。

- 2026-09-17 当前运行版本为 `06bd724b15bddf62f96e5a8a81d658a52b7f8046`：单条不可用素材按账户隔离、保留其他有效素材，以及预算指数显示修复。完整备份 `20260917T062358Z`、隔离恢复/迁移演练、18 表/920 份加密归档、五服务九进程、三 Worker、登录隔离与新接口验收通过；head 为 `preview_skipped_materials`，配置不变。旧冻结预览须由用户返回调整后重新生成；存在排除记录时不能只切旧代码回退。见[发布记录](../validation/2026-09-17-preview-partial-materials.md)。以下为历史记录。

- 2026-09-17 当前运行版本为 `219f51e054789ecbff0e2078b3c9fb84a85c5fa4`：冻结预览短批次公共读取复用、末尾新鲜复核，以及加载动画/阶段/真实进度/耗时提示。完整备份 `20260917T050329Z`、隔离恢复、18 表/845 份响应归档、五服务九进程、三 Worker、登录隔离与进度接口验收通过；配置、依赖、数据库 head 不变，见[发布记录](../validation/2026-09-17-preview-batch-progress.md)。以下为历史记录。

- 2026-09-17 当前运行版本为 `badccc2fa6c750f3922524f62aaca72a7dfb3487`：预览小程序前置校验、组合进度显示和素材检查提速。完整备份 `20260917T033621Z` 及隔离恢复、五服务/九进程、三 Worker、登录隔离与浏览器验收通过；配置、依赖、数据库 head 不变，见[发布记录](../validation/2026-09-17-preview-prerequisites-performance.md)。以下为历史记录。

- 2026-09-17 当前运行版本更新为 `1d7ab27001ea5d1a7d7fc439500fce031649e08e`：草稿本地素材有界批量匹配，取消逐剧固定等待。完整备份 `20260917T025246Z` 与恢复、五服务配置及健康验收通过，数据库与配置不变，见[发布记录](../validation/2026-09-17-material-matching-batch.md)。以下版本条目为历史记录。

- 2026-09-17 当前运行版本更新为 `9a9fea45b54a942733563694fee403368d67f94f`：网眼返回标题首尾空白修复；完整备份 `20260917T023736Z`、五服务实际配置和三部剧真实读取验收通过。数据库 head、功能开关与其他配置保持不变，见[发布验收](../validation/2026-09-17-wangyan-title-whitespace.md)。下方旧运行提交为历史基线。

- 2026-09-13 用户授权开启自动原件清理：三服务实际加载 `MATERIAL_CLEANUP_ENABLED=true`；已有 24 个原件完成删除回读，新批次 23 个账户素材均保持可用，详见 [清理验收记录](../validation/2026-09-13-staging-original-cleanup.md)。初始化时关闭开关的说明不代表当前运行配置。
- 运行提交：`6e7045a167509f312609a254bbe733444d6fd67f`；授权卡片现位于“授权管理”Tab，空名称用通道及编号尾号区分，见[授权页修正验收](../validation/2026-09-16-staging-authorization-tab.md)。已纳入 BC 授权统一入口、租户素材库、单 BC 首次转存及恢复修复，见[本次发布验收](../validation/2026-09-16-staging-tenant-library.md)。素材上传仍使用顶栏 BC，搭建可从租户库选材并自动跨 BC 准备，同 BC 后续目标复用来源。来源 BC `7678608005688066065`、目标 BC `7683817908149272592` 的真实读取/准备前提已核实，实际搭建由用户手动测试。数据库 head 为 `material_seed_generations`；最新完整备份 `20260916T102006Z` 已完成独立恢复及 501 份加密响应/18 表历史检查。已确认权限持续可用、租户/全局原件预算 `0`、入库/清理开启、调用额度和媒体白名单保持不变。旧版本及备份保留，生产环境未变更。
- 入口：`https://137.220.150.31`，80 跳转 443。已签发受信任 IP 证书，非自签名证书；无需域名即可访问本次测试入口。真实 TikTok 回调/App 接入另行配置并验收。
- Python `3.14.2` / uv `0.9.26` / Bun `1.4.2` / PostgreSQL `18.6` / Redis `8.10.1` / Certbot `5.8.0`。另已安装 Nginx、FFmpeg 和基础编译依赖；未安装 Docker。
- 数据库 `tt_ada_staging`，角色 `tt_ada`；应用角色无 CREATEDB 或超级用户权限。回归测试使用单独测试角色与 `tt_ada_acceptance_test`、Redis DB 14/15，业务使用 Redis DB 0。
- `/etc/tt-ada-staging/app.env` 为 root:tt-ada、0640；管理员登录信息位于服务器 `/root/tt-ada-staging-login.txt`。本地私有副本 `.runtime/singapore-staging/login.txt` 为 0600，已被 Git 忽略。
- API、素材 Worker、广告 Worker、控制 Worker、Beat 单元名依次为 `tt-ada-staging-api`、`tt-ada-staging-worker`、`tt-ada-staging-builds`、`tt-ada-staging-control`、`tt-ada-staging-beat`。服务使用 `ProtectSystem=strict`、独立临时目录及 960 秒正常停止期限。Worker 节点名中 Celery 的 `%h` 在 systemd 文件里必须写为 `%%h`，避免被 systemd 展开为 root 家目录。
- Nginx 独立站点 `/etc/nginx/sites-available/tt-ada-staging`。Redis 开启 AOF，淘汰策略 `noeviction`；PostgreSQL、Redis、API 均仅本机监听。

```bash
systemctl status tt-ada-staging-api tt-ada-staging-worker tt-ada-staging-builds tt-ada-staging-control tt-ada-staging-beat
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

## 跨账户分发配置验收

2026-09-13 真实目标场景已读取通过。实际 TikTok 视频响应的 HTTPS 预览主机为 `v16-tt4b.tiktokcdn.com` 和 `v19-tt4b.tiktokcdn.com`，均从真实视频返回确认，公网 DNS 已核实；当前 `MATERIAL_REMOTE_MEDIA_HOSTS` 已配置为这两个精确主机，并在完整备份和恢复验证后重启验证所有进程。HTTP 封面 URL 只回传 TikTok 图片导入服务，不由应用下载，不混入 HTTPS 视频中转白名单。
