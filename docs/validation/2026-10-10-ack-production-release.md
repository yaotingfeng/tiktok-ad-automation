# 2026-10-10 ACK 生产迁移与发布

> 后续已按用户要求切换至 `https://tkada.shortclipflix.com`，真实 DNS/HTTPS/登录通过；见[域名切换记录](2026-10-10-shortclipflix-domain.md)。本文保留首次发布时旧域名的历史验收结果。

## 结果与待验边界

生产数据与九个应用服务已部署到阿里云新加坡 ACK / `prod`。指定真实 ALB、保持生产域名 Host/SNI 和正常证书校验的 HTTPS 验收通过：健康、登录页、静态资源、原管理员登录、两个租户、无权限边界、API/MCP 回调路由及 MCP 注册配置均正常。七个 Worker ping 全部响应，唯一 Beat 与 20 GiB PVC 正常。

**普通域名访问仍未通过**：集群默认 DNS 和 Google DNS 均对 `tkada.galaxyshortdrama.com` 返回 NXDOMAIN。权威域名服务为 `ns7.alidns.com` / `ns8.alidns.com`；已请用户/运维核对 `tkada` 的 CNAME 目标 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`。用户此前确认已配置公司解析，这里保留实际观测差异；指定 ALB 的测试不代表普通解析已正常。

用户明确由其手动测试重新授权。新的 MCP 客户端已生效，旧客户端下三份授权不能直接续期，须在生产重新授权；原授权事实与密钥保留。完整 OAuth 收码/换码、tools/list、候选 BC 和授权后账户发现未代用户执行。没有调用广告创建、启停或手动重放未知结果任务，也没有进行生产业务压力测试。

## 固定版本和配置

- 集群：`c19dbd82590a34b9f87cebb8c1a858796`，namespace `prod`。
- 镜像：`junbo-image.tencentcloudcr.com/prod/tiktok-ad-automation:783d3b836b61171153cf1863f4ecd7617bc3e7d6`。
- Digest：`sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`。后续仅文档提交不改变部署代码。
- 数据库：PostgreSQL `tkada`，Redis DB 15；平台原有 `cron` 两表保留，未修改其他数据库/逻辑库。
- 迁移：正式 Job 成功，head=`0019_scene_budget_page`。
- 开关：`MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true`、`ADS_SYNC_ENABLED=false`；与迁移源一致。
- 资源：九角色合计 requests 4.25 CPU / 10.5 GiB，limits 10.5 CPU / 21 GiB；七 Worker 并发各 1。Beat 单副本，PVC Bound 20 GiB。
- 运行 Secret、注册 Secret 与准备文件逐项一致；九角色配置/注册摘要一致，调用额度和租约校验均通过。本地 `.env.production` 已同步实际生产值，未入 Git。

## 停写、备份与恢复

用户明确授权继续完成生产部署并由其手动重新授权，沿用此前复制源密钥、队列与授权的授权范围。

1. 暂停源备份 timer，确认无正在执行的备份；停止 API 新请求和 Beat 投递，七 Worker 的 active/reserved/scheduled、七业务队列及 unacked 均为零，再正常停止全部 Worker。2026-10-10 06:14:06 UTC 九服务均 inactive，没有强杀任务或清空队列。
2. 同一停写窗口保存 PostgreSQL 自定义 dump、Redis RDB/逻辑快照、独立源码/实际前端构建和私有配置/证书/服务定义。清单含源版本 `8b877f4`、目标 SHA、head、大小与 SHA-256。归档排除可重建虚拟环境、node_modules、Git 与缓存。
3. 在无外网 Docker 内部网络恢复 PostgreSQL 18 / Redis 8；独立解压项目/配置并核对前端入口。136 张表行数与源库完全一致，4 份凭据可用原密钥解密，Redis RDB 完整并恢复 7 键；候选镜像迁移通过，迁移前后表行数及凭据检查一致。验证容器、匿名卷和网络已清理。
4. 恢复前重新确认目标 public 无业务表，Redis DB 15 仅有 TK-ADA 登记标记；保留目标原始快照。PostgreSQL 使用 `pg_restore --single-transaction --exit-on-error --no-owner --no-acl` 恢复。Redis 按逻辑类型和原绝对到期时间恢复，WATCH/MULTI 防止覆盖已存在的同名键；7 键逐项回读通过，含登记标记共 8 键。未使用 FLUSH 或覆盖共享 Redis 实例 RDB。
5. 生产恢复后再次比较全部 136 表行数和凭据解密结果，完全匹配；随后执行正式迁移 Job 与九角色发布。

私有证据根目录为 `.runtime/company-production-20261010/ack/cutover/`：`manifest.json`、`backup-verified.json`、`source-fingerprint.json`、`target-fingerprint.json`、`target-restored.json`、`deployment-result.json`。原始归档、凭据、日志、运行配置和证书全部保留在忽略目录，未进入 Git。

## 生产验收

| 检查 | 结果 |
| --- | --- |
| 九 Deployment rollout | 全部成功，初次稳定检查无重启 |
| 七 Worker ping | 7/7 pong |
| 配置与调用策略 | 九角色一致，额度与租约校验通过 |
| Beat | 单副本，20 GiB 云盘 Bound |
| HTTPS | 指定 ALB，生产域名 Host/SNI，正常 CA/主机名校验通过 |
| 登录页面和前端资源 | `/login` 使用浏览器 Accept，页面及引用资源均 200 |
| 管理员登录和租户 | 原账号登录成功，两个租户可读 |
| 访问边界 | 未登录用户接口 401，未授权租户接口 403，未知 API 404 |
| API / MCP 回调 | 无有效 state 时均返回 409 / invalid_oauth_state；属于边界测试 |
| MCP 注册 | 两租户配置均 READY；不等于完成实际 OAuth |
| R2 | 生产集群创建唯一微小探针对象，PUT/HEAD/GET/DELETE 全通过，删除后 HEAD 404 |
| 共享入口 | 变更前后 Ingress spec 完全一致，没有重复追加或更改其他域名 |
| 历史任务 | 36 份搭建提交状态分布一致，16 条 material result_unknown 保留；未手动重放 |
| 启动日志 | 九角色抽查最新 300 行，无 ERROR/CRITICAL/Traceback |

首次验证脚本未携带浏览器 `Accept: text/html`，`/login` 返回框架正常的 404；按仓库 bootstrap 检查约定补齐请求头后通过，未为该检查修改应用。Worker 初次检查早于 readiness，等待 rollout 成功后重新验证全部通过。

私有证据：`http-verification.json`、`services-verification.json`、`worker-ping.json`、`r2-production-probe.json`、`startup-log-summary.json`、`postlaunch-data-summary.json`、`production-config-verified.json`、`public-dns.json`。

## 源环境与运维边界

- 旧九应用服务已 inactive/disabled，不能直接重启，否则可能重复消费历史事实。原数据库、Redis、release 与配置保留；旧 Nginx 入口显示 503 迁移提示。改动前配置已归档，Nginx 校验后 reload。
- 源备份 timer 已恢复，并清除此前磁盘满导致的失败状态；它只备份旧源，不是生产备份。生产托管 RDS/Redis 自动备份策略尚未从云控制台核验；本次冻结数据和生产配置恢复包已异机保存于当前工作站。
- 本轮新增运行 Secret、MCP 注册、镜像拉取凭据和 K8s 清单已独立打包到私有 `production-config.tar.gz`，含恢复所需版本资料。原客户端文件保留。
- 临时迁移 Pod/Secret 按 UID 核对删除，源 `/run` 临时 RDB 与已校验本地备份一致后删除；不删除正式资源和恢复归档。
- 既有共享 R2 桶的 Public Development URL 原已开启，本轮未改变公开状态。生产 CORS 与对象探针通过不证明私有桶要求已满足；不能擅自关闭其他项目使用的入口。
- 当前容量只通过启动与基础验收，实际上传、报表、业务并发峰值需要用户测试后观察，不能以空闲 RSS 判断峰值容量足够。

后续正常发版仍需按生产手册重新备份和验证；当前已启动生产消费者，禁止使用旧切换脚本直接重跑恢复，也禁止只启动旧源实现回退。
