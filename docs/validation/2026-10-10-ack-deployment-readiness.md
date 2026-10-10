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

后续已落实 PostgreSQL `tkada`；Redis DB 15 经撤回后，用户明确确认使用，已重新登记并验证，见末节。用户随后确认运维已完成公司侧域名解析；还需验证实际访问、配置 TLS host 与入口 rule，登记 TikTok API/MCP 回调并核对 R2 CORS。完成一致性备份及恢复验证后再停旧消费者并迁入生产；认证和空间创建不代表业务数据恢复或完整上线验收完成。

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
