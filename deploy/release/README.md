# 本地一键发布

目标固定为新加坡 ACK 的 `test` 命名空间，生产入口 `https://tkada.shortclipflix.com`。在本机完成检查、推送公司 main、构建上传镜像、发布前备份、迁移、部署和验收。无需 Jenkins、备份云盘或 OSS。日常数据库备份继续使用已有 PolarDB/Tair 托管策略。

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

依赖 Python3.14、uv、Bun1.4.2、Node、Docker/buildx、ffmpeg/ffprobe、PostgreSQL18客户端、kubectl。需具备公司 Git 推送凭据、ACR push/pull 登录及已授权 ACK 访问。不需要 age、AWS CLI 或任何备份桶凭据。工具不输出真实连接配置。

配置中的 ACK context 固定为 `kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796`，不接受隐式切换目标。旧 prod 资源停用清单用于核对残留资源，不能省略。首次部署参考 `deploy/k8s/README.md`；此入口只用于现有九角色、各一副本的部署。

## 备份位置与保留

最新完整备份固定为 **`.runtime/local-release/backups/latest.tar`**（在运行发布命令的这台电脑上）。保留最近一份成功且隔离恢复通过的归档：新归档回读成功后才原子替换旧文件。归档是本机私有明文文件，不上传云存储，权限 0600；目录 0700。每次成功验收后清除本批次重复 dump/配置，只保留状态与验证记录；失败现场保留，供恢复处理。

备份包括 PostgreSQL public 业务 schema、Redis DB15 每个键的值与绝对过期时间、Beat 调度文件、旧 SHA 独立源码、运行程序及前端构建、实际私有配置/密钥/MCP注册、部署资源和共享入口证书引用。共享证书私钥、R2素材正文、其他项目数据库/Redis及云平台系统 schema 不在项目归档中。

历史迁移审计目录不属于自动轮换目录，不按文件名盲删。旧证据的整理应在核实新备份覆盖内容后进行。云数据库原有每日备份仍独立保留，此命令不更改它们的策略。

## 实际执行顺序

1. 在独立源码副本中按检查计划并行运行发布门禁、后端检查/测试、前端构建/浏览器测试，以及按需的镜像构建与真实 PostgreSQL/Redis 恢复测试（见上文“检查范围”）。
2. 将指定 SHA 快进推送公司 main，核对远端一致，再构建上传 ACR。
3. 获取集群独占锁，保存九 Deployment UID、原配置和版本。验证旧/新镜像、本机备份目录、现有资源、三个开关及只读预检。
4. 停止 API/Beat；取消七 Worker 的消费，等待在途任务自然完成并连续两次空闲。停止 Worker，保留待处理队列，不清队列或重放 UNKNOWN。
5. 在同一停写窗口备份，通过集群传输校验 SHA256。本机 Docker 内部隔离网络恢复 PostgreSQL/Redis，验证表行数、迁移 head、授权可解密、Redis内容/TTL，试跑候选迁移。
6. 生成并完整回读本地归档，原子替换 `latest.tar`。成功后才迁移正式数据库，九服务保持停止直到迁移完成。
7. 删除本次临时工具 Pod，依次恢复 API、七 Worker 和唯一 Beat。核对九 Pod Ready、镜像摘要、实际配置/开关、HTTPS健康，再解锁和清理重复备份。

普通发版保留线上开关值（当前 true / true / false），不被本地模板覆盖。OAuth重新授权和真实广告业务写入不属于自动验收，不能把健康检查称为真实业务联调成功。

## 中断处理

先运行 `python3 deploy/local.py status`，保留所指批次目录：

- 迁移前失败：排除问题后执行 `python3 deploy/local.py recover`，验证原模板/锁/配置，排空在途任务，恢复旧版服务。不会回退数据库或清队列。
- `candidate-held` / `resuming`：执行 `python3 deploy/local.py finish`，继续候选版本启动与验收。
- `migration-started`：需检查迁移 Job、数据库 head 和兼容性后制定继续方案，脚本拒绝自动恢复旧应用。不要手改 phase 或盲目 rollout undo。
- 本机状态丢失或锁创建/删除中断：先核对锁 token、九服务和迁移状态，由管理员处理。不会按锁的年龄自动抢锁。

`DRAIN_TIMEOUT_SECONDS` 默认1200，超时停止而非强杀在途任务。

## 恢复验证

每个归档含 `manifest.json` 与 `restore-verified.json`，恢复证据绑定清单 SHA256。解包到新的私有目录，核对清单后从 `config.tar.gz` 取出原加密密钥；将数据库和Redis连接改为全新隔离实例。使用 `backup.py:restore_verify` 的算法恢复并验证，禁止直接覆盖在线库或带旧 AOF 的共享 Redis。发布时已完成一次真实隔离恢复。

```bash
node scripts/test-deployment-assets.mjs
python3 scripts/test-release-integration.py
bash deploy/ci/check.sh
```

集成测试用合成数据，覆盖 PG18、Redis7→8重新编码、损坏拒绝、真实 Celery 排空及本地归档。线上发布结果和待验项见 `docs/validation/2026-10-10-local-release.md`。
