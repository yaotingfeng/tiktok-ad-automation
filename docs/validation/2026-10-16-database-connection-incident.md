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
