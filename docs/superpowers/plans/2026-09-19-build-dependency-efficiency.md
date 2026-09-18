# 构建依赖效率实施计划

设计依据：`docs/superpowers/specs/2026-09-19-build-dependency-efficiency-design.md`

## Task 1：直接落定明确的素材与封面终态

Files: `backend/app/modules/builds/material_execution.py`、`backend/app/modules/builds/cover_execution.py`、相关测试。

- 先增加 PENDING 视频 ready/blocked 与封面 READY/BLOCKED 的失败回归，断言直接落定且不产生 Step 投递。
- 扩展候选查询和锁内状态复核，复用现有权限、冻结路由、映射和远端效果栅栏。
- 保留 UNKNOWN、armed、候选 ID、权限失败和不一致映射的原语义。

## Task 2：收窄通用依赖唤醒并减少单元写放大

Files: `backend/app/modules/builds/dependency_waits.py`、`backend/app/modules/builds/dispatch.py`、相关测试。

- 先增加明确终态不由通用唤醒器投递，以及 future due 首次提前、past due 后续不改写的回归。
- 通用唤醒仅保留 material `result_unknown`、cover `UNKNOWN` 和窗口规划职责。
- `wake_unit` 使用单次 now，并给 UPDATE 增加 `due_at > now` 条件；保留现有单元消息合并和 repair。

## Task 3：首次部署容量文档

Files: `docs/runbooks/bootstrap-deployment.md`、`docs/runbooks/deployment.md`、`docs/runbooks/staging-singapore.md`。

- 增加主机资源、数据库/Redis/API 拓扑、调用策略和 Worker MemoryPeak 采集清单。
- 增加内存、CPU、连接池和上游额度四重边界，以及逐槽压测、观察指标和回退阈值。
- 标注测试环境当前配置只适用于低配置测试机，未来生产必须重新评估并记录最终值与证据。

## Task 4：验证、进度与聚焦提交

Files: `docs/implementation-progress.md`。

- 运行目标真实 PostgreSQL/Redis 测试、相关构建恢复回归、Ruff、格式和 ty 检查。
- 记录行为变化、测试证据和未执行事项；检查仓库根目录、状态和暂存差异后做一次聚焦提交。
- 不自动推送或部署；发布需另行确认，并按手册完成全量备份和恢复验证。
