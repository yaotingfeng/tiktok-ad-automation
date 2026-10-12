# TK-ADA 公司生产首发与迁移

> **当前发布与备份**：使用[本地一键发布](../../deploy/release/README.md)，无需Jenkins或备份云盘。最新恢复验证通过的项目备份为本机 `.runtime/local-release/backups/latest.tar`，仅保留最近一份；旧迁移归档路径仅为历史审计记录，不能作为当前恢复入口。

> **当前部署**：已迁至同一新加坡 ACK 的 `test` 命名空间，入口为 `test/test-https`。本机使用阿里云 ACR 公网地址推送，Pod 通过 VPC 地址拉取。原 `prod` 已停用，旧 Deployment 后续已被外部清理；仍用退役资源清单核对残留资源，禁止重启旧消费者。后文 prod/TCR 记录为迁移历史。验收见[ACR 与命名空间迁移记录](../validation/2026-10-10-acr-test-migration.md)。

> **当前生产入口**：用户指定改为 `https://tkada.shortclipflix.com`；该域名已真实解析到新加坡 ALB，现有 `*.shortclipflix.com` 证书验证通过。本次配置切换及验收见[域名切换记录](../validation/2026-10-10-shortclipflix-domain.md)。下文 galaxyshortdrama 的 DNS 问题为首次发布时的历史情况。

## 已确认的目标与授权（2026-10-10 更新）

用户已明确“公司集群就是生产环境，现在要部署到生产环境”，并要求“测试服的密钥、队列和授权要直接复制过去”。本次是新加坡测试实例向公司生产集群的迁移，覆盖此前“禁止复制测试密钥/授权”的默认规则；不再要求用户另找一套集群凭据。迁移不授权重复创建广告或绕过已有 UNKNOWN/冻结执行保护。2026-10-10 用户提供 `/Users/yaotingfeng/Downloads/singaporeconfig`，明确改用阿里云新加坡 ACK；原 skill 中的腾讯云 TKE 仅为此前误用的准备目标，后续不再向其部署。

| 项目 | 已核实值 |
| --- | --- |
| 集群 | 阿里云 ACK，新加坡 `ap-southeast-1`；集群 ID `c19dbd82590a34b9f87cebb8c1a858796` |
| context | `kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796` |
| 凭据来源 | 用户提供的 `/Users/yaotingfeng/Downloads/singaporeconfig`；私有副本 `.runtime/company-production-20261010/ack/kubeconfig`，0600，禁止入 Git |
| namespace | `test`，Active；生产用途，名称不代表改为测试环境 |
| 服务端版本 | `v1.36.2-aliyun.1`；本地使用校验过官方 SHA256 的 `.tools/bin/kubectl-1.36` |
| 应用资源前缀 | `tiktok-ad-automation`；2026-10-10 ACK 查重无同名业务 Deployment/Service |
| 镜像仓库 | 公网 `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<完整 SHA>`；集群将主机替换为 `sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com` |
| 仓库凭据 | 用户提供的新加坡 ACR 账号，私有 `ack/acr-test-review/registry.json`；禁止回显/提交 |
| 对外入口 | 现有 `test/test-https`，IngressClass `alb`；ALB 域名 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com` |
| 证书 | ALB 按域名自动发现现有证书；TLS 条目不设置 secretName，不读取共享证书 Secret |
| 生产域名 | `tkada.shortclipflix.com`；CNAME 已真实指向上述 ACK ALB；原 galaxyshortdrama 域名已不作为生产入口 |
| PostgreSQL 业务库 | `tkada`，2026-10-10 已用所提供账号创建，所有者 `gzjunbo`；已恢复源库 136 张业务表并验证 |
| Redis 逻辑库 | DB 15；用户明确确认使用，已重新检查空库、登记本项目标记并回读验证 |
| 应用端口 | API/容器/Service 均为 80 |
| 拓扑 | 1 API、7 个独立 Worker、1 Beat，使用同一版本及运行 Secret |
| 应用资源 | 合计申请 4.25 核 / 10.5 GiB，上限 10.5 核 / 21 GiB；不含 PostgreSQL、Redis 与发布瞬时资源 |
| 存储 | Beat 申请 20 GiB，使用已存在的 `alicloud-disk-topology-alltype` / `WaitForFirstConsumer`，随首个 Pod 可用区绑定 |
| 镜像拉取 | `test/tiktok-ad-automation-registry` 保存 VPC ACR 主机认证；所有 Pod 显式引用本项目 Secret |
| 配置落点 | `test/tiktok-ad-automation-runtime` 与 `test/tiktok-ad-automation-mcp-registration` Secret；已创建并验证九角色一致 |

ACK 已核实 7 个 Ready 节点，分布在新加坡四个可用区；`test` 未配置 ResourceQuota/LimitRange。节点容量不是可直接独占的剩余资源，部署仍须核对其他工作负载已申请量及实际调度结果。

## 迁移源核验（停写前快照）

- 来源：新加坡 `137.220.150.31` 的现有测试实例，不将源服务器改称生产。
- 实际运行版本：`8b877f4`，9 个应用 systemd 服务均 active。
- 生效配置：`/etc/tt-ada-staging/app.env`；本地旧 `.env.staging` 已落后于实际服务，不作为权威来源。
- 已只读获取实际运行配置及 MCP 注册文件，存放在 Git 忽略的 `.runtime/company-production-20261009/source/`，目录 0700、文件 0600；不在文档记录密钥值。
- 生产配置草稿在同目录 `prepared/runtime.env.incomplete`，已保留来源密钥并转换为 Kubernetes env-file 格式；这是此前目标未定时的历史草稿；当前草稿位置见下文 2026-10-10 记录。源注册 JSON 原样保留，未伪造新域名注册。
- 2026-10-09 队列快照：七条业务队列长度均为 0，Redis unacked 为 0。这只是当时快照，最终切换前仍须停止调度并重新核对。
- 已生效开关为导入 `true`、清理 `true`、周期同步 `false`；迁移方案保留这些现有值，不用模板值静默覆盖。
- PostgreSQL 与 Redis 当前均监听 `127.0.0.1`；直接复制这些 URL 到 Pod 会连接 Pod 自身，不能作为生产目标连接信息。

## 2026-10-10 新目标准备过程（迁移前快照）

用户已提供阿里云新加坡 PostgreSQL / Redis 地址及凭据，覆盖 skill 中公司固定 Redis 的默认值。凭据仅保存于 `.runtime/company-production-20261010/infrastructure.json`（0600），不写入文档、命令行参数或 Git。

| 服务 | 内网主机 | 外网主机 | 端口 |
| --- | --- | --- | --- |
| PostgreSQL | `pc-gs5ka8g997w7sv5ci.rwlb.singapore.rds.aliyuncs.com` | `singapore-pg-test.rwlb.singapore.rds.aliyuncs.com` | 5432 |
| Redis | `r-gs5lmf0pt8tic0r0t7.redis.singapore.rds.aliyuncs.com` | `r-gs5lmf0pt8tic0r0t7pd-test.redis.singapore.rds.aliyuncs.com` | 6379 |

域名已确定为 `tkada.galaxyshortdrama.com`。同日私有运行草稿 `runtime.env.incomplete` 保留原授权加密密钥、R2/API 密钥及三个开关；同步新 origin 与回调，但未伪造远端注册文件。用户随后要求自行创建项目空间：已创建 PostgreSQL `tkada`（所有者 `gzjunbo`），此前自行登记 Redis DB 15 的做法曾撤回；随后用户明确回复“可以，就用15”。据此重新原子检查 DB 15 为空并登记本项目标记，回读匹配、键数为 1。私有草稿已配置 PostgreSQL `tkada` 与 Redis `/15` 的内网 URL；尚未迁入业务数据。

当时待落实项（已完成部分及最新剩余项见顶部状态和正式发布记录）：

1. **PostgreSQL 与 Redis 编号已落实**：使用 `tkada` 项目库与用户明确确认的 Redis DB 15，REDIS_URL 已填写。Redis 的 0–255 编号为实例预设，本项目标记仅用于记录用途，不提供访问隔离或公司全局分配证明；实例 CPU、内存仍共享。不得修改实例库数或清空共享实例。
2. ACK 已用所提供凭据完成内网 PostgreSQL 只读认证（`postgres` 管理库，服务端 18.3）和 Redis AUTH/PING。外网地址连接超时，部署选用内网；已随后建好 PostgreSQL 项目库并按用户确认登记 Redis DB 15；目标恢复仍待执行，没有迁入业务数据。
3. 用户确认运维已完成公司侧域名解析至新加坡集群，不再要求重复配置 DNS；从实际使用网络核验解析与访问。本项目 Service、TLS 声明与路由已创建，控制器已成功同步；应用启动后再验证完整访问。现有通配证书已通过真实验证，无需新申请。不要再使用旧腾讯云入口 `134.175.255.190`。本项目只追加自身路由，不读取其他项目的证书 Secret。
4. 用户已在 API 应用登记生产回调，复测正常展示 TK-ADA 授权同意页。按用户直接处理 MCP 的授权，已通过官方动态注册接口创建生产客户端，HTTP 201 响应确认生产 MCP 回调；私有注册文件为 `ack/mcp-client-production.json`。新客户端 ID 与旧授权绑定不同，现有 3 份 MCP 连接切换后须重新授权；原注册及授权保持不变，新注册尚未应用到生产。R2 CORS 已追加生产 origin，新旧 origin 的真实 OPTIONS 均通过。完整 OAuth 收码/换码仍待应用部署及用户实际授权验证。
5. 最终停写窗口中的一致性备份、目标恢复验证和单侧消费者切换；条件未齐前继续保持测试服运行。

首次迁移的历史资料清单（当前域名和回调见顶部及文末）：

```text
项目：TK-ADA / tiktok-ad-automation
目标：阿里云新加坡 ACK c19dbd82590a34b9f87cebb8c1a858796，namespace prod
DNS：用户确认运维已配置 tkada.galaxyshortdrama.com 指向新加坡集群；目标入口为上述 ACK ALB，实际访问待验
HTTPS：共享入口 prod/prod-https 已追加本项目 TLS host 和 / 路由，原有站点配置保持一致
PostgreSQL：tkada（已创建，所有者 gzjunbo）；使用已验证内网主机
Redis：DB 15（用户已确认使用，已登记 TK-ADA 标记）；使用已验证内网主机，实例资源仍共享
网络：ACK 内网 PostgreSQL、Redis 已认证成功；不再按腾讯云跨云超时处理
TikTok API 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/callback
TikTok MCP 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/mcp/callback
R2 CORS：已添加 https://tkada.galaxyshortdrama.com，保留测试 origin，OPTIONS 验证通过
状态：九个应用服务已就绪；指定 ALB 的 HTTPS/登录验收通过，普通域名解析待运维核对，用户手动重新授权待验
```

2026-10-09 的 13 个部署对象已通过服务端 dry-run，不代表应用、磁盘绑定或网络验收。2026-10-10 此前腾讯云仅创建本项目前缀的临时网络探针，无密钥、无队列消费者，用于验证真实镜像拉取、调度和网络；探针结束后清理。旧目标的结果见当日腾讯云记录；新目标以 [ACK 验证记录](../validation/2026-10-10-ack-deployment-readiness.md) 为准。

## 执行顺序

1. 保留来源私密配置、注册材料和授权加密密钥；整理目标配置，只有网络地址、域名及挂载路径按目标替换。不要生成新的加密密钥导致历史授权不可读。
2. 完成项目相关验证，将已验证 `main` 推送公司 Gitea，构建带完整 SHA/revision 标签的 AMD64 镜像并推新加坡 ACR；保留本机用户未提交的 `AGENTS.md` 改动，使用干净发布副本。
3. 目标运行配置齐全后，离线容器检查真实配置，执行集群只读预检与服务端 dry-run，核对配额、调度和阿里云云盘。
4. 切换窗口停止新入口写入、旧 Beat 和正常排空 Worker，暂停备份 timer 并等待已开始备份结束。备份 PostgreSQL、Redis、独立源码/构建产物、私有配置、注册材料、证书与服务定义；校验和及隔离恢复通过后才进行目标恢复。
5. 迁移原授权记录时同步保留密钥、连接代数、冻结请求、幂等记录和 UNKNOWN 状态；队列与持久业务事实必须来自同一停写窗口。不得用空库加队列、或单独复制 `.env` 代替授权数据迁移。
6. 目标先恢复事实和队列并完成验证，再运行审查后的版本迁移及九角色部署；确认旧消费者已经停止后才让生产消费。若发现仍有未确认的真实广告任务，先列明具体任务状态与原授权再处理，不盲目重发。
7. 本项目 Ingress 规则已提前追加，正式部署时核对现有规则归属，不重复追加。验证证书、DNS、登录、租户、七 Worker、唯一 Beat、版本/配置一致，以及真实存储和 API/MCP 的读取能力。真实广告写入按具体业务授权验收。
8. 完成后恢复应恢复的备份 timer，记录新旧版本、镜像 digest、资源 UID、配置摘要、备份位置和恢复证据。旧消费者保持停止，避免双端消费；旧备份保留至回退窗口结束。

## 回退与当前状态

目标未具备恢复条件或检查失败时，保持来源实例原状。切换后的回退必须先停止生产消费者、核对新旧持久状态兼容性，再按一致性备份恢复；禁止仅 `rollout undo`、清空队列或覆盖共享 Redis 来恢复。

生产已完成数据恢复与九角色部署，旧源服务已停止并取消自启；禁止直接启动旧消费者。回退前先停止生产写入与消费者，再判断是否已有新增业务事实并制定一致恢复方案。当前新域名的真实 DNS、HTTPS 和登录已通过；新域名 API 应用回调登记及用户 OAuth 仍待完成，详见顶部域名切换记录。

## 2026-10-10 磁盘清理与迁移恢复演练

- 源测试服根分区曾为 100%，PostgreSQL 无法创建初始化文件，Redis 最近持久化失败。用户明确授权删除旧数据库/项目备份后，仅清理 `/var/backups/tt-ada-staging/` 中 10 月 5–7 日的 25 个备份目录，释放 3,230,224,384 字节（约 3.0 GiB）；10 月 8 日的 5 个备份目录全部保留。删除前核验保留完整备份 `20261008T021306Z` 的 SHA256SUMS、pg_restore 目录、Redis RDB 与项目/配置 tar。
- 清理后 PostgreSQL 查询恢复、Redis RDB/AOF 状态均为 ok、9 应用服务 active、健康接口 200，备份 timer 保持启用。没有删除运行数据库、Redis 持久文件、当前 release 或素材。
- 已将在线 PostgreSQL 自定义格式备份、Redis RDB、独立程序/前端构建归档、配置/证书归档保存到本地私有 `ack/rehearsal-source/`，并完成隔离恢复。136 张业务表可读取、4 份历史凭据可用原密钥解密；候选镜像 e01da729 的 Alembic upgrade head 通过，head=0019_scene_budget_page，迁移前后表行数及凭据检查结果一致。Redis RDB 校验通过并恢复 7 个键。
- 这是运行中各自采样的恢复演练，不是同一停写窗口的正式切换备份；最终迁移仍须按执行顺序重新冻结、排空并备份。演练使用无外网内部 Docker 网络，没有启动 API/Worker/Beat；演练容器及其匿名卷已按 ID 核对清理，本地归档保留。
- R2 使用既有 `junbo-manju-fb` 桶；控制台核验发现其 Public Development URL 原已开启，本轮未改变该共享桶公开状态，不能把它描述为已验证私有桶。CORS 只增加生产 origin，其他规则保持一致。正式私有存储要求与该现状需另行处理，不能擅自关闭可能仍供其他项目使用的公开入口。

## 2026-10-10 回调检查与待发布镜像

- 从干净发布副本同步公司 main，构建并推送 `783d3b836b61171153cf1863f4ecd7617bc3e7d6` 的 AMD64 镜像，digest=`sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`。后续文档提交不代表运行代码变化；正式发布须固定所选 SHA 并核实镜像 revision。
- 源库只读汇总仍有 1 个 ACTIVE API 连接与 3 个 ACTIVE MCP 连接。仅为数据库记录状态，未执行令牌刷新或广告调用来判断远端授权有效性。
- API App ID 为 `7690990985458761748`。当前浏览器账号直接访问应用管理详情显示“未找到应用程序”。同一 App ID 使用旧测试回调可显示 TK-ADA 同意页面；换生产回调则显示应用状态错误。这个对照表明不能把错误简单归因为应用整体停用，生产回调需要由该应用管理账号核对并登记。
- 原 MCP client_id 配生产回调也能显示前端同意页面，但未点击 Authorize，不能据此认定远端接受回调。已查官方自定义客户端文档，公开流程只有注册/授权/换码/刷新/撤销，未找到原客户端回调更新接口；完整原注册响应也无管理 URI/令牌。不伪造注册材料，不擅自换客户端破坏原 3 份授权续期。
- 当前真实配置离线预检明确失败于 `MCP registration does not allow the target callback`，未写入集群运行 Secret 或启动应用。已请用户切换到上述 App ID 的管理账号；保留管理详情页供继续操作，临时授权测试页均已关闭，没有提交同意、换码或刷新。

## 2026-10-10：API 回调复测与 MCP 生产客户端注册

- 用户确认已在 API 应用配置生产回调，并明确授权直接处理独立 MCP 客户端。API 使用同一 App ID 和生产回调复测，正常显示 TK-ADA 授权同意页，之前的应用状态错误消失。未点击同意；此项证明授权入口已接受配置，尚未证明生产回调收码与令牌交换。临时检查页已关闭。
- 通过官方 discovery 的 registration_endpoint 执行一次生产客户端动态注册，HTTP 201；平台原始响应明确返回生产 MCP redirect_uri、authorization_code/refresh_token 配置及新的 client_id。完整响应与严格五字段注册文件仅保存在私有 `ack/mcp-registration-production-response.json`、`ack/mcp-client-production.json`，权限 0600。没有伪造原客户端的登记，也没有修改测试服。
- 原注册文件 SHA-256 前后相同。代码刷新流程校验凭据中的 client_id 与当前注册一致，因此新注册不能直接续期旧客户端下的 3 份 MCP 授权；迁移数据可保留，但切换新客户端后必须重新授权。尚未替换运行 Secret、撤销旧授权或启动生产消费者。
- 私有证据 `ack/callback-readiness-retest.json`；API/MCP 完整授权测试及历史 MCP 重新授权仍待完成。
- 使用正式候选镜像 `783d3b836b61171153cf1863f4ecd7617bc3e7d6`、真实运行环境与新注册文件，`DRY_RUN=1` 和 `PREFLIGHT_ONLY=1` 均 exit=0；后者包含镜像 revision 校验、断网容器 Settings/注册配置检查及 ACK 资源归属/入口只读检查。证据 `ack/production-client-config-preflight.json`。未写集群运行配置，未迁移数据库或队列；配置预检通过不代表历史 MCP 授权迁移通过。

## 正式切换记录与恢复入口

- 用户明确“继续完成，重新授权手动测试”，授权生产采用新 MCP 客户端并完成其余迁移。2026-10-10 06:14:06 UTC 源九服务全部优雅停止；七队列和 unacked 均为零，旧 Worker 无 active/reserved/scheduled 任务。
- 完整冻结备份、原配置证书、源独立程序/前端构建、Redis RDB 与逐键逻辑快照位于私有 `.runtime/company-production-20261010/ack/cutover/`。该目录的 `manifest.json`、`backup-verified.json`、`target-restored.json` 分别记录清单、隔离恢复和生产恢复证据。
- 发布 SHA `783d3b836b61171153cf1863f4ecd7617bc3e7d6`，镜像 digest `sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`，head `0019_scene_budget_page`。实际注入配置备份为 `cutover/runtime.env` / `production-config.tar.gz`，本地 `.env.production` 已同步；新 MCP 注册为 `ack/mcp-client-production.json`。文件均私有且不入 Git。
- 七 Worker 并发各 1、Beat 单副本、20 GiB PVC Bound；开关仍为导入 true、清理 true、周期同步 false。实际九角色配置摘要与注册摘要一致、额度校验通过、Worker ping 7/7、启动日志无 ERROR/Traceback。
- 旧测试服保留数据库、Redis、release 和配置；九应用服务 disabled/inactive，旧 Nginx 返回 503 迁移提示页。源备份 timer 已恢复；它只备份旧源数据，不能当作生产持续备份。生产 RDS/Redis 的托管备份策略尚未从云控制台核验，本次一致性备份及生产配置恢复包已异机保存于当前工作站。
- DNS 管理属于阿里云 ns7/ns8.alidns.com；2026-10-10 集群/Google DNS 实测生产主机名 NXDOMAIN。已请核对 `tkada` CNAME 指向 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`，本机未持有该 DNS 账号权限；不要通过修改共享 CoreDNS、忽略证书校验或硬编码 ALB IP 冒充修复。

## 当前域名配置与恢复入口（同日后续切换）

- 当前网站：`https://tkada.shortclipflix.com`，真实 DNS / HTTPS / 原账号登录已通过。新域名仅向共享入口追加自身条目。旧 galaxyshortdrama 的 NXDOMAIN 记录为历史观测。
- 当前运行配置与 MCP 注册：私有 `ack/domain-shortclipflix/runtime.env`、`mcp-client.json`；本地 `.env.production` 已与线上核对同步。切换后的恢复包为同目录 `config-after.tar.gz`，校验和及回读证据为 `config-after-verified.json`。上述 `cutover/` 配置及 `ack/mcp-client-production.json` 是旧域名历史备份，不可直接作为当前配置重发。
- 本轮冻结备份与恢复证据位于同目录 `manifest.json` / `backup-verified.json`；未改变运行镜像和数据库 head。九角色 ready，七 Worker ping 通过，唯一 Beat 和 PVC 正常；临时工具 Pod/Secret 已按 UID 清理，旧测试服务继续停止。
- TikTok API 应用须登记 `https://tkada.shortclipflix.com/api/integrations/tiktok/callback`；MCP 新回调已由官方客户端注册并部署。用户手动重新授权及完整业务验收仍待完成。
- 详细证据及运维待验边界见[域名切换记录](../validation/2026-10-10-shortclipflix-domain.md)。

## 2026-10-10：迁往 test 与新加坡 ACR

用户已授权修改并迁移。目标 namespace 为 `test`，共享入口为 `test-https`，与原 `prod-https` 使用同一 ALB，DNS、域名、API/MCP 回调和数据连接不变。公网镜像地址为 `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<SHA>`；Pod 拉取地址为 `sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<SHA>`。用户提供的账号已登录成功；原始凭据仅保存在私有 `ack/acr-test-review/registry.json`。

备份及迁移证据使用私有 `ack/acr-test-migration/`。先发布脚本到公司仓库并构建新镜像，验证 VPC 拉取，再停写/排空并完成 PG/Redis/源码构建/配置/Beat 文件备份及恢复演练。生产 PG/Redis 原地复用，不执行数据覆盖恢复；原 namespace 九服务最终保留零副本。目标 Beat 使用新 20 GiB PVC 恢复原调度文件，目标配置/注册/开关必须与来源逐项一致。共享入口仅迁移本项目 host，保留其他所有 spec 条目。

当前运行 SHA 为 `673eecb9f928261541be57ecc93b3c7f4350682f`；ACR 镜像 index digest 为 `sha256:4923ba800e3c403d40cdbf8131fdadf164b783c25067c554e6dc32e9c439ec48`，AMD64 manifest 为 `sha256:9fa1b3d971991f44a4bf0afeef999c3d50186638dd0bf03002ef19b10637506a`。旧 `prod` 九 Deployment 为零副本，原 Beat PVC 和 Secret 保留作受控恢复依据，禁止直接启动。未来部署必须设置 `RETIRED_SOURCE_INVENTORY` 指向 `ack/acr-test-migration/retired-source.json`，逐项核对旧资源 UID/模板与停止状态；不能绕过查重。

最新恢复配置包为 `ack/acr-test-migration/config-after.tar.gz`，同目录保存冻结备份、独立源码及实际构建、Beat 文件、恢复验证、入口前后快照与部署验收。当前运行配置和注册文件与切换前逐项相同，本地 `.env.production` 匹配。旧 `domain-shortclipflix/` 为上轮配置证据，不能直接重跑其中旧 namespace 部署脚本。

## 2026-10-12：准备阶段读取并发与预算检查

- 候选配置：`worker-resource` 保持一个 Pod，Celery prefork 从 1 槽调整为 4 槽；请求仍受线上共享额度控制（应用/租户并发 4、单接口并发 2、每秒应用 10/接口 3），不放宽 TikTok 配额。
- 发布前基线：资源 Worker 约 355 MiB，原请求 2 GiB/上限 4 GiB；PG 连接约 69/200。新增三槽按默认连接池每槽最多 15 连接估算，上限增量 45；场景调用的短事务使用 NullPool。发布后复核 Pod RSS/重启、连接占用与错误日志；视频处理仍受素材准入约束。
- 每账户场景不再读取历史 Smart+ 广告组来判断预算资格，USD 金额/精度与预算竞价组合本地校验；保留旧预算证据模型供历史记录读取，不再由场景任务调度。
- VBO 最低 ROAS 资格只约束目标 ROAS 策略；最高价值不再被此资格阻断。有效场景仍按租户、BC、账户、Mini、冻结授权及版本隔离，沿用实际 86400 秒缓存；场景合同更新后首次会重新准备。
- 九角色、三个业务开关及应用额度不变。本次不迁移数据库；正式上线结果以对应验收记录为准。
