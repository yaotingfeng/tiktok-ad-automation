# TK-ADA 公司 K8s 生产部署

本目录沿用公司 `deploy-help` 的镜像、端口和命名约定；2026-10-10 根据用户提供的 `singaporeconfig`，生产目标已切换为阿里云新加坡 ACK，入口、存储类和镜像拉取凭据以本手册实际核验值为准。2026-10-09 用户明确指定公司集群为生产环境，并授权直接迁移新加坡测试服的密钥、队列和授权。新加坡作为迁移源；最终切换前先停止旧调度/消费并完成一致性备份。数据库类型继续使用 PostgreSQL。具体目标与迁移边界见 `docs/runbooks/production-junbo.md`。

## 已固定的部署约定

- 集群拉取镜像：`sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<完整 Git SHA>`。
- Gitea：`company` 远端 `https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git`；GitHub `origin` 保留。
- API、Dockerfile、Service 均为 80；外部通过共享 Ingress 的 HTTPS 443 访问。
- namespace 为 `test`，Ingress 为已有的 `test-https`（`alb`）；不创建第二个 Ingress。ACK 拉取 ACR VPC 镜像必须显式引用本项目 `tiktok-ad-automation-registry` Secret，禁止借用其他项目 Secret。
- `K8S_CONTEXT` 和域名必须显式传入；当前用户指定 `tkada.shortclipflix.com`，也支持公司分配的单层 `*.jetmobo.com` 域名；没有测试服务器 IP/sslip.io 回退值。
- API、7 个 Worker 和 Beat 使用同一镜像及配置。Worker 初始并发均为 1；Beat 单副本、`Recreate` 更新。
- API 与 Worker 均有就绪探针；所有角色有 CPU/内存 requests 与 limits。应用合计请求 4.25 核 CPU / 10.5 GiB 内存，上限合计 10.5 核 / 21 GiB；发布任务另请求 100m / 256 MiB。它们是首发预算，尚未经过生产压力测试。
- Beat PVC 使用 `alicloud-disk-topology-alltype`，申请 20 GiB；`WaitForFirstConsumer` 将云盘绑定到实际 Pod 的可用区。
- MCP Secret 使用单文件 `subPath` 挂载，兼容注册文件读取的 `O_NOFOLLOW`。配置摘要写入 Pod 注解，配置变化会重建所有角色；不依赖 Secret 热更新。

### 生产首发资源预算

2026-10-09 用户反馈 2 核 / 2 GiB 测试服不足；只读实测物理内存 1967 MiB、Swap 已用 2004/2047 MiB。此前 850m / 4.25 GiB 仅为启动验证预算，生产按下表提高申请量；不以低负载时的 RSS 或启动成功推断峰值容量。

| 角色 | 副本 | 每副本申请 CPU / 内存 | 每副本上限 CPU / 内存 |
| --- | --- | --- | --- |
| API | 1 | 0.5 核 / 1 GiB | 1 核 / 2 GiB |
| 素材 Worker | 1 | 1 核 / 2 GiB | 2 核 / 4 GiB |
| 报表 Worker | 1 | 1 核 / 2 GiB | 2 核 / 4 GiB |
| 搭建 Worker | 1 | 0.5 核 / 1 GiB | 1 核 / 2 GiB |
| 结果、控制、目录、广告管理 Worker | 各 1 | 0.25 核 / 1 GiB | 1 核 / 2 GiB |
| Beat | 1 | 0.25 核 / 512 MiB | 0.5 核 / 1 GiB |

总申请量为 4.25 核 / 10.5 GiB，上限合计 10.5 核 / 21 GiB，均不含外部 PostgreSQL、Redis、集群组件和发布瞬时资源。迁移 Job 另申请 0.1 核 / 256 MiB（上限 1 核 / 1 GiB）；Deployment 滚动更新还会创建临时副本。ACK 的 7 个节点是共享资源，应核对已有工作负载申请量和实际可调度容量；不能把集群总容量视作本项目独占额度。

七 Worker 仍各并发 1。上线先观察真实素材上传、报表同步及两者重叠时的峰值内存、CPU 限流、任务耗时、队列等待和数据库连接。内存持续超过 limit 的 80%、出现 OOM 或 CPU 限流伴随队列积压时，先定位对应角色再调整。完成这一负载验证前，不声明生产容量已验收；不以开启自动投放制造测试负载。

## 部署前要准备的具体资料

1. 获授权的 kubeconfig、明确的 context，以及 `test` 资源配额。优先使用项目受限凭据；凭据必须允许下述只读查重，权限不足时停止，不能跳过检查。
2. 生产域名 `tkada.shortclipflix.com` 的 CNAME 指向 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`。集群内真实 TLS 握手确认 ALB 已提供有效 `*.shortclipflix.com` 证书，无需新申请；2026-10-10 已按用户授权创建正式 Service 并追加新 host 的 TLS 声明与路由，控制器同步成功；九角色已部署；生产域名已确认解析到该 ALB，使用现有 shortclipflix 通配证书；切换验收见生产手册。脚本只检查 TLS hosts 声明，不读取共享证书 Secret；发布验收还需通过域名真实访问。
3. 仓库外的运行配置和真实 MCP 注册文件；注册文件必须允许目标回调域名。私有 R2 桶的 CORS 同步允许目标前端 origin，禁止改为公开桶。
4. 已确认从新加坡迁移密钥、队列和授权；最终切换时先停止旧调度、排空任务并按发布手册备份。不得让两套 Beat/Worker 同时消费同一批任务。
5. 三个部署开关的明确取值。`MATERIAL_INGEST_ENABLED` 控制新导入入口，`MATERIAL_CLEANUP_ENABLED` 控制自动清理，`ADS_SYNC_ENABLED` 控制新周期同步；关闭它们不等于已有队列任务不会执行。首次值按用户确认填写，后续发布保留已确认值。

`runtime-secret.example.env` 是字段模板，不是可直接部署的配置。使用 literal `KEY=value`，不要加 shell 引号或 `source` 到当前 shell；秘密文件权限设为 0600。目标域名必须同时用于 `FRONTEND_HOST`、MCP 回调和已启用的 TikTok API 回调。API 应用未配置时三个 API 注册字段全部留空，不能只填写一部分。

## 本地验证

环境检查只检查本机，不读取或修改集群：

```bash
docker --version
docker info
kubectl version --client
node scripts/test-deployment-assets.mjs
uv run --with kubernetes-validate python scripts/validate-k8s-manifests.py
```

ACK 服务端为 1.36.2，使用经官方 SHA-256 验证的 `.tools/bin/kubectl-1.36`；`.tools/ack-bin/kubectl` 链接到它。原 1.34 客户端仅留作旧环境工具，不用于 ACK 发布。本地 schema 检查验证 1.34 通用 API 合同，ACK 兼容性另由 1.36 真实服务端 dry-run 验证。

构建目标架构镜像并验证：

```bash
docker buildx build --platform linux/amd64 --load \
  -f backend/Dockerfile -t tiktok-ad-automation:deploy-check-amd64 .
uv run python scripts/check-local-deployment.py \
  --image tiktok-ad-automation:deploy-check-amd64
```

本地检查创建本轮专用的无外网 Docker 网络、临时依赖和合成配置，直接运行 K8s 清单里的九个应用命令及资源上限。检查实际配置解析、注册文件、额度、前端、登录、API 边界、七个 Worker 就绪、Beat 存活和 API 重启恢复。退出只清理本轮创建的容器、临时匿名卷及网络；不使用 `prune`，不读取现有私有运行配置，不调用真实 TikTok/R2。

本地结果不包含 DNS、TLS、集群网络、真实授权和真实业务验收。全仓历史回归待处理项仍见 `docs/validation/2026-10-09-regression-followups.md`。

## 提交、推送与正式镜像

相关验证通过后提交本轮文件；按项目授权规则推送公司 Git。发布要求工作区干净，不应为了通过检查覆盖或丢弃用户未提交的改动。

```bash
git status -sb
git rev-parse --show-toplevel
git push company main
docker login sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com
bash deploy/build-and-push.sh
```

脚本要求当前分支为 `main`，`RELEASE_SHA` 等于实际 HEAD，实时查询公司 `main` 必须与 HEAD 一致；随后才调用 Docker。镜像写入 `org.opencontainers.image.revision`。禁止换用其他项目/仓库或 `latest`。`DRY_RUN=1` 只检查本地参数并展示命令，不验证远端推送状态，不构成发布证据。

## 配置校验与只读预检

以下操作在运行配置及发布范围已确认后执行。文件路径和域名均替换为目标环境实际值：

```bash
export PATH="$PWD/.tools/ack-bin:$PATH"
export KUBECONFIG="$PWD/.runtime/company-production-20261010/ack/kubeconfig"
export K8S_CONTEXT='kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796'
export K8S_NAMESPACE=test
export INGRESS_HOST='tkada.shortclipflix.com'
export RELEASE_ID="$(git rev-parse HEAD)"
export APP_IMAGE="sg-acr-prod-registry-vpc.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:${RELEASE_ID}"
export RUNTIME_ENV_FILE='/private/path/tk-ada-production-runtime.env'
export RETIRED_SOURCE_INVENTORY="$PWD/.runtime/company-production-20261010/ack/acr-test-migration/retired-source.json"
export MCP_CLIENT_REGISTRATION_FILE='/private/path/mcp-client.json'

export LOCAL_IMAGE="sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:${RELEASE_ID}"
docker pull "$LOCAL_IMAGE"
DRY_RUN=1 bash deploy/k8s/apply.sh
PREFLIGHT_ONLY=1 bash deploy/k8s/apply.sh
```

- `DRY_RUN=1` 完全不联系集群，校验镜像/SHA、显式开关、目标域名、回调和外部配置文件。
- `PREFLIGHT_ONLY=1` 先核对本地镜像 revision，再用 `--network none` 的容器运行应用实际配置校验器；随后只读检查集群。任何一步失败均不会写集群。远程 Docker 引擎须能读取注册文件路径；本机 Colima 使用仓库外但位于已共享用户目录的私有路径，不用 `/var/folders`。
- 写入前全命名空间检查 Deployment/Service/Ingress/ConfigMap/PVC 名称及标签；同名或同标签跨 namespace、错误标签、外来镜像或域名冲突均阻断。
- Secret 只查询本项目 runtime、mcp-registration、registry 三个精确名称并核对标签，禁止查询其他项目 Secret。旧资源缺少归属标签时停止，不能自动认领；需要人工核对后按具体授权修复。
- 错误只输出字段名或动作，不回显 Secret。若实际配置校验失败，可在上述隔离容器内运行 `python scripts/check_deployment.py` 查看失败类别。

## 实际部署与入口

按 `docs/runbooks/deployment.md` 完成目标环境的发布前备份/排空和恢复验证；首次无历史状态的项目记录不适用项。上述条件齐全后：

```bash
bash deploy/k8s/apply.sh
# 仅首次尚未登记域名时执行；当前生产域名已登记，不重复追加。
bash deploy/k8s/patch-public-ingress.sh
```

部署脚本再次执行预检，之后更新带项目标签的两个 Secret、静态资源，运行本版本与配置摘要对应的 Job，最后发布九个应用 Deployment 并等待 rollout。Job 名称长度受限，完整 SHA/摘要保存在注解；已有同名 Job 只有归属、SHA、摘要和完成状态全部符合时才能复用。失败时停止，不自动删除旧 Job 或回滚持久状态。

Ingress 脚本固定追加 `test/test-https`，核对 Service 的标签、选择器和端口，读取完整共享规则，使用 resourceVersion 乐观锁，只追加一条规则，再完整回读旧规则与新规则。已有 host 会拒绝重复追加；正常升级保留已属于本项目的入口即可，不必重复执行追加脚本。所有 kubectl 命令固定使用显式 context 和 namespace。

## 集群验收与回退

```bash
kubectl --context "$K8S_CONTEXT" -n prod get deploy,svc,pods -l app=tiktok-ad-automation
kubectl --context "$K8S_CONTEXT" -n prod get job -l app=tiktok-ad-automation,role=migration
kubectl --context "$K8S_CONTEXT" -n prod logs deployment/tiktok-ad-automation-api --tail=100
curl --connect-to "$INGRESS_HOST:443:alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com:443" "https://$INGRESS_HOST/api/utils/health-check/"
uv run python scripts/check-bootstrap.py "https://$INGRESS_HOST"
```

HTTPS 验收不使用 `-k`，必须验证证书有效。继续检查七个 Worker、唯一 Beat、实际版本及配置一致性、R2 签名上传回读、API/MCP 注册/真实授权/账户发现。真实广告写入按具体授权另验。

保存镜像 digest、旧/新 SHA、配置摘要、资源版本、备份证据及验收结果。回退前先确认当前持久状态与旧应用兼容，并按原流程停止调度/排空；使用旧镜像和对应的已核实私有配置，不盲目执行 `rollout undo`（它不会恢复 Secret）。恢复相关内容按既有发布手册处理，本脚本不自动回退。

## 新加坡 ACR 与命名空间迁移

本机通过公网 `sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:<SHA>` 构建/推送/拉取；集群使用同仓库的 VPC 地址。部署脚本在本机校验公网镜像 revision 与真实运行配置，工作负载只接受 VPC 地址；发布后须核对 Pod imageID 与推送 digest。仓库密码只通过 stdin 或受控 dockerconfigjson 注入，不能出现在命令参数、文档或 Git。

`test` 是公司指定的 Kubernetes 命名空间，生产运行配置仍为 `.env.production`。迁移时先备份、停入口/Beat 并排空 Worker，再保留源资源为零副本；Beat 文件恢复到目标独立 PVC。只迁移本项目两个域名的共享入口条目，不改变其他站点，避免两个 Ingress 同时声明同一域名。

如保留旧 namespace 的停用资源，设置 `RETIRED_SOURCE_INVENTORY` 为私有归档 JSON（`namespace` 和原始 `items`，只含本项目 Deployment/Service/ConfigMap/PVC）。部署前逐项核对 UID、标签、原 Pod 模板及 Deployment 零副本，并检查源应用 Pod 已全部消失；未登记的同名资源仍阻断。该文件不是无条件跳过查重的开关；源服务被重新启动或替换会立即阻断部署。回退必须先停止目标，不能直接启动旧消费者。

当前 `test/test-https` 的 TK-ADA TLS 条目使用 ALB 按域名自动发现证书（不设置 `secretName`），可跨 namespace 复用证书管理服务中的证书。依据：[阿里云 ALB HTTPS 证书配置](https://www.alibabacloud.com/help/en/ack/ack-managed-and-ack-dedicated/user-guide/use-an-alb-ingress-to-configure-certificates-for-an-https-listener)。仍须验证控制器同步与真实域名 HTTPS，TLS hosts 声明本身不是证书可用证明。
