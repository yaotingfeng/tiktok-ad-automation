# Material Pipeline Refactor Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development proportionately; project/user preferences keep this in the existing main workspace and do not require a new branch. Read-only audits may run in parallel; implementation ownership must not overlap.

**Goal:** 将素材准备收敛成单次转存、统一批量共享和无周期性重复核验的流水线，验证后部署。

**Architecture:** 复用现有 MaterialBCSeed、MaterialDistribution、MaterialShareBatch，不增加第二套发送器。上传回执快速发布，内容/账户映射按明确失效更新。搭建依赖以有界切片登记给同一共享调度器。

**Tech Stack:** Python / SQLModel / PostgreSQL / Redis / Celery prefork / official TikTok SDK and application MCP adapters.

**Spec:** docs/superpowers/specs/2026-09-18-material-pipeline-refactor-design.md

## Global Constraints

保留租户、BC、冻结路由、原请求身份、UNKNOWN 和全部证据。只在新加坡测试环境发布；不改生产。所有文件编辑用 apply_patch；不输出凭据，不直接改业务表。测试用独立数据库和各测试进程独立的 Redis DB，不调用真实广告接口。

### Task 1: 回执与映射生命周期

修改 distribution.py、readiness.py、covers.py、cover_execution.py 及需要的图片 DTO/adapter；复用 source_url_uploads.py 的已有成功回执语义。

- [x] 先增加行为测试：一次 URL 成功上传后目标映射 ready，正常路径不追加 get_videos；原结果未知仍只读。
- [x] 增加视频和封面映射早于 900 秒仍可消费、撤权仍拒绝的回归。
- [x] 运行上述测试观察失败，再最小修改共享生命周期；删除涉及范围内重复年龄判断。
- [x] 验证 URL/source upload、cover、build 相关回归，记录命令和结果。

### Task 2: 统一来源与有界矩形准备

修改 distribution_sources.py、bc_seeding.py、batch_distribution.py 和现有搭建准备入口；不得另建 native share 发送器。

- [x] 增加真实 PG 的 20 条素材 × 10 账户首次登记测试，并复跑既有物理共享批次及唯一 seed 上传回归；不把纯函数测试当作完整流程验收。
- [x] 覆盖已有同 BC 与跨 BC seed 就绪两种入口、排除素材、不同 route 不混批、重复消息无重复发送。
- [x] 先观察未聚合行为失败，再集中登记一个有界切片；已发送历史任务沿原账本恢复。
- [x] 对候选选择和锁事务测 SQL 数量/时间，避免新增 200 次完整执行窗口计算。

### Task 3: 并发与发布

修改现有 deploy/staging-*.service 和发布文档；实服私有配置仅在备份后更新。

- [ ] 测试资源池增加执行槽后，结果/搭建仍可推进，硬期限和共享配额生效。
- [ ] 根据服务内存、连接、swap 和完成吞吐选择稳定并发；不将本地限额称作平台额度。
- [ ] 代码审查、相关回归、静态检查，聚焦提交并推送。
- [ ] 完整备份及独立恢复验证后部署，核对六服务实际版本/配置并监控原批次；记录真实结果和未完成项。

## 进度

- 2026-09-18：已核实资源准备/核验/搭建/控制各 1 执行槽；2 核、1967 MiB RAM，可用 414 MiB、swap 已用 1393 MiB，不能盲目加大量 prefork。
- 设计依据为用户对上一轮六项重构方向的明确批准，本轮不重复索要同一授权。
- 旧 UNIT 代际栅栏、等待原消息唤醒及下一片自动接续已补回归。矩阵 SQL 由 11,446 降至 4,596，本地实测 2.61 秒。
- 最终搭建/矩阵/seed 组合 53 passed / 140.66 秒；新分页前等待过滤补跑 3 passed / 15.73 秒。素材合并回归与服务器发布仍在执行，详见对应验收记录。
- 素材最终单次合并 499 passed / 417.41 秒；矩阵/滚动补货/万级窗口最终 16 passed / 66.48 秒。静态检查通过，准备提交与服务器验证。
