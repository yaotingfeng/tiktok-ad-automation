# 经营核算准备与前缀复制可恢复推进

本方案由 root 根据生产只读证据作工程选择，并非用户逐项选型。用户已授权自行处理；本任务不再次请求批准。实现者不提交、不推送、不操作生产，发布由 root 负责。

目标：Control 的 business.schedule 只安排轻候选；business.work 冻结快照、确定可复用边界，并分页复制业务日及汇总。旧正式报表保持可读，全部步骤及原来源/配置/授权复验通过后才能发布。

范围最多四源文件：business/projection.py、tasks.py、新 prefix.py；只有大快照实测需要才使用 source.py。无迁移、新队列、业务开关、CPU/期限扩容。

状态：SNAPSHOT → PREFIX_CHECK → COPY_DAY → COPY_SUMMARY → BASE → SUBSIDY。现有 BASE/SUBSIDY 和已发布版本保持可运行；未知 phase 明确拒绝。定时请求 coalesce 同修订、同 actor 的未完成候选，避免每分钟从头替换；人工 API 默认仍冻结其请求并返回真实 run_ids。

新阶段只加载小元数据/小 coverage 进度；COPY 不读写整 inputs。比较必须用真实约33MB JSON验证，不能每个路径反复解压大JSON。一次复制最多500行，完整主键keyset、事实与游标同事务。source run 每批必须仍为相同 tenant/BC/quarter 的当前正式 COMPLETE 版本，否则候选退役；保持现有清理保护。

SNAPSHOT 属于单个重型工作单元，使用全局业务快照事务锁仅允许一个 Reporting 业务快照同时运行，并设置小于原 soft25/hard30 的有限预算。3秒工作量子不重复启动第二次重型单元。实测内存或耗时不满足时停止扩大实现，交 root 评估，不把 Control 问题转移成 Reporting OOM/永久重试。未变化快照候选只能 SUPERSEDED 并保留原 publication，不能 COMPLETE 空事实。

部署必须先升级全部 Reporting 消费者并确认旧 Pod/fork 退出，再升级 API/control/Beat 新生产者；消息 body 不变。首个新 phase 后禁止自动回退 P，门禁由 root 的私有发布控制器执行。

## 已实现的工程细化

- 实测后采用PG单次展开旧/新states、精确比较原值与complete_through覆盖语义，只返回dirty日期。1.5秒SAVEPOINT快速比较超时则提交按日游标，后续有界推进，不重试同一无界聚合。
- 长pending按原检查周期继续补源：PG最多检查128账号的冻结日期范围，只返回小窗口；扫描完整账号前缀也推进repair_cursor。整段补源使用2秒SAVEPOINT，取消不撤销轻候选及next_check提交。
- 双管理员请求仅可复用相同actor的未完成候选；已正式COMPLETE结果保持跨授权actor复用。
- root明确选择保留last_daily字段/历史值、不迁移，删除已失效的同步eager写入；此未消费标记不作为验收依据。验收next_check/history_cursor、候选phase/source_signature、真实事实及完整publication。
- rows_count保持原计算行计数语义，不把前缀副本计入该字段；准备中小进度另记实际复制行数，验收不得拿rows_count替代两表完整性。
