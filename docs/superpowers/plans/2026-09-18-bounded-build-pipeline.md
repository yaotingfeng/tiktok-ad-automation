# 批量搭建流水线 Implementation Plan

> 执行方式：按用户要求在当前会话持续执行，使用 executing-plans；不启动子代理、不等待重复确认。用户明确要求原批次完成前继续。

**Goal:** 完成原180组合，同时用有界并行与结果独立执行解决大批量队头阻塞。

**Architecture:** PostgreSQL持久依赖与窗口控制活跃图，准备和结果各保留一个素材槽。旧消息经正式任务重投递正确路由，原任务栅栏及外部额度保持。

**Tech Stack:** Python/SQLModel/PostgreSQL、Celery/Redis、systemd。

**Spec:** ../specs/2026-09-18-bounded-build-pipeline-design.md

## Global Constraints

仅新加坡测试服；原冻结租户/BC/账户/预算/素材/ENABLE意图不变。活跃素材槽总数2，未知发送不盲重发，不使用运维清队列/改状态/提优先级。

## Task 1：统一窗口与批量准入

Files: app/modules/builds/execution_window.py、app/modules/materials/cover_sharing.py；tests/modules/builds/test_execution_tasks.py、test_execution.py（均位于backend）。

- [x] 三账户实际冻结图复现规划未来封面后领取失败，原代码报cover_claim_lost。
- [x] cover_admission_condition()供规划与单领取复用；当前批次不再选入未来成员，新回归通过。
- [x] window_units()以row_number按tenant/submission分区、drama/unit排序选前10；material_unit_admitted用集合包含而非仅第一行。
- [x] 单槽故障用例显式设置测试槽1；新增默认10槽、超过一波的完整执行和10000组合准入容量回归。关键断言：`len(admitted)==10`，补位后仍10，等待图投递不随总量增长。

## Task 2：结果独立执行与旧路由正式交接

Files: backend/app/jobs/tasks.py、backend/app/modules/materials/cover_tasks.py；deploy/staging-worker.service、新deploy/staging-results.service；backend/tests/jobs/test_worker_queue_isolation.py和queue_isolation_worker.py。

- [x] 新测试先让所有准备槽阻塞，未释放时投递结果，断言3秒内完成；旧两槽混用必须失败。
- [x] prepare_cover回归resources；staging准备1槽、results1槽。旧prepare_cover在结果队列的任务使用`task.retry(queue=dispatch_queue(task.name), countdown=0, max_retries=None)`交接并保留原身份；本地路由/参数通过，真实Linux prefork留发布验收。
- [ ] 测试正确路由不交接、旧路由同ID交接、重复不产生远端重复写、交接故障可由原投递恢复；测试模板总素材槽为2。
- [ ] 扩展实际Redis消费者测试覆盖独立结果服务和10000个准备积压，Linux验证不依赖释放准备槽。

## Task 3：一体回归与正式发布

Files: docs/implementation-progress.md、docs/runbooks/staging-singapore.md、docs/validation/2026-09-17-build-pipeline-liveness.md；私有发布bundle不提交。

- [ ] 运行完整执行/恢复/封面吞吐/双通道/outbox/消费者隔离回归、Ruff/格式/ty app，记录真实PG/Redis容量结果。
- [ ] 聚焦提交并推送；发布bundle覆盖新增results服务的停止、备份、恢复、启动、版本与配置核验，不改变额度和功能开关。
- [ ] 六服务统一版本；素材准备/结果各1、广告/控制各1；备份及独立恢复全部通过，Linux真实消费者测试通过后继续业务验收。

## Task 4：持续业务验收

- [ ] 通过原产品RETRY/RECONCILE恢复确定未发送和未知只读任务，回读恢复进度，不直接调用执行器或改表。
- [ ] 观察源准备、结果核验、窗口补位、广告创建、回读五段真实进度；卡住则记录具体依赖和租约，补回归修代码。
- [ ] 原180组合全部成功并核对无重复创建，再结束目标；未知13来源未取得充分证据前不当作未发送。

## Task 5：业务观察发现的恢复筛选遗漏

- 原等待步骤关联已BLOCKED、确定未发送封面时，RETRY仅查FAILED导致漏选；步骤先失败再重试会重复分轮。将明确三种持久等待状态且满足原COVER_RETRY/权限/无外部效果证据的步骤纳入显式批次重试。不修改冻结数据；已armed/receipt/活动claim仍禁止重传，未来任务仍走10组合准入。
- [x] 新六项回归先验证未发送等待遗漏、已发送仍禁止；修复SQL并跑原恢复/容量/权限回归：51 passed，Ruff/format/ty app通过。
- [ ] 完整备份发布后使用原产品批次恢复，继续180组合验收。
