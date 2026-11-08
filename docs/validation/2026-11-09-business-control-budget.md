# 经营周期调度的重复快照读取与软超时连接处理（2026-11-09）

状态：代码、真实PG18验证与独立审查通过，待精确发布及运行验效。用户已授权自行推送生产并继续修复问题。

## 实际问题和最小修复

a88稳定窗口约12分钟内，Control的business.schedule有10次25秒软超时、2次another command already in progress；3个经营范围next_check均逾期。堆栈落在request_rebuild候选查询的psycopg JSON解码，已发布快照的inputs压缩存储最大约2.24MB。软中断可能打断驱动取结果，普通回滚后把物理连接交回池会污染后续Control任务。

候选过滤、BC写互斥、租户/季度/签名/发布身份和排序不变，先只查候选ID，session.get复用同事务已加载版本，避免为了检查签名重复传输/解码inputs与coverage。Celery单次25秒唤醒按原next_check顺序处理一个BC并先提交；当前3个经营范围与每60秒唤醒、五分钟复查间隔相容，跨BC大快照不会把前BC调度一起回滚。软超时在Session退出前invalidate物理连接、释放事务锁，再抛原异常。没有延长SQL/任务/租约期限，没有新队列、槽位或额度变化。

## 验证与限制

两项新增真实PG回归在旧码均红：候选SELECT仍包含大JSON，单连接池在软超时后返回相同backend PID。修复后全business模块59项通过25.68秒，包含原BC/FK并发、配置修订/旧发布围栏、原子发布和权限隔离；新用例确认对象复用且不再选择inputs列，软超时后PID更换且事务advisory锁可重新获得。两源mypy、Ruff/格式通过，独立复核无Critical/Important。

测试不保证单个真实BC必然在25秒内完成；发布后须回读逾期范围next_check、真实新候选与Control异常。原生产发布锁、CI原状态、正常退出/后台排空、11角色/0101/配置/资源（publisher4进程2CPU、Control3进程4Gi、后台8）保持。按无迁移快修授权不创建备份或恢复演练，不能把未执行事项记为通过；没有真实广告或权限写入。
