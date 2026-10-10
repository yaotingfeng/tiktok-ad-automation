# TK-ADA 公司生产首发与迁移

## 已确认的目标与授权（2026-10-10 更新）

用户已明确“公司集群就是生产环境，现在要部署到生产环境”，并要求“测试服的密钥、队列和授权要直接复制过去”。本次是新加坡测试实例向公司生产集群的迁移，覆盖此前“禁止复制测试密钥/授权”的默认规则；不再要求用户另找一套集群凭据。迁移不授权重复创建广告或绕过已有 UNKNOWN/冻结执行保护。2026-10-10 用户提供 `/Users/yaotingfeng/Downloads/singaporeconfig`，明确改用阿里云新加坡 ACK；原 skill 中的腾讯云 TKE 仅为此前误用的准备目标，后续不再向其部署。

| 项目 | 已核实值 |
| --- | --- |
| 集群 | 阿里云 ACK，新加坡 `ap-southeast-1`；集群 ID `c19dbd82590a34b9f87cebb8c1a858796` |
| context | `kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796` |
| 凭据来源 | 用户提供的 `/Users/yaotingfeng/Downloads/singaporeconfig`；私有副本 `.runtime/company-production-20261010/ack/kubeconfig`，0600，禁止入 Git |
| namespace | `prod`，Active |
| 服务端版本 | `v1.36.2-aliyun.1`；本地使用校验过官方 SHA256 的 `.tools/bin/kubectl-1.36` |
| 应用资源前缀 | `tiktok-ad-automation`；2026-10-10 ACK 查重无同名业务 Deployment/Service |
| 镜像仓库 | `junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:<完整 SHA>` |
| 仓库凭据 | 同工具包 `vendor/tcr-cred.txt`，禁止回显/提交 |
| 对外入口 | 现有 `prod/prod-https`，IngressClass `alb`；ALB 域名 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com` |
| 证书 | 集群内以 `tkada.galaxyshortdrama.com` 为 SNI 的真实 TLS 校验通过，现有证书含 `*.galaxyshortdrama.com`；新 host 的 Ingress TLS 声明待登记 |
| 生产域名 | 用户于 2026-10-10 指定 `tkada.galaxyshortdrama.com`；尚无 DNS 解析及 Ingress rule |
| PostgreSQL 业务库 | `tkada`，2026-10-10 已用所提供账号创建，所有者 `gzjunbo`；尚未迁入业务数据 |
| Redis 逻辑库 | 待公司运维正式分配；此前 DB 15 空库快照不能证明无人预留，已撤回自行占用配置 |
| 应用端口 | API/容器/Service 均为 80 |
| 拓扑 | 1 API、7 个独立 Worker、1 Beat，使用同一版本及运行 Secret |
| 应用资源 | 合计申请 4.25 核 / 10.5 GiB，上限 10.5 核 / 21 GiB；不含 PostgreSQL、Redis 与发布瞬时资源 |
| 存储 | Beat 申请 20 GiB，使用已存在的 `alicloud-disk-topology-alltype` / `WaitForFirstConsumer`，随首个 Pod 可用区绑定 |
| 镜像拉取 | 已创建本项目 `tiktok-ad-automation-registry` Secret；ACK 默认 ACR 凭据不能拉取 TCR，工作负载显式引用本项目凭据 |
| 配置落点 | `prod/tiktok-ad-automation-runtime` 与 `prod/tiktok-ad-automation-mcp-registration` Secret；未创建 |

ACK 已核实 7 个 Ready 节点，分布在新加坡四个可用区；`prod` 未配置 ResourceQuota/LimitRange。节点容量不是可直接独占的剩余资源，部署仍须核对其他工作负载已申请量及实际调度结果。

## 迁移源核验

- 来源：新加坡 `137.220.150.31` 的现有测试实例，不将源服务器改称生产。
- 实际运行版本：`8b877f4`，9 个应用 systemd 服务均 active。
- 生效配置：`/etc/tt-ada-staging/app.env`；本地旧 `.env.staging` 已落后于实际服务，不作为权威来源。
- 已只读获取实际运行配置及 MCP 注册文件，存放在 Git 忽略的 `.runtime/company-production-20261009/source/`，目录 0700、文件 0600；不在文档记录密钥值。
- 生产配置草稿在同目录 `prepared/runtime.env.incomplete`，已保留来源密钥并转换为 Kubernetes env-file 格式；这是此前目标未定时的历史草稿；当前草稿位置见下文 2026-10-10 记录。源注册 JSON 原样保留，未伪造新域名注册。
- 2026-10-09 队列快照：七条业务队列长度均为 0，Redis unacked 为 0。这只是当时快照，最终切换前仍须停止调度并重新核对。
- 已生效开关为导入 `true`、清理 `true`、周期同步 `false`；迁移方案保留这些现有值，不用模板值静默覆盖。
- PostgreSQL 与 Redis 当前均监听 `127.0.0.1`；直接复制这些 URL 到 Pod 会连接 Pod 自身，不能作为生产目标连接信息。

## 2026-10-10 新目标配置与剩余条件

用户已提供阿里云新加坡 PostgreSQL / Redis 地址及凭据，覆盖 skill 中公司固定 Redis 的默认值。凭据仅保存于 `.runtime/company-production-20261010/infrastructure.json`（0600），不写入文档、命令行参数或 Git。

| 服务 | 内网主机 | 外网主机 | 端口 |
| --- | --- | --- | --- |
| PostgreSQL | `pc-gs5ka8g997w7sv5ci.rwlb.singapore.rds.aliyuncs.com` | `singapore-pg-test.rwlb.singapore.rds.aliyuncs.com` | 5432 |
| Redis | `r-gs5lmf0pt8tic0r0t7.redis.singapore.rds.aliyuncs.com` | `r-gs5lmf0pt8tic0r0t7pd-test.redis.singapore.rds.aliyuncs.com` | 6379 |

域名已确定为 `tkada.galaxyshortdrama.com`。同日私有运行草稿 `runtime.env.incomplete` 保留原授权加密密钥、R2/API 密钥及三个开关；同步新 origin 与回调，但未伪造远端注册文件。用户随后要求自行创建项目空间：已创建 PostgreSQL `tkada`（所有者 `gzjunbo`），此前自行登记 Redis DB 15 的做法已撤回：用户明确这是公司共享实例，空库不等于未分配，必须先查公司的分配登记。私有草稿保留 PostgreSQL URL，REDIS_URL 已清空待分配；尚未迁入数据。

待落实项：

1. **PostgreSQL 已落实，Redis 编号待分配**：保留 `tkada` 项目库。Redis 的 0–255 编号为实例预设，键数为 0 或写入自定义标记都不能证明归属、排除其他项目预留。由公司运维确认 TK-ADA 使用编号并登记后再填 REDIS_URL；不得自行把空库视为专用库，不得修改实例库数或清空共享实例。
2. ACK 已用所提供凭据完成内网 PostgreSQL 只读认证（`postgres` 管理库，服务端 18.3）和 Redis AUTH/PING。外网地址连接超时，部署选用内网；已随后建好 PostgreSQL 项目库；Redis 正式分配与目标恢复仍待执行，没有迁入业务数据。
3. DNS CNAME 指向上表 ACK ALB 域名，登记新 host 的 TLS 声明与路由。现有通配证书已通过真实验证，无需新申请。不要再使用旧腾讯云入口 `134.175.255.190`。本项目只追加自身路由，不读取其他项目的证书 Secret。
4. 在现有 TikTok API 应用及 MCP 客户端中登记新回调，核对 R2 CORS 允许新 origin。复制 client_id/token 不等于已登记新的 redirect URI。
5. 最终停写窗口中的一致性备份、目标恢复验证和单侧消费者切换；条件未齐前继续保持测试服运行。

可交给运维的资料清单：

```text
项目：TK-ADA / tiktok-ad-automation
目标：阿里云新加坡 ACK c19dbd82590a34b9f87cebb8c1a858796，namespace prod
DNS：tkada.galaxyshortdrama.com，CNAME -> alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com
HTTPS：为共享入口 prod/prod-https 确认匹配证书及 TLS host，保留其他站点
PostgreSQL：tkada（已创建，所有者 gzjunbo）；使用已验证内网主机
Redis：请按公司分配登记给 TK-ADA 一个未分配/未预留的逻辑库编号；不能只凭空库判断
网络：ACK 内网 PostgreSQL、Redis 已认证成功；不再按腾讯云跨云超时处理
TikTok API 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/callback
TikTok MCP 回调：https://tkada.galaxyshortdrama.com/api/integrations/tiktok/mcp/callback
R2 CORS：允许 https://tkada.galaxyshortdrama.com
状态：生产业务服务未启动，Ingress rule 尚未追加
```

2026-10-09 的 13 个部署对象已通过服务端 dry-run，不代表应用、磁盘绑定或网络验收。2026-10-10 此前腾讯云仅创建本项目前缀的临时网络探针，无密钥、无队列消费者，用于验证真实镜像拉取、调度和网络；探针结束后清理。旧目标的结果见当日腾讯云记录；新目标以 [ACK 验证记录](../validation/2026-10-10-ack-deployment-readiness.md) 为准。

## 执行顺序

1. 保留来源私密配置、注册材料和授权加密密钥；整理目标配置，只有网络地址、域名及挂载路径按目标替换。不要生成新的加密密钥导致历史授权不可读。
2. 完成项目相关验证，将已验证 `main` 推送公司 Gitea，构建带完整 SHA/revision 标签的 AMD64 镜像并推 TCR；保留本机用户未提交的 `AGENTS.md` 改动，使用干净发布副本。
3. 目标运行配置齐全后，离线容器检查真实配置，执行集群只读预检与服务端 dry-run，核对配额、调度和阿里云云盘。
4. 切换窗口停止新入口写入、旧 Beat 和正常排空 Worker，暂停备份 timer 并等待已开始备份结束。备份 PostgreSQL、Redis、独立源码/构建产物、私有配置、注册材料、证书与服务定义；校验和及隔离恢复通过后才进行目标恢复。
5. 迁移原授权记录时同步保留密钥、连接代数、冻结请求、幂等记录和 UNKNOWN 状态；队列与持久业务事实必须来自同一停写窗口。不得用空库加队列、或单独复制 `.env` 代替授权数据迁移。
6. 目标先恢复事实和队列并完成验证，再运行审查后的版本迁移及九角色部署；确认旧消费者已经停止后才让生产消费。若发现仍有未确认的真实广告任务，先列明具体任务状态与原授权再处理，不盲目重发。
7. 追加本项目 Ingress 规则，验证证书、DNS、登录、租户、七 Worker、唯一 Beat、版本/配置一致，以及真实存储和 API/MCP 的读取能力。真实广告写入按具体业务授权验收。
8. 完成后恢复应恢复的备份 timer，记录新旧版本、镜像 digest、资源 UID、配置摘要、备份位置和恢复证据。旧消费者保持停止，避免双端消费；旧备份保留至回退窗口结束。

## 回退与当前状态

目标未具备恢复条件或检查失败时，保持来源实例原状。切换后的回退必须先停止生产消费者、核对新旧持久状态兼容性，再按一致性备份恢复；禁止仅 `rollout undo`、清空队列或覆盖共享 Redis 来恢复。

目前已完成本地验证、生产集群预检、临时网络探针及来源配置快照；尚未创建生产业务服务或切停测试服。此次记录取代“公司集群是测试环境”的此前表述，但不把本地验证或只读检查记为生产上线成功。
