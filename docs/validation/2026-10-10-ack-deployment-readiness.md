# 2026-10-10 阿里云新加坡 ACK 目标纠正与验证

## 目标与结果

用户提供 `/Users/yaotingfeng/Downloads/singaporeconfig` 后，确认生产目标为 ACK `c19dbd82590a34b9f87cebb8c1a858796`，地域 `ap-southeast-1`，namespace `prod`。此前腾讯云 TKE 的网络超时不代表新目标不可达；旧入口 IP、CBS 存储和镜像拉取免凭据约定不再用于本次发布。

- 新 kubeconfig 成功连接；7 个节点 Ready，服务端 `v1.36.2-aliyun.1`。客户端 1.36.2 已下载并核对官方 SHA256，通过 `.tools/ack-bin/kubectl` 使用，不覆盖全局 kubeconfig。
- 内网 PostgreSQL `192.168.1.22:5432`：以用户提供的账号密码连入管理库 `postgres`，强制只读事务，执行当前数据库/用户/版本查询成功，返回服务端 18.3。没有指定、创建或迁移 TK-ADA 业务库；SSLRequest 返回 N，实例当前未提供该连接的 PostgreSQL TLS。
- 内网 Redis `192.168.3.251:6379`：凭据认证、PING 成功；没有读写业务键或选择迁入库。两个外网端点连接超时，运行配置选用已验证的内网主机。
- 共享入口为 `prod/prod-https`，IngressClass `alb`，地址 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`。集群内以 `tkada.galaxyshortdrama.com` 为 SNI、使用默认可信 CA 校验 TLS 成功，证书含 `*.galaxyshortdrama.com` 与根域。无需新申请证书，但新 host 的 DNS、TLS 声明与路由尚未配置。

## 部署适配

- Ingress 脚本改为操作 `prod-https`，保留乐观锁、只追加自身 rule、全量回读与 TLS host 覆盖检查。
- Beat PVC 改用 `alicloud-disk-topology-alltype` / 20 GiB，`WaitForFirstConsumer` 避免提前绑定到错误可用区。未实际创建 PVC，因此未声明云盘供应或绑定已验收。
- 默认 ACR 拉取凭据无法拉取 TCR 镜像，实际返回 insufficient_scope。已创建本项目 `tiktok-ad-automation-registry` Secret，九角色与迁移 Job 显式引用。添加凭据后，正式 SHA 镜像 `0ecccb9d6e8803509651b11c3c55a941cd4dd87c` 拉取并运行成功。
- 部署前检查该 registry Secret 的项目归属、类型和数据字段；不读取/修改其他项目 Secret。运行环境和 MCP Secret 尚未发布。

## 验证与操作范围

- 4 项构建 + 21 项部署行为测试通过，覆盖 ACK 入口名称、缺少拉取凭据时禁止写入及原有域名/归属门禁。
- 13 个对象通过本地 Kubernetes 1.34 通用 API 严格 schema，以及 ACK 1.36.2 真实服务端 dry-run；没有启动九个业务角色或迁移 Job。
- Ruff、格式、compileall、差异检查通过。Pod 探针仅做协议、认证和 TLS 验证，没有运行广告任务、业务消费者或数据迁移。
- 所有本轮临时探针 Pod 和临时数据库凭据 Secret 按 UID 核实后清理；只保留后续部署所需的本项目镜像拉取 Secret。测试服未停止。
- 原始证据在 Git 忽略的 `.runtime/company-production-20261010/ack/`：`network-check.json`、`authentication-check.json`、`server-dry-run.log` 及资源 UID 记录。凭据文件 0600，目录 0700，不提交。

## 剩余发布条件

后续已落实 PostgreSQL `tkada`；Redis DB 15 经撤回后，用户明确确认使用，已重新登记并验证，见末节。用户随后确认运维已完成公司侧域名解析；Service、TLS host 与入口 rule 已随后配置；还需验证域名实际访问，登记 TikTok API/MCP 回调；R2 CORS 已随后配置并通过新旧 origin 预检。完成一致性备份及恢复验证后再停旧消费者并迁入生产；认证和空间创建不代表业务数据恢复或完整上线验收完成。

## 后续：自行准备项目数据库空间

用户追问实例是否为空、能否自行创建，已在既有生产准备授权下完成以下操作：

- 只读查询显示 PostgreSQL 原有 6 个非模板数据库，没有 `tkada`；Redis DB 0 有 39 个键，不能称两个实例全空。未读取其他项目业务表数据、Redis 键名或键值。
- `gzjunbo` 的通用角色字段 `rolcreatedb=false`，但实际执行 `CREATE DATABASE tkada OWNER gzjunbo` 成功。此前仅凭字段判断不能建库不准确，以该云实例实际执行结果为准，未使用其他账号或提权。
- 连接新 `tkada` 后确认所有者为 `gzjunbo`，当前账号拥有 public schema CREATE 权限。非系统目录下仅有云平台扩展自带的 `cron.job`、`cron.job_run_details`（所有者 `polardb_admin`）；无 TK-ADA 业务表。保留平台扩展表，未直接修改表结构或执行 Alembic。
- Redis 实例为非 Cluster 模式，配置 256 个预设逻辑库（0–255）。DB 15 的 DBSIZE 为 0 后，使用原子 Lua 再次检查空库并创建本项目 `tkada:deployment:allocation` 标记；回读匹配，库内键数为 1。仅登记现有空逻辑库，无新 Redis 实例，无业务队列，没有修改 DB 0。
- 原来不存在 TK-ADA 目标库，目标业务备份不适用；保留创建前目录/键数证据。测试服仍未停写或迁移，最终一致性备份与恢复检查照常执行。恢复 Redis DB 15 时须识别本项目标记并保留/明确处理，不能把整个共享实例当成可覆盖卷。
- 私有 `infrastructure.json` 和 `runtime.env.incomplete` 已登记 `tkada`、DB 15 和内网 URL，密码正确 URL 编码，0600；未将配置发布到应用 Secret。原始查询/创建/回读证据在 `.runtime/company-production-20261010/ack/tiktok-ad-automation-*.result.json`。
- 本轮所有临时 Pod 和临时凭据 Secret 已按 UID 核对清理；仅保留先前的项目 registry Secret。

## 更正：撤回未经公司分配确认的 Redis DB 15 登记

用户明确关心公司共享实例中的编号分配和其他项目影响。此前把空 DB 15 直接登记给本项目不够严谨：空库可能已被其他项目预留，自定义标记也不是实例级分配或访问隔离。

- 使用 Lua 原子比较 `tkada:deployment:allocation` 的完整值，仅在与本轮写入证据一致时删除；实际删除成功，随后 DBSIZE 为 0。没有读取、删除或修改其他业务键，没有使用 FLUSHDB/FLUSHALL。
- 私有配置的 Redis database 改为 null、REDIS_URL 清空，状态为 pending_company_assignment；PostgreSQL `tkada` 保留。
- Redis DB 15 从未迁入业务数据或启动本项目消费者。清理探针和临时凭据 Secret 均已删除；证据为 `ack/tiktok-ad-automation-redis-unreserve-20261010.result.json`。
- 编号须由公司运维核对分配记录并登记。各逻辑库键空间分开，但实例内存、CPU 和实例级操作共享；不能把不同编号视为独立 Redis 实例。

## 后续：用户明确确认使用 Redis DB 15

用户回复“可以，就用15”，授权使用该编号；无需再次询问编号分配。本次按此决定执行，不将空库检查本身解释为公司分配证明。

- 在指定 ACK / prod 内运行本项目临时探针，Lua 原子确认 DB 15 无键后写入 `tkada:deployment:allocation`；结果 `allocated=true`、`marker_verified=true`、`key_count_after=1`。
- 私有 `infrastructure.json` 已恢复 database=15、allocation_status=user_confirmed 及标记键；`runtime.env.incomplete` 已填入密码正确 URL 编码的内网 Redis `/15` URL。回读验证编号、主机、密码编码与两文件 0600 权限通过，未输出凭据。
- 原始证据：`.runtime/company-production-20261010/ack/tiktok-ad-automation-redis-confirm-20261010.result.json`；临时 Pod 和凭据 Secret 已按 UID 核对后删除。
- 标记仅记录 TK-ADA 用途，不构成 ACL 或资源隔离。没有操作其他库、迁入业务数据或启动消费者；源测试服保持运行。目标应用运行 Secret 尚未创建，剩余发布条件见上文。

## 后续：公司侧域名解析配置确认与实际访问核验

- 用户明确说明运维已将公司域名 `tkada.galaxyshortdrama.com` 解析到新加坡集群。按此记录为已配置，不再将公共 DNS 无记录解释为运维未配置，不要求用户重复申请域名。
- 本轮公共 DNS 查询返回 NXDOMAIN；本机普通 DNS 返回代理地址，不能作为公司解析是否配置的结论。随后在指定 ACK / prod 新建无业务凭据的本项目临时 Pod，通过集群默认 DNS 实测：应用域名返回 Name or service not known，ALB 域名正常解析，HTTPS 尚不能通过域名访问。公司 DNS 的生效范围、缓存及实际客户端访问仍待核验，不能声称域名访问已通过。
- 只读核对 `prod/prod-https`，当前没有本项目 host rule 或匹配的 TLS host 声明；后续在应用 Service 准备后按部署流程追加本项目入口。
- 私有证据为 `ack/company-domain-check.json`。临时 Pod 已按 UID 核实后清理；没有启动业务消费者。

## 后续：按用户授权配置正式域名入口

- 用户要求“那你配置一下”。本次只配置入口：全 namespace 查重后创建 `prod/tiktok-ad-automation` ClusterIP Service，selector 为 app=tiktok-ad-automation、role=api，port/targetPort=80，与仓库正式清单一致。
- 在 `prod/prod-https` 追加本项目 TLS 条目，引用原有 `galaxyshortdrama.com` 证书；未读取或修改证书 Secret。TLS patch 使用 resourceVersion 比较，回读确认仅追加本项目条目。
- 随后调用现有 `patch-public-ingress.sh`，核对 Service 归属后追加 host=`tkada.galaxyshortdrama.com`、path=/、Prefix、backend=`tiktok-ad-automation:80`。全量回读确认原有全部 rules/TLS 条目保持一致，控制器事件报告 SuccessfullyReconciled。
- 变更前共享 Ingress JSON 已独立保存并核对 SHA-256；Service/TLS patch 先通过服务端 dry-run。首次入口不存在旧项目 Service、应用运行文件、构建产物或私有证书，因此这些旧应用备份项不适用；本次没有应用发布、数据迁移或运行配置变更，不切停源实例，最终迁移的完整备份要求仍保留。回退须按届时资源版本只撤销本项目新增条目，不能整份覆盖共享入口。
- 指定 ACK ALB 地址并携带应用域名 SNI/Host 的集群内真实 HTTPS 检查通过默认 CA 与主机名校验，证书含 `*.galaxyshortdrama.com`；HTTP 返回 503。正式 Service 的 EndpointSlice 无后端实例，与生产应用尚未启动一致，不能报告登录页或业务已可用。本地直连 TLS 失败，以上成功证据来自集群探针。
- 同一探针通过集群默认 DNS 仍无法解析应用域名；该结果单独保留，不将指定 ALB 地址的 HTTPS 验证视为 DNS 已生效。临时探针已清理。
- 私有证据：`ack/ingress-before-tkada-route.json`、`ingress-after-tkada-route.json`、`tkada-service-created.json`、`ingress-route-result.json`、`ingress-https-verification.json`。正式部署应复用现有 Service 和入口，不再重复追加域名。

## 后续：清理测试服磁盘、补齐 R2 CORS 和验证备份恢复

- 磁盘清理明确授权来自用户“把一些什么备份、数据库备份，还有项目的备份文件给删掉”。限定删除 10 月 5–7 日的 25 个旧备份目录，保留 10 月 8 日所有 5 个目录，且删除前验证完整备份 `20261008T021306Z`。私有精确清单为 `ack/staging-backup-cleanup.json`；释放 3,230,224,384 字节，根盘从 100% 降至 85%。
- 清理前 PostgreSQL 报 No space left on device，Redis 最近 RDB 失败；清理后 PostgreSQL 查询成功，RDB/AOF 最近写入均 ok，9 服务 active、健康 200、备份 timer active。没有清理当前程序、数据库、Redis 状态或素材。
- R2 S3 凭据读取 CORS 返回 AccessDenied；通过浏览器既有 Google 登录流程进入 Cloudflare，并核对账号与配置中 R2 endpoint 匹配后，在原桶 CORS 中仅追加 `https://tkada.galaxyshortdrama.com`。保留旧 origin、PUT/GET/HEAD、AllowedHeaders、ExposeHeaders 与 900 秒 MaxAge。保存后 UI 回读成功，两个 origin 的签名 URL OPTIONS 均返回 204、正确 Allow-Origin 及 PUT/GET/HEAD；未上传测试对象。证据为 `r2-cors-before.json`、`r2-cors-verification.json`。
- 该既有桶 Public Development URL 已开启，本轮没有修改其公开状态。CORS 验证只证明浏览器跨域响应，不证明桶私有或生产完整上传已验收。
- 在线数据库备份 157,252,972 字节，独立程序/构建归档 6,269,368 字节，配置/证书归档 19,132 字节；Redis RDB 2,194 字节。全部仅存私有 `ack/rehearsal-source/`，数据库/归档核对 SHA-256，项目与配置恢复到独立目录，前端构建位于 backend/app/frontend。
- 使用独立内部 Docker 网络的 PostgreSQL 18、Redis 8 演练恢复；数据库 136 表可读取，4 个凭据 envelope 解密验证通过。候选镜像 e01da729 执行 Alembic upgrade head 成功，head=0019_scene_budget_page、全部表行数与凭据校验前后一致。Redis RDB checksum 成功、恢复 7 键。未运行应用消费者，按 ID 删除本轮容器/匿名卷/内部网络；源服务器临时 RDB 经校验后删除，本地副本保留。
- 演练时源服务仍在线，因此上述备份不是一致性切换批次，不能直接用于双端启动。最终停写备份和目标恢复尚未执行。
- 回调真实卡点：浏览器当前 TikTok API 管理账号只展示审核拒绝且无有效 ID 的应用，无法修改运行配置所用应用；原 MCP 完整动态注册响应没有客户端管理 URI/令牌，只登记测试回调。代码刷新检查历史凭据 client_id 必须等于注册 client_id，直接换新客户端会破坏续期；未伪造新回调登记、未重新授权或刷新令牌。已询问用户现有 API 应用的管理账号，旧测试服继续运行。
