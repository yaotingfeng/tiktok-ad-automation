# TK-ADA 公司生产首发与迁移

## 已确认的目标与授权（2026-10-09）

用户已明确“公司集群就是生产环境，现在要部署到生产环境”，并要求“测试服的密钥、队列和授权要直接复制过去”。本次是新加坡测试实例向公司生产集群的迁移，覆盖此前“禁止复制测试密钥/授权”的默认规则；不再要求用户另找一套集群凭据。迁移不授权重复创建广告或绕过已有 UNKNOWN/冻结执行保护。

| 项目 | 已核实值 |
| --- | --- |
| 集群 | `cls-n950t106`，公司 TKE 弹性集群 |
| context | `cls-n950t106-100040386575-context-default` |
| 凭据来源 | 用户提供的 `deploy-help 2/vendor/kubeconfig`；禁止入 Git |
| namespace | `prod`，Active |
| 服务端版本 | `v1.34.1-tke.8` |
| 应用资源前缀 | `tiktok-ad-automation`；2026-10-09 查重无已存在同名项目资源 |
| 镜像仓库 | `junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:<完整 SHA>` |
| 仓库凭据 | 同工具包 `vendor/tcr-cred.txt`，禁止回显/提交 |
| 对外入口 | 现有 `prod/public` Ingress，IP `134.175.255.190`，HTTPS 443 |
| 证书 | 现有 Ingress `*.jetmobo.com` 通配证书 |
| 生产域名 | 拟用 `tiktok-ad-automation.jetmobo.com`，待用户/运维确认；当前 Ingress 无此 host |
| 应用端口 | API/容器/Service 均为 80 |
| 拓扑 | 1 API、7 个独立 Worker、1 Beat，使用同一版本及运行 Secret |
| 应用资源 | 合计申请 4.25 核 / 10.5 GiB，上限 10.5 核 / 21 GiB；不含 PostgreSQL、Redis 与发布瞬时资源 |
| 存储 | 已有 `cbs` StorageClass，集群没有默认 StorageClass；Beat PVC 显式申请 CBS 20 GiB |
| 配置落点 | `prod/tiktok-ad-automation-runtime` 与 `prod/tiktok-ad-automation-mcp-registration` Secret；未创建 |

集群是弹性虚拟节点，API 返回的巨大 allocatable 数值不代表已购买或可无限使用的物理容量。现有业务 Pod 已由平台自动注入 eklet toleration；不修改别人的资源。应用首发保守预算与扩容观测要求见 K8s 手册和通用发布手册。

## 迁移源核验

- 来源：新加坡 `137.220.150.31` 的现有测试实例，不将源服务器改称生产。
- 实际运行版本：`8b877f4`，9 个应用 systemd 服务均 active。
- 生效配置：`/etc/tt-ada-staging/app.env`；本地旧 `.env.staging` 已落后于实际服务，不作为权威来源。
- 已只读获取实际运行配置及 MCP 注册文件，存放在 Git 忽略的 `.runtime/company-production-20261009/source/`，目录 0700、文件 0600；不在文档记录密钥值。
- 生产配置草稿在同目录 `prepared/runtime.env.incomplete`，已保留来源密钥并转换为 Kubernetes env-file 格式；目标 PostgreSQL/Redis 留空，待落实后填写。源注册 JSON 原样保留，未伪造新域名注册。
- 2026-10-09 队列快照：七条业务队列长度均为 0，Redis unacked 为 0。这只是当时快照，最终切换前仍须停止调度并重新核对。
- 已生效开关为导入 `true`、清理 `true`、周期同步 `false`；迁移方案保留这些现有值，不用模板值静默覆盖。
- PostgreSQL 与 Redis 当前均监听 `127.0.0.1`；直接复制这些 URL 到 Pod 会连接 Pod 自身，不能作为生产目标连接信息。

## 当前缺项（不可用猜测值代替）

1. 集群可达的生产 PostgreSQL 连接配置。skill 固定的是 MySQL，不能用它连接现有 PostgreSQL 程序，也不在本轮更改数据库类型。
2. 公司 Redis 中明确分配给 TK-ADA 的逻辑库和恢复方式；不得向共享 DB 0 直接灌入来源 RDB，也不得覆盖其他项目键。先由运维落实隔离位置与导入方法。
3. 最终生产域名/DNS，以及现有 API/MCP 应用是否允许该域名回调。复制已有 token 与 client_id 不等于远端登记了新的 redirect URI，禁止只修改本地 JSON 冒充已注册。
4. 对来源授权记录和队列的一致性备份/目标恢复验证。目标地址未落实前保持测试服运行，不提前切停业务。

2026-10-09 查询拟用域名未返回 A 记录；13 个部署对象已通过真实集群 `apply --dry-run=server`，不会持久化对象，也不代表镜像拉取、CBS 实际绑定或应用已启动。日志位于私有目录 `server-dry-run.log`。

可交给运维的资料清单：

```text
项目：TK-ADA / tiktok-ad-automation
目标：公司 TKE cls-n950t106，namespace prod
DNS：tiktok-ad-automation.jetmobo.com，A 记录 -> 134.175.255.190
数据库：提供集群可达的 PostgreSQL 连接配置，保持现有 PostgreSQL 类型
Redis：公司 10.33.48.33:6379，分配本项目独占逻辑库并确认迁移导入方式
回调：在现有 TikTok API 应用及 MCP 客户端登记以下 HTTPS 回调：
https://tiktok-ad-automation.jetmobo.com/api/integrations/tiktok/callback
https://tiktok-ad-automation.jetmobo.com/api/integrations/tiktok/mcp/callback
状态：共享 Ingress 尚未追加规则；基础设施落实后执行迁移与上线验收
连接凭据通过本机私有配置文件提供，不放入代码或群消息。
```

## 执行顺序

1. 保留来源私密配置、注册材料和授权加密密钥；整理目标配置，只有网络地址、域名及挂载路径按目标替换。不要生成新的加密密钥导致历史授权不可读。
2. 完成项目相关验证，将已验证 `main` 推送公司 Gitea，构建带完整 SHA/revision 标签的 AMD64 镜像并推 TCR；保留本机用户未提交的 `AGENTS.md` 改动，使用干净发布副本。
3. 目标运行配置齐全后，离线容器检查真实配置，执行集群只读预检与服务端 dry-run，核对配额、调度和 CBS。
4. 切换窗口停止新入口写入、旧 Beat 和正常排空 Worker，暂停备份 timer 并等待已开始备份结束。备份 PostgreSQL、Redis、独立源码/构建产物、私有配置、注册材料、证书与服务定义；校验和及隔离恢复通过后才进行目标恢复。
5. 迁移原授权记录时同步保留密钥、连接代数、冻结请求、幂等记录和 UNKNOWN 状态；队列与持久业务事实必须来自同一停写窗口。不得用空库加队列、或单独复制 `.env` 代替授权数据迁移。
6. 目标先恢复事实和队列并完成验证，再运行审查后的版本迁移及九角色部署；确认旧消费者已经停止后才让生产消费。若发现仍有未确认的真实广告任务，先列明具体任务状态与原授权再处理，不盲目重发。
7. 追加本项目 Ingress 规则，验证证书、DNS、登录、租户、七 Worker、唯一 Beat、版本/配置一致，以及真实存储和 API/MCP 的读取能力。真实广告写入按具体业务授权验收。
8. 完成后恢复应恢复的备份 timer，记录新旧版本、镜像 digest、资源 UID、配置摘要、备份位置和恢复证据。旧消费者保持停止，避免双端消费；旧备份保留至回退窗口结束。

## 回退与当前状态

目标未具备恢复条件或检查失败时，保持来源实例原状。切换后的回退必须先停止生产消费者、核对新旧持久状态兼容性，再按一致性备份恢复；禁止仅 `rollout undo`、清空队列或覆盖共享 Redis 来恢复。

目前仅完成本地验证与生产集群只读核验、来源配置快照；尚未创建任何生产资源，尚未切停测试服。此次记录取代“公司集群是测试环境”的此前表述，但不把本地验证或只读检查记为生产上线成功。
