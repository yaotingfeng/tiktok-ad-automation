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

明确 TK-ADA 的 PostgreSQL 数据库名、Redis 专用逻辑库编号与迁入位置；配置新域名 CNAME、TLS host 与入口 rule，登记 TikTok API/MCP 回调并核对 R2 CORS。完成一致性备份及恢复验证后再停旧消费者并迁入生产。当前认证检查使用管理库，不代表业务数据恢复和完整上线验收完成。
