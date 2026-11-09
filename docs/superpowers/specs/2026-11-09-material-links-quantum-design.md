# 后台素材关联三步读取量子

2026-11-09：用户授权通过 root 实现，当前仅本地开发，不提交或发布。源文件所有权为 reporting/jobs.py、sync_control/dispatcher.py、必要的 reporting/job_tasks.py，以及独立审查批准的 sync_control/executions.py 极小中断分支；不触碰经营模块及 ads/tasks.py。

现有 P 五分钟证据：209 个关联步骤涉及 204 父，202 WAITING、7 COMPLETE，HTTP 中位373ms，单步中位2.91s。S 只读样本6075活跃关联父中有旧定位分页已等待约67分钟；不据历史日期取消，也不把10个目录head误报为后台读取槽。该变化减少每单请求后重新排队，不调整平台限额、缓存300秒、六小时负证据期限或池公平权重。

仅 execution_pool=background 且 kind=material_links 使用最多3步、20秒绝对量子；其他kind及实时当前日关联保持单步。新canonical dispatcher在原成本主端点预留3单位。consumer按tenant/job/generation/主端点读取既有SyncCostLedger.reserved_units，max_steps=min(3,granted_budget)，预算缺失或0不执行读取。旧grant1不能扩成3步；新grant3由旧consumer只读1步时正常按真实收据结算未使用预留。

同一gen/private token/head grant及原240秒租约内，每小步独立提交已证明的progress和链接。每次HTTP前后继续复用TransactionBoundaryReader的owner/代数/token/route核验，网络期间不持业务事务。20秒从worker入口冻结，reader期限取原170秒与量子截止的较早值，不逐步重开；已完成一步后剩余不足即交回。仅量子最终一次outcome清租约及补位。COMPLETE、依赖退避、准入错误、业务错误或失租立即结束；不能将WAITING状态再次当本代RUNNING绕过授权。

每次发送仍走原准入、单个execution_cost_context和dispatch_context；实际重试/辅助端点由observer真实记账，绝不把已发送请求标成NOT_SENT或退款。预留单位是既有成本估计而非允许绕过平台上限；后续逻辑步还受已用真实请求数/原预留剩余约束，内部SDK原重试策略不扩大。每小步request_count只累计该步新增发送，异常不能回滚之前已提交进度。新旧版本混合不改变消息正文、数据库结构或租户冻结语义。

验证使用真实PostgreSQL与Redis：3步可见提交、第二步错误、owner/gen/token/route/head替换、旧grant1、缺/零预算、截止时间、依赖/完成停止、实时与其他kind单步、一次释放与补位、真实费用/旧consumer闭合。外部服务替身仅在reader/传输边界，不调用SDK或生产。

独立审查补充：在二次预算读取和循环前无条件核验原head，预算消失也不能绕过后继保护；SDK_SCOPE_INTERRUPTS原样传播，后继owner/gen/token同时变化不能以ExecutionLost遮盖中断、将unknown成本误作完成收据。
