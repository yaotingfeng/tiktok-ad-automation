# 2026-10-16 生产数据库连接耗尽与报表同步恢复

目标：新加坡 ACK `c19dbd82590a34b9f87cebb8c1a858796`，生产用途 `test`，入口 `https://tkada.shortclipflix.com`。用户要求直接排查并处理数据库错误和报表停滞。变更前线上版本 `7bea4b9e`，迁移 head `0033_management_rename_values`。

## 证据与影响

- 11:23 CST 左右：`tkada` 中目录 Pod 有 105 个租户行锁等待会话、1 个持锁的 idle in transaction 会话，最早事务超过 3 小时；实例 max_connections 200。
- 近三小时日志按 ERROR 事件统计：API 15、Control 1035、目录 3282、报表 1665、素材资源 90；大量 `TooManyConnections`，目录另有 `SoftTimeLimitExceeded`、`another command is already in progress` 和 QueuePool 耗尽。
- 近八小时报表终态失败中，5292 个为 `tiktok_local_resources_unavailable`（星屿 5215、骏伯租户 77），属于本地连接资源故障，不代表平台权限拒绝。
- Beat 与 Control 均在运行、`ADS_SYNC_ENABLED=true`；目录发布事务缺少领取事务已有的锁超时。Celery 软超时打断 psycopg 后，savepoint/事务回滚失败，物理连接未关闭，锁等待会话不断积累。

## 现场恢复

- 限定数据库 `tkada`、当前用户、故障目录 Pod IP、租户发布锁查询、事务超过两分钟，确认 103 个泄漏会话；102 个终止成功，1 个在清理过程中已退出。不终止其他项目或正常业务会话。
- 正常删除故障目录 Pod，让原 Deployment 以相同版本、配置和并发重建；未清空队列，未改开关或重发广告。
- 11:26–11:28 CST 目录重新完成发布。调用现有 `plan_hot`/`plan_material` 手动刷新流程，为两个现有生产计划中仍归本 BC 的活跃账户补排：21 + 555 个账户，116 个广告日报任务、576 个素材日报任务。使用各计划的独立冻结授权，并逐账户验证读取权限。
- 11:27:55 CST 星屿广告日报已重新写入；其余任务继续由正常队列和额度调度。

## 代码修复与验证

- 领取和发布两个本地事务共用 `_directory_session`：锁超时 20 秒、单语句超时 30 秒、事务空闲超时 10 秒，仅作用于本次事务，不改共享数据库配置。
- 任意异常（包括软超时及其引发的回滚异常）在退出 Session 前 invalidate，直接关闭物理连接，防止连接及行锁泄漏。
- 真实 PostgreSQL 回归覆盖锁竞争超时和 SIGALRM 打断 savepoint 中查询后服务器连接最终消失。新增用例先失败，再修复通过。
- 无数据库迁移、无新增部署开关。上线和最终业务验证结果另行追加。

现场原始日志及查询脚本只保存在 Git 忽略的 `.runtime/incident-20261016-db/`，不提交真实业务明细与运行凭据。

- 发布前检查：干净源码副本中 ads/reporting/队列 238 项通过；ruff、ty、Python 编译通过。共享工作区有其他会话正在修改自动规则，未纳入本次提交；早期一次共享区回归因此遇到 `plan_next_run` 导入中间态，隔离源码后全部通过。

## 发布窗口与应急接管

- 精确推送 `008c63d25e0c11e85d214d9acad8647277a499ee` 到公司 main，触发 Jenkins 自动发布。按当前线上 `7bea4b9e..008c63d2` 计算的全部受影响发布检查另在本机通过 40 项（含报表工作台验收），没有使用快速层替代发布门禁。较早的本机检查因选中陈旧全量基线而主动停止（143），其结果不计为通过。
- Jenkins 构建镜像摘要 `sha256:8c16890b4ee0103a563b6428141a30f88f52a315d0702b8e9b4c6c5a31dafbb2`；本机备用构建在发现 Jenkins 已取得生产锁后停止，未覆盖镜像。
- Jenkins 私有批次 `20261016T033219-7c9547` 在所有 Worker 排空后，被 `broker.unacked != 0` 门禁阻止，尚未生成 PostgreSQL dump、执行正式备份或创建候选迁移 Job。故障目录 Pod 退出遗留 4 条 `ads.sync_step` 原消息；已核对消息队列、任务名和时间，并在所有消费者停止后用 Kombu `restore_by_tag` 原子归还。任务 ID 与业务代数不变，Redis unacked/hash 与 index 均回读为 0；未清队列或重放广告写入。
- 临时恢复旧版 API/报表等服务，健康接口重新 200；目录保持不消费。Jenkins 登录不可用时，不修改其私有 phase：核对旧锁 UID/token、原版九 Deployment、迁移仍为 `0033_management_rename_values`、候选迁移 Job 不存在；恢复中的原版 Pod 使旧发布迁移门禁无法通过。随后用 UID/resourceVersion 条件删除旧辅助 Pod（保留 PVC）和旧发布锁，隔离旧流程。
- 原九角色重新 Ready 后，使用同一干净 `008c63d2` 发布副本、既有本机配置与标准 `deploy/release/release.py deploy` 应急入口重新执行完整备份、隔离恢复、正式部署和验收。原环境开关保持不变。私有状态为 `.runtime/incident-20261016-db/emergency-release-state/`，日志 `emergency-release.log`；最新备份使用 `.runtime/local-release/backups/latest.tar`。

## 最终上线与业务回读

- 标准应急发布 `phase=complete`、退出码 0；线上九角色均为 `008c63d25e0c11e85d214d9acad8647277a499ee`，镜像摘要 `sha256:8c16890b4ee0103a563b6428141a30f88f52a315d0702b8e9b4c6c5a31dafbb2`。九 Pod Ready、七 Worker 就绪、唯一 Beat、HTTPS 健康 200；发布锁及辅助 Pod 已由流程清理。
- 完整备份约 647 MiB PostgreSQL dump，传输是本次维护的主要耗时。隔离恢复校验通过：164 张表行数指纹一致、6 份授权材料可解密、13 个有效 Redis 键逐值/类型/TTL 验证通过；Redis 编码转换 13 个键。候选迁移演练与生产迁移 Job 均通过，head 保持 `0033_management_rename_values`。最新归档 `.runtime/local-release/backups/latest.tar` 已通过校验并替换旧备份。
- 四个实际开关发布前后均为 `true`：素材导入、素材清理、目录/报表同步、自动规则。九角色实际配置、注册文件和镜像摘要一致；本次未改开关。
- 除首轮手动刷新 692 个任务，再核对故障以来仅因 `tiktok_local_resources_unavailable` 失败的报表任务：按 job_key 去重，排除已成功覆盖和现有排队/执行中的请求，并按当前计划、账户归属与冻结授权重新入队。演算 3481 条，实际执行时又有 8 条已恢复，最终创建 **3473** 个正常读取任务；保留原档位优先级和原失败记录。范围含 7 月 27 日起的历史补数，不能用当日刷新替代。原、新任务对应只保存在私有 `recovered-failed-reports.json`。
- 通过既有领取/执行入口推进两条素材任务完成；另优先执行两个 BC 已领取但尚未消费的广告日报任务，复用原 claim 和终态去重保护，星屿、新骏伯各一条 `COMPLETE`，耗时 2.91 / 2.62 秒。未修改历史数据为假完成、未清空队列。
- 12:29 CST 业务回读：新版本已完成 106 个目录发布、27 个素材报表任务，以及两个 BC 各一条广告日报；最新完成分别推进到 04:29 UTC。上线后新增连接资源不可用终态失败为 0。两个现有同步计划均 enabled、disabled_reason 为空。

## 临时容量措施与剩余性能问题

- 新代码阻止了连接持续泄漏，但大目录逐条发布仍可能超过 Celery 55 秒软期限；四并发会争抢同一租户版本锁。初始上线五分钟观测到目录软超时 3 次及受控锁超时，连接数维持十余个，未再出现 TooManyConnections/QueuePool 耗尽。**不能把本次验收表述为所有目录超时均已消失。**
- 已正常取消目录消费，等待 active/reserved/scheduled 均为 0，再用 Celery `pool_shrink(3)` 缩减池并恢复原队列消费。实际子进程列表回读仅 1 个、active 为 1；`stats.pool.max-concurrency` 仍显示启动配置 4，判断实际并发须看 processes。
- 最新回读：tkada 9 个连接、0 个锁等待、目录 Pod 没有超过 20 秒的空闲事务，最老事务约 21 秒；目录与报表继续推进。
- **并发 1 是当前 Pod 的临时运行态，Deployment 启动参数仍为 4，Pod 重启/重新部署会恢复 4。** 后续需优化大目录逐条写入和租户版本锁持有时间，并将验收后的稳定容量配置持久化；重启/发版前核对本记录。当前永久上线的连接失效释放和事务超时保护仍有效。
- Jenkins 的中断批次已通过辅助 Pod/发布锁 UID 条件删除隔离，不再直接对它执行 recover/finish；本次完成状态与恢复依据是本机 `emergency-release-state/`。
