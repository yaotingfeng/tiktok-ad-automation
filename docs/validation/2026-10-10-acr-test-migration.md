# 2026-10-10 新加坡 ACR 与 test 命名空间迁移

按用户最新公司规范，TK-ADA 已从同一新加坡 ACK 的 `prod` 迁至 `test`。**入口仍为 https://tkada.shortclipflix.com**，真实 DNS、正常 TLS 校验、原管理员登录及两租户读取通过。`test` 是公司指定命名空间，仍按生产管理。

## 实际部署

| 项目 | 当前值 |
| --- | --- |
| 集群 | 阿里云新加坡 ACK `c19dbd82590a34b9f87cebb8c1a858796` |
| namespace / Ingress | `test` / `test-https` |
| 公网推送镜像 | `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:673eecb9f928261541be57ecc93b3c7f4350682f` |
| Pod 拉取镜像 | `sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:673eecb9f928261541be57ecc93b3c7f4350682f` |
| 镜像 index digest | `sha256:4923ba800e3c403d40cdbf8131fdadf164b783c25067c554e6dc32e9c439ec48` |
| AMD64 manifest | `sha256:9fa1b3d971991f44a4bf0afeef999c3d50186638dd0bf03002ef19b10637506a` |
| 数据 | 原 PostgreSQL `tkada` / Redis DB 15，未覆盖恢复生产数据 |
| 迁移 head | `0019_scene_budget_page`，正式迁移 Job 成功 |
| 拓扑 | API、七 Worker、唯一 Beat；九角色就绪，初次验收重启 0 |
| 资源 | requests 合计 4.25 CPU / 10.5 GiB；limits 10.5 CPU / 21 GiB；Worker 各并发 1 |
| Beat | 新 20 GiB PVC Bound，冻结源调度文件恢复并逐文件 SHA-256 核对 |
| 开关 | 导入 true、清理 true、周期同步 false，与迁移前一致 |

发布脚本提交 `673eecb` 已先推送公司 main；使用独立干净发布副本构建，保留用户本地未提交的 AGENTS.md。本次后端/前端源码相对原运行版本 783d3b8 没有业务变化。

构建脚本只推送 ACR 公网地址；部署只接受 ACR VPC 地址并在本机验证公网同 SHA 镜像。本地构建完成后 BuildKit 上传会话发生连接错误；中断仅属于本项目该镜像的卡住进程，改用 `docker push` 推送同一已构建镜像成功。公网仓库权限核验具有本项目 pull/push，`test` 探针通过 VPC 拉取并核对实际 imageID。原业务入口直到新镜像及网络检查完成后才冻结。

## 备份与切换

1. 保存原 namespace 本项目 Deployment/Service/ConfigMap/PVC/Job/Pod、三个项目 Secret、运行 env、MCP 注册及两个共享 Ingress 快照。私有材料只保存在忽略目录。
2. 停止旧 API 与 Beat，七 Worker active/reserved/scheduled、七队列、unacked 全部为零，再停止 Worker。2026-10-10 07:55:09 UTC 冻结完成；没有清空队列或重放 UNKNOWN。
3. 同一窗口备份 PG public、Redis DB 15、旧运行版本独立 Git 归档、实际运行镜像 `/app`（含前端构建）、私有配置及 Beat 文件。数据库 dump 在远端落盘后复制，源端与本地 SHA-256 一致。
4. 隔离 PostgreSQL 18 / Redis 8 恢复通过：136 业务表行数一致，4 份凭据可解密；Redis 本轮冻结的 7 个存活键恢复成功。候选镜像迁移前后指纹相同。Redis 键数属于本轮快照，不以此前 8 键快照覆盖当前状态。验证容器、网络和临时卷已清理。
5. 新 Beat PVC 恢复 `celerybeat-schedule` 并逐字节摘要核对；启动 Beat 前删除目标恢复 Pod、释放 RWO 挂载。旧 PVC 保留。
6. 通过 resourceVersion 和具体原条目双重校验，仅将 TK-ADA 的 shortclipflix 与历史 galaxyshortdrama 两个 host 的规则和 TLS 条目从 `prod-https` 移至 `test-https`。两端 spec 与预期完全一致，其他站点未改变；ALB 两个入口均 SuccessfullyReconciled。
7. 新 TLS 条目通过域名自动发现已有平台证书，不读取或复制共享证书私钥。依据：[阿里云 ALB 证书自动发现与跨 namespace 支持](https://www.alibabacloud.com/help/en/ack/ack-managed-and-ack-dedicated/user-guide/use-an-alb-ingress-to-configure-certificates-for-an-https-listener)。真实域名 CA/主机名检查通过。
8. 目标正式迁移 Job 成功后启动九角色；原 `prod` 九 Deployment 保持零副本。旧测试服务器九服务仍停用，不新增第二套消费者。

源备份工具首次在 Beat 节点因 CPU requests 不足被拒绝，业务服务未受影响；仅调低临时工具 requests 后成功。该现象属于节点已申请额度，不代表本项目业务负载容量通过验证。

## 验收

- 本地：4 项构建 + 24 项部署行为测试、Ruff、Python 编译、Shell 语法及差异检查通过。新增错误 namespace、公网镜像误用于集群、退休源 UID/模板漂移、旧副本/Pod 仍活动等阻断测试。
- 新九 Deployment ready；七 Worker ping 7/7、单 Beat、新 PVC Bound；九角色配置/注册摘要一致，调用额度与租约检查通过。
- 目标运行 Secret 和 MCP 注册与源逐项相同，本地 `.env.production` 与线上匹配；账号、数据加密密钥、数据库/Redis、回调及功能开关未变化。
- 使用真实 DNS 的 HTTPS 健康页、浏览器登录页及引用静态资源均成功；原账号登录及两个租户读取通过；401/403/404 与无效 OAuth state 边界正常。
- MCP 两租户配置 READY；这不替代 OAuth 收码/换码、tools/list 和授权后的 BC/账户发现验收。
- 九角色最新 200 行启动日志未发现 ERROR/CRITICAL/Traceback。
- 迁移临时 Pod/Secret 按 UID 清理。旧正式部署、Secret、Beat PVC 和所有恢复归档保留。

## 恢复入口与未验事项

私有根目录 `.runtime/company-production-20261010/ack/acr-test-migration/`：`manifest.json`、`backup-verified.json`、`beat-restored.json`、`target-image-probe.json`、`deployment-result.json`、`http-verification.json`、`services-verification.json`、`worker-ping.json`、`final-status.json`、`cleanup-verified.json`。切换后配置独立归档 `config-after.tar.gz` 已逐成员回读核对，摘要见 `config-after-verified.json`。

后续部署必须传入私有 `retired-source.json`：只允许保留 UID/标签/原 Pod 模板匹配、零副本且无运行应用 Pod 的已归档旧资源；其他跨 namespace 冲突照常阻断。该检查防止新旧同时消费。回退先停目标写入/消费者，核实新增事实、schema 与调度状态，再制定一致恢复；禁止直接启动 prod 旧服务。

用户手动 API/MCP 授权和真实业务/峰值容量测试尚未验收，TikTok 应用新域名 API 回调登记仍待用户确认。本轮未改变域名，不需要因 namespace 改名再次注册回调。托管 PG/Redis 自动备份策略仍未从云控制台核验；共享 R2 桶原有公开状态限制继续保留。本次发布验证不代表上述事项已完成。
