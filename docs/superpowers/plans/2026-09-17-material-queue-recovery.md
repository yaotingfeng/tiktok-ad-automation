# 素材队列恢复与核验提速实施计划

> 按用户要求在当前会话直接执行，沿用当前 main，不另开分支或启动并行代理。每项先回归复现，再实现和验证。

**Goal:** 阻止仍排队的原任务被反复补投，让已上传素材的核实与封面处理持续推进。

**Architecture:** outbox 在补投前核对 Redis 中完全相同的消息；存在性缓存只能作为索引，必须用实时 LPOS 确认，不能延迟真正丢失的消息恢复。素材核实/封面使用同一 resources Worker 的独立结果队列，保留两个执行槽和全部外部额度。批量核验在每次实际发送前逐项校验领取身份，但同账户、同冻结路由的共同权限只读取一次。

**Tech Stack:** PostgreSQL、Redis、Celery/Kombu、SQLModel、Python；无新依赖、无数据库迁移。

**Spec:** 用户本次要求及 `docs/validation/2026-09-17-cross-bc-material-route.md` 的真实排障证据。

**执行记录（2026-09-17）：** 三项代码工作已实现、验证、推送并发布，后续补发恢复 SQL 索引/期限修复。当前运行 `d22bde3`；下列清单保留原设计，完成证据见 `docs/validation/2026-09-17-material-queue-recovery.md`。一个原始单元的素材到广告及回读已全部成功，整批其余任务仍在执行，不能标记整批完成。

## 安全边界

- 仅新加坡测试服；生产不部署。
- 不清队列、不改变广告账户/预算/素材集合、不重发未知上传。
- 完全相同指 task ID、task name、tenant、actor、payload；错误身份或错误队列不得抑制恢复。
- Redis 不可读或索引不完整时仍允许原至少一次恢复，不把缓存当成业务完成证据。
- 旧结果消息只在发布完整备份后按原始字节原子换队列，身份和原始数量保持；不删除重复项。

## Task 1：阻止队列内消息重复补投

Files: `backend/app/jobs/queued_dispatches.py`、`backend/app/jobs/outbox.py`、`backend/tests/jobs/test_queued_dispatches.py`。

- [ ] 写真实 Redis / PG 回归：队列中原消息经过 3 次恢复仍为 1 份；消息实际丢失后可重新发送；不同 tenant/actor/payload/task 或错误队列不能抑制；畸形消息不能阻止恢复。
- [ ] 用现有 outbox 运行并确认失败，再实现 `QueuedDispatches.contains(queue, task_name, task_id, kwargs)`；批量读取有界，单次 Redis 命令有短超时。
- [ ] `flush_dispatch` 复用 broker 已存在的相同投递作为发布确认，不追加消息；保留原数据库锁、公平轮次和失败退避。
- [ ] 运行 outbox、公平轮次、竞争与新增回归，检查消息丢失恢复不被缓存吞掉。

## Task 2：让核实与封面及时执行

Files: `backend/app/jobs/tasks.py`、`backend/app/jobs/celery_app.py`、`deploy/staging-worker.service`、Compose 消费配置、`backend/tests/jobs/test_worker_queue_isolation.py`。

- [ ] 先写传输/真实消费者回归：资源准备积压时，结果队列中的核实任务可在有限次消费内开始。
- [ ] `materials.verify_target`、`materials.verify_original`、`materials.prepare_cover`、`materials.verify_cover` 统一进入 `resource-results`；所有部署消费者同时监听 resources 和 resource-results。
- [ ] 显式 Redis round-robin；不增加 Worker、并发或额度。原 resources 列表保留并继续消费。
- [ ] 在私有发布流程中对旧结果消息做原子 `LREM 1 + LPUSH` 搬移，记录前后数量和消息摘要；断点可重复执行，不清队列。

## Task 3：降低批量核验的共同权限查询成本

Files: `backend/app/modules/materials/batch_verification.py`、`backend/tests/modules/materials/test_batch_distribution.py`。

- [ ] 对 50 个已知 VID 的真实 PG 核验记录 bounded-session SQL 数；先确认原实现重复做共同账户权限检查。
- [ ] 每次回调仍逐项核对租户、操作者、BC、账户、素材、operation、claim、路由和摘要；全部通过后对共同账户权限执行一次 `_target_access`。
- [ ] 测试同账户 50 项 READY；跨账户/租户、撤权和变更摘要不得通过；保留物理请求前的新鲜检查和原有锁序。

## 集成与发布

- [ ] 运行 jobs、批量分发、跨 BC 约束回归及 Ruff/ty/语法；提交并推送。
- [ ] 遵循 staging/deployment 手册：正常排空、完整备份、隔离恢复、版本切换、五服务九进程配置验证。
- [ ] 记录基线并连续对比任务重复数、队列长度、素材 READY、封面 READY、Campaign/AD 成功数；明确区分未知回执与可重试失败。
- [ ] 更新部署和验证记录；仍存在平台未知结果时不得宣称整批完成。
