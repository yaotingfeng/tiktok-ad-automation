# Jenkins 与一键发布

目标固定为新加坡 ACK `c19dbd82590a34b9f87cebb8c1a858796` 的 `test` 命名空间，生产入口 `https://tkada.shortclipflix.com`。这套脚本用于已建立的九服务部署；首次部署继续按 `deploy/k8s/README.md`。不支持自动扩缩容拓扑或更换配置：当前九角色各一副本，普通发版直接读取线上 Secret，保留三个功能开关。

## 已实现与接通条件

仓库提供发布、备份、恢复指令和 Jenkinsfile。**尚未连接公司 Jenkins 或备份桶，不能据此认定自动发布、定时备份已经运行。** 首次需要公司提供 Jenkins Job/Agent、Gitea webhook 接入及专用私有备份存储；现有本地管理员 kubeconfig 不自动上传 Jenkins。

- Gitea Multibranch Pipeline 指定根目录 `Jenkinsfile`；main 测试通过后自动构建/上传/发布，无人工审批按钮，其他分支只测试。使用 Gitea 插件配置 push webhook 或公司批准的 SCM 定期扫描；单有 Jenkinsfile 不会建立 webhook。
- 备份 Job 固定可信 main 的 `Jenkinsfile.backup`，不接 push webhook。`BACKUP_CRON` 默认空；按双方确定的停服窗口填写，例如 Jenkins 时区为 Asia/Shanghai 时 `H 3 * * *`。这是停写一致性备份，会短暂停止网站与任务；云 PostgreSQL PITR/Redis 托管备份应由运维另外配置，不能用此 Job 代替在线灾备。
- main 的完整测试未通过时停止发布，不跳过既有失败。现有 Gitea Actions 可并行做测试，不含发布阶段，无需为了接入 Jenkins 删除。
- 配置 Jenkins 构建失败通知和备份最大允许时间告警。仓库未指定公司通知渠道，不擅自向任何人发送消息。

## Agent 和凭据

Agent 标签默认 `tkada-release`，要求独占或只用于可信项目；具备 Docker/buildx（本地 daemon）、Python 3.14、uv、Bun 1.4.2、Node、ffmpeg/ffprobe、PostgreSQL 18 客户端、kubectl 1.36、age 和 AWS CLI。Playwright 的系统依赖由运维预装。设置 `PG_BIN_DIR` 为 PG18 客户端目录。Docker CLI 插件须安装在系统目录，不能只装在被临时 DOCKER_CONFIG 替换的用户目录。

Agent 要能访问公司 Gitea、公网 ACR、ACK API、HTTPS 入口、私有桶及基础镜像源；PG/Redis 实际备份通过集群内工具 Pod 访问 VPC。Docker 需要本地可读 env 文件；不得将生产私有状态放 Jenkins workspace 或共享节点的公开目录。禁用 shallow clone，保留旧运行 SHA 对应源码用于独立归档。

Jenkins 插件：Pipeline、Declarative Pipeline、Git、Credentials Binding、Gitea Branch Source（或公司现有触发方式）。Git 工具名示例 `Default`，按公司实际名称调整。分支 Job 不授予可滥用的生产凭据；由管理员设置可信 Jenkinsfile/受保护 main 和凭据作用域，防止非可信分支修改流水线获取凭据。

| 凭据 ID | 类型 / 用途 |
| --- | --- |
| `tkada-gitea-read` | Username/password，用于 fetch 与实时核对 main |
| `tkada-acr-push` | Username/password，仅本项目仓库 push/pull |
| `tkada-ack-kubeconfig` | Secret file，ACK 本项目权限 |
| `tkada-retired-source` | Secret file，旧 prod 停用资源清单；已有私有迁移记录中的 retired-source.json |
| `tkada-backup-aws` | Secret file，AWS CLI INI 凭据；只读写专用备份前缀，不具备删除权限 |

Agent/Job 非秘密环境参数：

```text
TKADA_STATE_ROOT=/var/lib/tkada-release
BACKUP_BUCKET=<专用私有桶>
BACKUP_PREFIX=tkada/production
BACKUP_AGE_RECIPIENT=age1<备份恢复公钥>
BACKUP_ENDPOINT_URL=<S3兼容服务的HTTPS地址；AWS S3可省略>
AWS_DEFAULT_REGION=<桶所在区域>
PG_BIN_DIR=/usr/lib/postgresql/18/bin
```

状态根目录预先创建，归 Agent 用户，权限 0700。age 私钥由负责人离线保存并做解密演练，**不放到日常发布 Agent**。备份为客户端加密的 `.tar.age`，不复用公开素材桶。私有桶需禁止公共读取、启用适用的版本保护及保留策略；建议日备份至少 14 天、周备份 8 周，具体保留时长由公司确认。脚本不删除备份；桶 lifecycle/周归档由运维配置，未配置时不要声称已生效。

ACK 权限需满足既有 apply 的全 namespace Deployment/Service/Ingress/ConfigMap/PVC 查重（不列举其他项目 Secret）、旧源只读核对，以及 test 中本项目资源读写：九 Deployment/scale、Service、ConfigMap（包括独占锁）、PVC、精确三个 Secret、migration Job、工具 Pod/exec/log。共享 Ingress 只读。不能把 test 全 namespace 的 Secret 管理权限泛授给其他 Job；按公司 RBAC 策略落实，禁止自动借用管理员凭据。

## 执行方式

已推送公司 main 的干净 checkout 中运行；本地用户改动保留，使用干净副本。先登录公网 ACR，导出上述桶参数与 AWS 凭据文件。以下公共环境值可直接使用，私有路径替换为实际值：

```bash
export KUBECONFIG=/private/tkada/kubeconfig
export K8S_CONTEXT=kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796
export K8S_NAMESPACE=test
export RETIRED_SOURCE_INVENTORY=/private/tkada/retired-source.json
export AWS_SHARED_CREDENTIALS_FILE=/private/tkada/backup-aws.ini
bash deploy/build-and-push.sh
python3 deploy/release/release.py deploy \
  --sha "$(git rev-parse HEAD)" \
  --state-dir "/private/tkada/releases/$(date -u +%Y%m%dT%H%M%SZ)"
```

只有构建不发布时停在 build-and-push；只备份使用 `release.py backup --state-dir <新私有目录>`。发布/备份共用集群 ConfigMap 原子创建锁，冲突即停止，不自动抢占过期锁。Jenkins 的 disableConcurrentBuilds 不中断旧发布，脚本在停服前再次检查远端 main，过期 SHA 拒绝切换。

## 发布步骤和故障边界

1. 保存九个 Deployment/UID、实际配置和原版本；准备镜像、age、桶及只读预检。旧生产配置来自当前 Secret，不从本地模板覆盖。
2. API/Beat 缩到零，取消全部七 Worker 的消费。等待 active/reserved/scheduled 和订阅全部清空，**不要求 Redis 业务队列长度为零**。在途步骤自行收尾；超时或 Worker 失联就停止，不强杀进入备份。
3. Worker 归零，生成同一停写窗口的备份：public 业务 schema 的 PG18 dump、Redis DB15 DUMP/绝对 TTL、Beat 文件、旧 SHA 独立源码归档、运行 `/app`（含前端构建，依赖由旧 SHA 镜像重建）、三个项目 Secret、ConfigMap/服务/PVC/Deployment 与当前 Ingress 的 ALB 证书引用。云库托管 cron schema/账号、共享 Redis 其他 DB、共享证书私钥及 R2 素材正文不在此项目备份范围；对应平台须单独保留，不能称整个云账户可恢复。
4. 远端文件先生成再 `kubectl cp`，逐文件核对 SHA256。真实隔离 Docker internal 网络中恢复 PostgreSQL/Redis，比对表行数、迁移 head、凭据可解密性以及 Redis 键内容/TTL。候选迁移在隔离库先执行；不启动任何广告消费者或访问外部平台。
5. 客户端 age 加密，上传后完整下载核对密文 SHA256，成功才对正式库迁移。apply 将九个 Deployment 保持零副本；失败保留现场，不自动恢复旧数据库或版本。
6. 卸载备份工具 Pod/Beat 磁盘，依次恢复 API、七 Worker、唯一 Beat，检查九 Pod Ready、镜像、运行开关和真实 HTTPS。OAuth、素材/广告真实写入不在自动验收内。

私有状态目录有机器可读 phase、备份 manifest、恢复验证与远端地址。Jenkins 不把这些作为公开 artifacts。失败时先查看：

```bash
python3 deploy/release/release.py status --state-dir /private/tkada/releases/<批次>
```

- **迁移前失败**：解决原因后，`recover --state-dir <原批次>` 验证锁、UID、原模板/配置，取消剩余消费并正常收尾，恢复旧版本服务；不恢复数据库、不清队列。仍有无法确认的在途任务时继续阻断，需检查 Worker 状态。
- **candidate-held / resuming**：候选迁移已成功或恢复服务中断。排查后使用 `finish --state-dir <原批次>` 完成候选版本恢复及验收。
- **migration-started**：迁移可能部分执行，不允许 recover；检查 migration Job/Alembic head、数据库兼容性和已应用 Deployment。由管理员制定继续迁移方案，不能盲目重跑、修改 phase 或 rollout undo。
- 进程断在锁创建/状态写入之间，或状态目录丢失，需要人工检查锁 token、九服务和迁移 Job 后处理；不会按时间自动删除锁。`complete` 后删除锁失败时，核验 acceptance 后由管理员仅删除该锁。

`DRAIN_TIMEOUT_SECONDS` 默认 1200；增加等待时长不改变“不能强杀在途任务”的规则。失联 Worker 的 UNKNOWN 写入必须按业务恢复机制核对，不通过发布脚本重放。

## 灾难恢复演练

在独立机器/隔离环境下载指定 `.tar.age`，用离线私钥执行 `age -d -i <identity> -o backup.tar backup.tar.age`，解包后验证 manifest 中每个文件 SHA256。从 `config.tar.gz` 恢复原加密密钥及连接配置到私有目录，将 **DATABASE_URL 和 REDIS_URL 改为全新隔离实例**；PG 按 pg_restore 恢复 public schema，Redis 每项使用 DUMP/RESTORE 与绝对 expires_at_ms（已过期键跳过）。必须先核实表/凭据及队列状态再讨论真实业务恢复，禁止直接覆盖仍带旧 AOF 的共享 Redis。完整隔离恢复算法在 `backup.py:restore_verify`，不提供误触生产库的自动恢复命令。

每月独立下载/解密演练，并记录恢复耗时与可接受数据损失；脚本每次发布时的恢复验证只覆盖本次归档，不替代离线私钥和远端长期可读性演练。

## 验证

```bash
node scripts/test-deployment-assets.mjs
python3 scripts/test-release-integration.py
uv run ruff check deploy/release scripts/test-release-tools.py scripts/test-release-integration.py
bash -n deploy/ci/check.sh deploy/build-and-push.sh
```

integration 默认使用本地 `tiktok-ad-automation:deploy-check-amd64`，或设置 `RELEASE_TEST_IMAGE`。创建独立 PG18/Redis8，使用纯合成数据，测试恢复、损坏归档拒绝，以及真实 Celery 取消消费/排空/恢复时队列不丢失。退出只删除本次记录的容器/网络/临时镜像。完整 Jenkins 检查由 `deploy/ci/check.sh` 运行现有后端、前端、浏览器和构建测试；没有 Jenkins 服务时无法验证公司插件、webhook、Agent 权限与完整线上流水线。
