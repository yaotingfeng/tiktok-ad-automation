# 过程数据维护

2026-10-29 发布审查发现新增清理草稿与现有维护重复，且全表清空 payload 会破坏仍受业务引用的任务恢复。已统一复用现有 `reporting.cleanup_store` 和 `jobs.compact_dispatches`，没有新增 Beat 定时任务、保留期变更或自动 VACUUM。

- 报表任务及分页沿用 7 天保留期，终态、游标扫描、分批提交、语句超时和跳锁保护保持。
- 投递压缩只处理超过 7 天、可压缩任务名且已无活跃业务所有者的记录，保留参数摘要、任务身份与压缩时间，每次最多 1000 条。素材恢复消息不清空。
- 目录只清理已发布或作废的暂存页；不直接删除运行记录，FAILED 可重试数据受保护。
- 不增加 Control 队列上的重复清理，不全表 DELETE/UPDATE，也不执行 VACUUM FULL。普通 VACUUM 不保证数据库文件大小下降，不能承诺固定释放 GB 数。

在 backend 目录、使用已核对目标环境时：

```bash
uv run python -m scripts.verify_cleanup
uv run python -m scripts.cleanup_process_data
# 显式执行一轮有界维护，不会循环全量清理：
uv run python -m scripts.cleanup_process_data --apply
```

发布本身不运行人工清理。脚本默认只读，统计采用 PostgreSQL 估算活/死行数；后续通过既有周期维护持续消化，不能要求所有超期数据立即为零。停用调度无法恢复已删除数据；本轮没有新增备份或恢复演练。
