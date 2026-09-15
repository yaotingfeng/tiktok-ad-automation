# 搭建批处理实施计划

> **For agentic workers:** Use superpowers:executing-plans task-by-task; independent domains use superpowers:dispatching-parallel-agents. 用户已批准方案，按项目约定保留现有分支并限定文件归属，不创建新分支或重复请求批准。

**Goal:** 让共享/核验按批次执行、广告创建不被素材阻塞，并将已验证补建关联回原任务。

**Architecture:** 官方gateway之上的持久批次与逐成员证据，独立只读批量接口；不可变纠正关系派生业务完成状态，原UNKNOWN历史不改。共享准入、冻结route/claim和数据库迁移由根代理统一集成。

**Tech Stack:** Python/SQLModel/PostgreSQL/Redis/Celery prefork、官方TikTok SDK/MCP、React/shadcn。

**Spec:** `docs/superpowers/specs/2026-09-15-build-batching-design.md`

## 执行检查点

2026-09-15：Task 1–4 代码与专项回归完成、独立审查闭环。Task 5 迁移及本地验收完成，根集成复测/固定提交发布进行中。实际测试文件为 `test_material_bulk_reads.py`、`test_cover_bulk_verification.py`、`test_batch_distribution.py`、`test_shared_mid_discovery.py`、`test_verified_replacements.py`；目标任务入口无需新增 Celery task，在已有 distribution 入口拼批。新增 MID 查询解决共享后目标 VID 未知仍逐项搜索的热点。详细计数及未验边界见 `docs/validation/2026-09-15-build-batching.md`。

## Global Constraints

- 基线`356269b`；现有`feat/platform-implementation`，不推送、不新建分支。
- 共享最多20不同MID×10目标，矩形每个成员均为已有授权待执行操作；禁止跨租户/BC/冻结授权拼批。
- 只读批量1–50唯一ID；900秒素材时效不放宽；未知写入永不盲重放。
- TDD；真实PG/Redis，外部替身只在HTTP边界；无真实广告压测。
- 测试服每次变更前完整备份和隔离恢复；生产不修改。

### Task 1：批量读取与封面verify合并（bulk_reads_v2）

**Files:** contracts/materials.py、modules/materials/sdk_assets.py、adapters对应材料模块、covers.py/cover_tasks.py；新增`tests/modules/materials/test_bulk_reads.py`与批量封面回归。

**Interfaces:** 两通道都提供以下关键接口，供共享任务直接使用：

```python
def read_videos(*, advertiser_id: str, video_ids: tuple[str, ...], budget: RemoteCallBudget) -> tuple[VideoRecord, ...]: ...
def read_images(*, advertiser_id: str, image_ids: tuple[str, ...], budget: RemoteCallBudget) -> tuple[ImageRecord, ...]: ...
```

- [x] RED：23个视频和23图片分别只用一次业务调用；非法/重复/请求外ID拒绝，缺失逐项识别；现有单项解析应保持行为。
- [x] GREEN：SDK使用完整数组、共同严格解析；接入`run_cover(read=True)`同scope待核验成员，保留每个claim/revision。
- [x] 回归：权限/claim在物理发送前变动零写入；KNOWN/候选/receipt conflict/UNKNOWN不得重传，单候选复用保持1次图片GET。
- [x] 执行`pytest -q tests/modules/materials/test_bulk_reads.py tests/modules/materials/test_cover_fast_path.py`及新增批量封面测试；Ruff、mypy后冻结，根代理整合。

### Task 2：完整矩形共享与逐成员恢复（batch_sharing_v2）

**Files:** distribution.py、tasks.py目标分发入口、新share batch模型/执行模块和独立测试。模型与迁移接口先交根代理。

**Consumes:** Task 1 `read_videos`；现有`share_assets(AssetShare(...))`、逐分发原operation/revision/claim。

- [x] RED：20素材×10目标只发1次share；21×11分批仍每个合法组合恰好一次；跨scope不拼批。
- [x] GREEN：领取合法矩形，持久化批次/成员、冻结请求和摘要，短事务arm全体；网络回调重新验证全部成员。
- [x] 恢复：重复投递/进程中止/UNKNOWN都不重发；部分目标缺失仅继续读取；精确NOT_SENT可受控恢复。
- [x] 实际PG双连接竞争领取，断流HTTP替身确认send次数；运行既有分发、素材恢复和新批次组合后冻结。

### Task 3：已验证纠正台账与任务展示（correction_closure_v2）

**Files:** builds新增纠正model/service，submission读模型/聚合/恢复候选，frontend任务详情/列表及测试。根代理负责schema注册和迁移。

- [x] RED：原6成功+2有正证据补建，业务聚合8成功0待核实，旧UNKNOWN正文/ID不变；替代组/广告ID可见。
- [x] GREEN：内部导入服务真实gateway读取新组/广告及旧组停用，严格比对允许差异后写不可变唯一关系。
- [x] 负向：wrong tenant/BC/actor/route/原digest/新creative/组状态/重复归属拒绝；并发重复导入幂等；已解决旧项不能再次恢复。
- [x] 前端显示完成和补建计数，保留历史详情，已有无纠正任务行为不变；真实PG测试、Playwright、类型检查和构建通过后冻结。

### Task 4：队列隔离与可复现容量（root）

**Files:** 新staging builds消费者模板、现有staging-worker.service；新增`backend/tests/jobs/test_worker_queue_isolation.py`与容量报告。

- [x] RED：从实际部署模板解析队列，资源阻塞期间广告必须由不同消费者执行；使用独立Redis前缀及事件，不以源码字符串存在代替行为。
- [x] GREEN：resources/builds分开消费者；保留prefork和既有硬时限、control独立；先检查主机可用内存再确定并发，不扩大未经验证的上游额度。
- [x] 100/1000/10000阶梯合成负载测真实模块调用/批次/结果；验证共享quota下并行批次达到允许并发，429退避不占worker睡眠。
- [x] 记录冷启动/新鲜复用/过期复验分开结果；不把本地调用数下降等同平台端到端同倍加速。

### Task 5：迁移、集成与测试服发布（root）

**Files:** app/alembic/env.py、迁移文件、必要模型注册、docs/runbooks/staging-singapore.md、docs/implementation-progress.md、验证记录；私有发布/导入脚本不进Git。

- [x] 读取Agent实际模型，写迁移RED：从现有head升至新head，旧步骤/摘要/队列/UNKNOWN不变，新约束拒绝跨scope和重复替代。
- [x] GREEN：审查后的Alembic增加新表/约束/索引，不修改历史迁移；新库完整upgrade及旧head升级回归通过。
- [ ] 根集成共享/封面/创建/恢复/聚合及前端测试；逐文件检查、Ruff/mypy/构建和diff-check。检查暂存范围后聚焦提交。
- [ ] 固定提交归档、实际前端构建；停止新写入与Beat，正常排空全部消费者，完整备份和隔离PG/Redis恢复、迁移演练、归档摘要/解密验证。
- [ ] 正式升级测试库并切版本，保留开关和授权；验证所有进程同SHA/配置、无异常重启、HTTPS/登录/队列。
- [ ] 使用内部真实只读验证服务导入此前两条纠正：原组DISABLE、新组/两AD MATCH、原六条不变；认证任务API及页面显示已完成/补建2，原历史留存。
- [ ] 更新验收报告、实施进度并聚焦提交文档；不推送，不创建额外真实广告。
