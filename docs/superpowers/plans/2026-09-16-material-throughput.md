# Material Throughput Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution; a bounded independent contract investigation may run separately. User-approved scope takes precedence over optional workflow ceremony.

**Goal:** 集中素材并消除无依据串行，减少搭建临界路径的重复上传与查询。

**Architecture:** 沿用现有素材操作账本、outbox、20×10 共享和封面任务。BC 固定主素材账户，来源计数不再限制不同文件并行；封面准备与视频成功事务解耦，图片共享必须有真实 MID 与目标回执。

**Tech Stack:** Python 3.14, SQLModel/PostgreSQL, Celery/Redis, official SDK/MCP.

**Spec:** `docs/superpowers/specs/2026-09-16-material-throughput-design.md`

## Global Constraints

- 不保留新旧兼容/失败回退路径。
- UNKNOWN 请求只核实，不重发或改用另一种写入路径。
- 正在上传的批次不重建、不迁移；生产环境不在本次范围。

## Task 1: 持久主账户与不同文件并发

Files: `accounts/models.py`, new Alembic revision, `materials/source_selection.py`, `tests/modules/materials/test_source_selection.py`.

- [ ] 将现有轮转/单文件测试改为实际并发、稳定同源和幂等计数断言；RED 专项必须因仍串行/轮转而失败。
  ```python
  first = claim(db, env, ids[0])
  second = claim(db, env, ids[1])
  assert first.advertiser_id == second.advertiser_id
  assert claim(db, env, ids[0]) == first
  ```
- [ ] TenantBC 增加 nullable `material_advertiser_id`；首次短事务锁定 BC 后选合法来源并持久化。删除 SOURCE_MAX_INFLIGHT 和负载轮转；保留账户冷却、真实授权和操作幂等。
- [ ] 跑 `test_source_selection.py`、`test_url_ingest.py`、`test_unknown_original_fences.py`；迁移升级与已有记录不改来源测试，检查重复回调计数。
- [ ] 审查 diff 后聚焦提交。

## Task 2: 源封面准备与图片批量分发

Files: `materials/covers.py`, `cover_models.py`, source finalization integration, image DTO/adapters and focused cover/share tests. 先读取独立合同调查结论确定 MID 证据入口，不能发明字段。

- [ ] RED：视频成功后只有一个异步封面任务；重复成功不重复入队；封面失败不改变视频 available。
- [ ] 在成功事件后以正常 outbox 处理源封面，不在视频网络回执事务里执行封面 HTTP。沿用 ensure_cover 的身份去重，区分准备阶段上传权限和广告创建权限。
- [ ] RED：20 个真实源图片 MID、10 个目标只发一次 IMAGE 共享；已有目标图片不重传；源 image_id 不冒充 MID；共享 UNKNOWN 只回读，不回退上传。
- [ ] 接通同源图片批次与目标实际 ID 核实，保留视频/图片分开请求；旧目标图片库存作为事实复用而非旧实现回退。
- [ ] 跑双通道封面/批量/重复与未知请求测试，审查提交。

## Task 3: 实际并发、批量与集成验证

Files: deployment configuration/scripts only after measurement; focused throughput tests and validation notes.

- [ ] 记录当前 Worker 槽、并发与频率、短/长请求负载；真实 PG/Redis + 传输边界计数比较同源与散源、并发与耗时。
- [ ] 无固定租户均分；检查等待重试不占名额，现有队列充分利用空闲资源；上传/共享/查询分别测量参数，不盲改 QPS。
- [ ] Ruff/mypy/相关全套回归；记录基线既有故障，不伪称全库通过。审查与记录实际待验项。
- [ ] 如测试环境可安全发布，排空、完整备份、隔离恢复、迁移演练、同版本配置核实后部署；正在上传批次和生产不改。

## Progress

- 初始：代码未改。主账户策略采用首次合法选择后持久固定；失效不自动切换。图片共享合同正在独立只读核对。
