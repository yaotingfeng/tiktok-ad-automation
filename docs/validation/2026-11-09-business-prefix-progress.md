# 2026-11-09 经营核算有界准备与前缀复制

状态：代码/真实PG回归/独立审查完成，生产待按专门顺序发布。

生产队首BC4859可读账号，Q4旧输入33.8MB、184642状态，旧版本598081业务日事实。实际调度快照/JSON加无界复制超过25秒，回滚后next_check长期不变，其它BC受阻；只读源SELECT计划不足0.5秒，无锁阻塞证据。不能据此断言每次都复制全部旧事实。

采用现有business.work的可恢复阶段：Control仅安排轻SNAPSHOT候选，同口径未完成候选合并；Reporting单重型18秒单元冻结输入。未变候选空inputs退役，保留旧正式版本。PREFIX_CHECK数据库精确比较states及complete_through，快速整段比较触及预算时仅回滚SAVEPOINT，持久按日进度；COPY_DAY/COPY_SUMMARY完整主键每批至多500行，数据和游标同事务。两表完成前不进入BASE/补贴/原来源复验与正式publication切换。copy阶段仅加载小元数据，不反复读取33MB inputs。

每批验证基准仍为同tenant/BC/quarter当前COMPLETE出版物，否则候选退役；保留旧正式查询与导出。未知阶段拒绝，不当作补贴处理。人工未完成候选严格同actor，正式COMPLETE可依原范围复用；长pending仍每轮以2秒SAVEPOINT/128账号有界补源，日期受member start/end及complete_through约束，取消不撤回已安排候选与next_check。last_daily仅保留历史字段，删失效同步写分支，不作为进展证明。

验证：全business80项通过，最终日期边界/大JSON与补源取消追加3项通过；三源严格类型、Ruff/格式/差异通过。真实4859账号/184642状态、冻结JSON34558027B，SNAPSHOT约2.66秒、进程峰值约598MiB；精确dirty比较约1.35秒，128账号补源约0.10秒。崩溃/重放/源出版物替换/配置与actor变化/未变候选与完整发布均覆盖。独立审查两项Important已修，无剩余阻断。仅本地量测，不宣称生产达到同一倍数。

无DDL/新开关/新队列，但存在持久任务阶段合同变化。选择专门发布：先取消并正常排空旧Reporting消费者，确认旧Pod/fork全部退出，零副本换新镜像并核验12fork；之后才能升级API/control等新生产者，BG仍12正常排空、Beat最后。新consumer自身也可能创建阶段，因此启动新Reporting之前持久forward-only标记，任何失败保锁/CI暂停、只前向修复，禁止自动回旧S。不创建备份或恢复演练，按用户自行修复发布授权执行并如实记录；正式版与四开关/额度/schema0101保持精确核验。
