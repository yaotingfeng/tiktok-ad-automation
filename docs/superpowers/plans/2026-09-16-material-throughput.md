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

- [x] 将现有轮转/单文件测试改为实际并发、稳定同源和幂等计数断言；RED 专项必须因仍串行/轮转而失败。
  ```python
  first = claim(db, env, ids[0])
  second = claim(db, env, ids[1])
  assert first.advertiser_id == second.advertiser_id
  assert claim(db, env, ids[0]) == first
  ```
- [x] TenantBC 增加 nullable `material_advertiser_id`；首次短事务锁定 BC 后选合法来源并持久化。删除 SOURCE_MAX_INFLIGHT 和负载轮转；保留账户冷却、真实授权和操作幂等。
- [x] 跑 `test_source_selection.py`、`test_url_ingest.py`、`test_unknown_original_fences.py`；迁移升级与已有记录不改来源测试，检查重复回调计数。
- [x] 审查 diff 后聚焦提交：`924150a`。

## Task 2: 源封面准备与图片批量分发

Files: `materials/covers.py`, `cover_models.py`, source finalization integration, image DTO/adapters and focused cover/share tests. 先读取独立合同调查结论确定 MID 证据入口，不能发明字段。

- [x] RED：视频成功后只有一个异步封面任务；重复成功不重复入队；封面失败不改变视频 available。
- [x] 在成功事件后以正常 outbox 处理源封面，不在视频网络回执事务里执行封面 HTTP。沿用 ensure_cover 的身份去重，区分准备阶段上传权限和广告创建权限。
- [x] RED：20 个真实源图片 MID、10 个目标只发一次 IMAGE 共享；已有目标图片不重传；源 image_id 不冒充 MID；共享 UNKNOWN 只回读，不回退上传。
- [x] 接通同源图片批次与目标实际 ID 核实，保留视频/图片分开请求；旧目标图片库存作为事实复用而非旧实现回退。
- [x] 跑双通道封面/批量/重复与未知请求测试，审查提交 `57b745c`；根整合回执及事件随后聚焦提交。

## Task 3: 实际并发、批量与集成验证

Files: deployment configuration/scripts only after measurement; focused throughput tests and validation notes.

- [x] 记录当前 Worker 槽、并发与频率、短/长请求负载；真实 PG/Redis + 传输边界计数验证同源、并发与慢接口耗时。
- [x] 无固定租户均分；保留任务释放与准入逻辑，当前 2 个资源槽不被更小的共享门槛挡住；不盲改 Worker/QPS，不将本地测试说成真实平台吞吐。
- [x] Ruff/mypy/相关回归；最终根整合 272 passed/549.47s，图片历史候选专项 15 passed；记录基线既有故障及测试环境缺项，不伪称全库通过。审查与记录实际待验项。
- [ ] 如测试环境可安全发布，排空、完整备份、隔离恢复、迁移演练、同版本配置核实后部署；正在上传批次和生产不改。

## Progress

- 主账户实现 `924150a` 已独立审查。首次合法选择后持久固定，失效不自动切换；2,000 文件/20 账户测试验证新素材集中，历史来源保持不变。
- 源封面事件与 URL 上传、来源选择、UNKNOWN 围栏及 Redis 准入组合：110 项通过（122.15 秒）；包含旧 VID 延迟事件、BUILD 先创建、处理确认与事务回滚。源事件已处理标记与封面创建同事务，避免将“已有任意封面任务”误认成源准备已处理。
- 主账户历史保留及受影响历史 schema 夹具共 8 项迁移测试通过。仅测试夹具显式播种旧列，生产代码不增加 schema/行为兼容路径。
- 图片共享已完成持久分页与独立批次唤醒，慢接口专项超过 45 秒总时长但单任务小于 40 秒且仅一次 POST。集中回归 221 passed/1 skipped，后续合同快路径 64 passed；原子继续、保留已复用成员、来源所有权和 MID 漂移问题已修复。详见[验证记录](../../validation/2026-09-16-material-throughput.md)。
- 新加坡只读测量：资源 Worker prefork 2；主机约 1,967 MiB 内存，后次读数可用 326 MiB、swap 已用 996 MiB。每接口并发 2、应用/租户/账户并发 4 不额外限制现有两个资源槽；本轮不盲增 Worker 或将本地 QPS 当官方配额。线上尚未切换版本。
