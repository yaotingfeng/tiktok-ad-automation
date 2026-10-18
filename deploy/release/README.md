# 本地一键发布

目标固定为新加坡 ACK 的 `test` 命名空间，生产入口 `https://tkada.shortclipflix.com`。日常由公司 Jenkins 在推送后自动执行（见下文），也可在本机完成检查、推送公司 main、构建上传镜像、发布前备份、迁移、部署和验收。无需备份云盘或 OSS。日常数据库备份继续使用已有 PolarDB/Tair 托管策略。

## 日常操作

在项目根目录执行：

```bash
python3 deploy/local.py deploy
```

使用**已提交的本地 main**。脚本创建独立干净副本，完整检查通过才向公司 main 快进推送，然后构建 AMD64 镜像到公司 ACR。未提交的文件留在原工作区，不会自动提交或发布；让助手完成改动和验证提交后，再运行此命令。不会推送 GitHub origin。检查失败、远端 main 已变化或备份验证失败都会阻止切换。

### 检查范围（默认按改动，按需全量）

默认只检查受改动影响的部分：对比“已检查通过且覆盖到当前线上版本的最近提交”（没有则对比线上版本）与本次提交，由 `deploy/ci/select_tests.py` 选择检查线，结果打印在控制台：

- 只改文档、说明：不跑测试，直接备份发布。
- 后端模块：代码规范与类型检查，加上按导入关系反向找到的受影响测试；搭建、素材、版权方、授权和平台集成区域会附带对应端到端验收与浏览器验收。
- 前端：构建与工作台浏览器测试；搭建或素材页面另跑对应浏览器验收。
- 发布工具：发布门禁测试；发布编排、迁移或镜像本身变化时另跑镜像集成测试。
- 核心配置/调度/数据库迁移/依赖锁/共用测试夹具，或无法归类的文件：自动全量。

端到端验收（后端 `tests/acceptance` 与浏览器验收）默认使用精简规模（2 剧 × 2 账户 × 每剧 5 素材，4 系列/12 广告组/24 广告）；需要满规模（2 剧 × 3 账户 × 23 素材，6/18/36）时设置 `ACCEPTANCE_SCALE=full`，或执行 `python3 deploy/local.py check --full-scale`。

需要全量回归时显式执行 `python3 deploy/local.py check --full` 或 `deploy --full`。同一提交已通过且覆盖到当前线上版本时直接复用结果，不重复检查。通过记录保存在 `.runtime/local-release/checks/`，实测用例耗时缓存在 `.runtime/local-release/ci-cache/`，用于并行分组均衡。

后端测试分两个阶段：普通用例按耗时多组并行（默认按 Docker CPU 数、最多 6 组，`CHECK_BACKEND_SHARDS` 可调），全部结束后，端到端验收（`tests/acceptance`）再单独分 2 组运行（`CHECK_ACCEPTANCE_SHARDS` 可调），避免多分钟级验收与普通用例同时争用测试数据库而超时。每组独立数据库与 Redis 库号；独立测试库从迁移好的模板复制。并行失败的用例会在全新库中单独重跑一次，重跑通过的列为“并行不稳定”，仍失败才阻止发布。后端、前端、发布工具三条检查线同时运行。

日常快速检查：`python3 deploy/local.py check --fast`（可与 `--full` 组合）。快速层跳过 `slow` 标记的后端用例（全部 `tests/acceptance` 及 `backend/tests/slow_tests.txt` 中实测 10 秒以上的测试函数）、浏览器验收和发布镜像集成，结果不写入通过记录，不能作为发布依据；`deploy` 始终运行全部层级。本地直接跑 pytest 时用 `uv run pytest -m "not slow"` 得到同样的快速层。慢用例清单按实测耗时重新生成：`python3 deploy/ci/slow_tests.py .runtime/local-release/ci-cache/test-durations.json`。

仅检查：`python3 deploy/local.py check`；仅做一次一致性备份：`python3 deploy/local.py backup`（会短暂停止服务、排空在途任务，再恢复原版）。

当前批次：`python3 deploy/local.py status`。完整输出位于该命令显示的私有 `execution.log`。历史批次用 `--run <批次目录名>` 查看或恢复。发布期间保持电脑开机、Docker 运行和网络连接。

## 公司 Jenkins 自动发布

推送到公司 Gitea `main` 后，公司 Jenkins 任务 `tiktok-ad-automation`（`http://123.207.4.46:8080`）自动执行与 `python3 deploy/local.py deploy` 完全相同的流程（`Jenkinsfile` → `deploy/ci/jenkins-run.sh` → 工具镜像内的 `deploy/local.py`）。本机命令保留为应急入口；两边同时发布由集群锁阻止。

- 状态与检查通过记录在 Jenkins 私有目录 `/var/jenkins_home/tk-ada-private`（宿主 `/data/jenkins/tk-ada-private`），与本机 `.runtime/local-release/` 结构相同、互不共享。备份本身在集群备份盘上（见下文），两边共用同一份。
- 公司 main 在排队或检查期间前进时，本批在停服前退出（退出码 3），Jenkins 标记为 `NOT_BUILT`，由最新提交的构建发布；不会再白跑完整检查后才在推送阶段失败。
- 控制台会输出各检查线的耗时行（`检查线通过：backend（N 分钟）`），其余检查输出仍只在私有日志。
- 中断处理：在 Jenkins 手动运行该任务，`ACTION` 选 `status` 查看阶段，再按下文选 `recover` 或 `finish`。详细日志在私有目录的批次 `execution.log`，控制台只显示阶段。
- 工具镜像 `deploy/ci/tools.Dockerfile` 与 Docker Hub 依赖镜像副本（`test/tiktok-ad-automation-base:*`）在能访问外网的机器上构建推送；改动工具版本时同步更新 `Jenkinsfile` 的 `TOOLS_IMAGE`。
- 临时检查容器、恢复测试网络和备份卷以 `net.shortclipflix.tkada.ci-owner=$BUILD_TAG` 标记构建归属。`jenkins-run.sh` 退出与 Jenkins `post always` 双重回收；先结束本批工具容器，再删除同标签的测试容器、网络、卷，不按 `tkada-*` 前缀或年龄清理，也不执行全机 prune。Jenkins/宿主异常退出而未执行 post 时，核实构建结束后可运行 `bash deploy/ci/cleanup-resources.sh '<该构建 BUILD_TAG>'`；无标签的历史残留需人工核对，不由脚本猜测归属。

## 本机配置

本机私有配置为 `.runtime/local-release/config.json`，目录权限 0700、文件 0600，不进入 Git。配置示例：

```json
{
  "kubeconfig": "/absolute/private/kubeconfig",
  "retired_source_inventory": "/absolute/private/retired-source.json",
  "tool_paths": ["/absolute/kubectl/bin", "/opt/homebrew/bin"],
  "BUN_BIN": "/absolute/bun",
  "PG_BIN_DIR": "/opt/homebrew/opt/libpq/bin"
}
```

依赖 Python3.14、uv、Bun1.4.2、Node、Docker/buildx、ffmpeg/ffprobe、PostgreSQL18客户端、kubectl。需具备公司 Git 推送凭据、ACR push/pull 登录及已授权 ACK 访问。不需要 age、AWS CLI 或任何备份桶凭据；发布端不再拉取应用镜像。工具不输出真实连接配置。

配置中的 ACK context 固定为 `kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796`，不接受隐式切换目标。旧 prod 资源停用清单用于核对残留资源，不能省略。首次部署参考 `deploy/k8s/README.md`；此入口接受完整旧九角色或新十一角色、各一副本；旧角色任何一个缺失、新角色只出现一个或多出未知角色均阻断。首次 9→11 只在备份与恢复验证通过后创建两个新 Worker。

## 备份位置与保留

最新完整备份固定在新加坡 ACK `test` 命名空间的备份盘 **PVC `tiktok-ad-automation-backup`（50Gi ESSD）的 `/backup/latest/`**，在 `deploy/k8s/tk-ada.yaml` 声明，首次发布在停服前自动创建。备份与恢复验证都在集群内完成，备份文件不回传 Jenkins 或本机（此前跨境回传约 0.5 MiB/s，是停写时间的主要来源）。按用户 2026-10-16 决定不保留异地副本；可用区级故障依赖云数据库自身的每日备份。

- 只保留一份：新备份写入同盘 `/backup/.pending-<token>/`，集群内恢复验证通过后才以目录改名替换 `latest`；失败时旧 `latest` 不变，残留的未完成目录在下次发布开始时清理。
- 余量：辅助 Pod 就绪后、取消 Worker 消费前检查剩余空间 ≥ max(3×上一份 dump, 数据库大小/4, 2 GiB)，不足即停止（此时可直接 `recover`），提示在清单里扩容（存储类支持在线扩容，改 `storage` 后由下次发布的 `apply.sh` 生效）。2026-10-16 数据库 5.66 GB、压缩 dump 约 0.8 GB。
- 发布端批次目录只保存清单、校验和、恢复证明和 `cluster-backup.json`；成功验收后删除其中的私密配置副本，失败现场保留。
- 备份盘只由发布辅助 Pod 挂载，读取备份需在 `test` 命名空间临时起一个挂载该 PVC 的 Pod（与 Beat 盘同一可用区）。

备份包括 PostgreSQL public 业务 schema、Redis DB15 每个键的值与绝对过期时间、Beat 调度文件、旧 SHA 独立源码、运行程序及前端构建、实际私有配置/密钥/MCP注册、部署资源和共享入口证书引用。共享证书私钥、R2素材正文、其他项目数据库/Redis及云平台系统 schema 不在项目归档中。

历史迁移审计目录不属于自动轮换目录，不按文件名盲删。旧证据的整理应在核实新备份覆盖内容后进行。云数据库原有每日备份仍独立保留，此命令不更改它们的策略。

## 实际执行顺序

1. 在独立源码副本中按检查计划并行运行发布门禁、后端检查/测试、前端构建/浏览器测试，以及按需的镜像构建与真实 PostgreSQL/Redis 恢复测试（见上文“检查范围”）。
2. 将指定 SHA 快进推送公司 main，核对远端一致，再构建上传 ACR。
3. 获取集群独占锁，保存全部现存 Deployment UID（首次升级九个，升级后十一个）、原配置和版本。从镜像仓库取旧/新镜像摘要，确保备份盘存在，经运行中的 API 只读查询数据库大小，核对现有资源、开关及只读预检。
4. 停止 API/Beat；创建发布辅助 Pod（挂 Beat 盘、备份盘与按数据库大小新建的临时恢复盘）并检查备份盘余量。取消现存全部 Worker（七个或九个） 的消费，等待在途任务自然完成并连续两次空闲。停止 Worker，保留待处理队列，不清队列或重放 UNKNOWN。
5. 在同一停写窗口把备份直接写入备份盘并生成 SHA256 清单（PostgreSQL 用目录格式 4 进程并行导出、lz4 压缩，目录校验和为按文件名排序的 sha256sum 输出再取 sha256；Beat 盘上人工留存的 `.incident-*` 故障快照不打包）。在辅助 Pod 内只监听回环地址的临时 PostgreSQL/Redis 上 4 进程并行恢复，验证表行数、迁移 head、授权可解密、Redis内容/TTL，并用候选镜像容器试跑迁移；执行前强制覆盖连接并断言只连回环临时实例。
6. 核对恢复证明后以目录改名替换 `/backup/latest`。成功后先迁移指定 broker 消息，再迁移正式数据库并创建新角色；全部角色保持零副本直到迁移完成。
7. 删除本次辅助 Pod（临时恢复盘随之删除），同时启动 API 与本批目标全部 Worker、全部就绪后启动唯一 Beat。候选升级须核对十一 Pod Ready（九 Worker）；单独备份或迁移前恢复按原九/十一角色核对、镜像摘要、实际配置/开关，再查 HTTPS健康，解锁并清理私密配置副本。

普通发版保留线上四个开关的实际值（2026-10-16 核实均为 true），不被本地模板覆盖。OAuth重新授权和真实广告业务写入不属于自动验收，不能把健康检查称为真实业务联调成功。


### 2026-10-18 首次 9→11 与 broker 迁移

新增 `worker-ads-backfill`（队列 `ads-backfill`，prefork 1，申请512Mi/上限1Gi）与 `worker-ads-live-directory`（prefork 4，申请1Gi/上限3Gi）。2026-10-18吞吐修复把共享实时 `worker-ads-reporting` 调至prefork12、CPU申请2/上限4、内存申请4Gi/上限6Gi，事实在途候补24、关联1、历史事实1；候补不是专属执行槽。Control2，其余并发不变。四开关及所有平台配额原值保留。全部Worker显式注入 `DATABASE_POOL_SIZE=2` / `DATABASE_MAX_OVERFLOW=1`，API保留5/10。

27个Worker子进程池上限81，9个父进程预留27，API15，事故期间其它连接实测54，合计177/200；54是快照而非硬上限，发布后仍核对实际连接。必须沿原发布流程冻结旧Worker后启动新Worker；不能临时滚动双开reporting（额外39，预算会到216/200）。十一角色总request 5.75CPU/14Gi、limit16.5CPU/27Gi。原reporting节点释放旧Pod后CPU余量1.935，不足新2CPU；另三个节点可用CPU2.735–3.235、内存6.5–7.8Gi，允许调度迁移，不固定原节点。

停写、正常排空、备份及隔离恢复、备份发布全部成功后，`migrate-targeted` 专门探针只筛选 `ad-management` 及 Kombu 优先级键内 `headers.task == ads.targeted_step` 的消息。Celery `body`、ID、参数及其余 header 不变；仅将 delivery routing_key（及存在时的同名 header）改为 `ads-live-directory`，保证 reject/requeue 不返回写队列。源队列未命中消息保留原字节与顺序；目标已有消息先消费，迁入消息内部仍按原 FIFO，各优先级相互独立。异常优先级键、非标准 exchange/routing、未确认消息、过期队列、类型异常或并发修改都阻断。

队列变化与包含数量/迁移前后 SHA256 的凭据在同一个 Redis WATCH/MULTI 事务中提交，凭据绑定 release token 与备份清单；回包丢失可按相同 token 取回结果。私有批次保留 `broker-migration.json`。不执行 FLUSH，不删除未匹配消息，不重建任务 ID。正式迁移开始前的旧版恢复只针对原角色；一旦进入 broker 迁移或数据库迁移阶段，自动旧版恢复被禁止。候选清单应用只完成部分新角色时也停止并保留现场，管理员核对后继续完整候选，不能容忍缺失老角色或启动半套新合同。

底层 `operations.py` 在旧九角色上扩容时额外校验冻结零副本、原 UID、集群锁、本批发布状态与已经发布的恢复证明；不能绕过编排直接创建两个新角色。首次无历史部署仍走 bootstrap 流程。

真实 Redis 回归（独立回环 DB12；CI 使用专用 DB42，只清理测试随机前缀）：

```bash
TEST_RELEASE_REDIS_URL=redis://127.0.0.1:<本地测试端口>/12 uv run python scripts/test-broker-migration.py
```

## 中断处理

先运行 `python3 deploy/local.py status`，保留所指批次目录：

- 迁移前失败：排除问题后执行 `python3 deploy/local.py recover`，验证原模板/锁/配置，排空在途任务，恢复旧版服务。不会回退数据库或清队列。
- `candidate-held` / `resuming`：执行 `python3 deploy/local.py finish`，继续候选版本启动与验收。
- `broker-migration-started` / `broker-migrated`：可能已移动队列，不能启动不认识新队列的旧镜像；核对本批 Redis 原子迁移凭据及私有 `broker-migration.json`，由管理员继续候选迁移或按一致备份制定恢复方案。命令回包丢失时同 token 重跑迁移为幂等，不能改 token 重跑或手改 phase。
- `migration-started`：需检查迁移 Job、数据库 head 和兼容性后制定继续方案，脚本拒绝自动恢复旧应用。不要手改 phase 或盲目 rollout undo。
- 本机状态丢失或锁创建/删除中断：先核对锁 token、现存角色和迁移状态，由管理员处理。不会按锁的年龄自动抢锁。

`DRAIN_TIMEOUT_SECONDS` 默认1200，超时停止而非强杀在途任务。

## 恢复验证

每份备份目录含 `manifest.json` 与 `restore-verified.json`，恢复证据绑定清单 SHA256；`deploy/release/archive_check.py` 可在挂载备份盘的 Pod 内单独核对校验和与归档结构。手动恢复时在新的私有环境中核对清单，从 `config.tar.gz` 取出原加密密钥，将数据库和 Redis 连接改为全新隔离实例，按 `backup.py:restore_verify` 的步骤恢复并验证；禁止直接覆盖在线库或带旧 AOF 的共享 Redis。每次发布已在集群内完成一次真实隔离恢复。

```bash
node scripts/test-deployment-assets.mjs
python3 scripts/test-release-integration.py
bash deploy/ci/check.sh
```

集成测试用合成数据，覆盖 PG18、Redis7→8重新编码、损坏拒绝、真实 Celery 排空及本地归档。线上发布结果和待验项见 `docs/validation/2026-10-10-local-release.md`。
