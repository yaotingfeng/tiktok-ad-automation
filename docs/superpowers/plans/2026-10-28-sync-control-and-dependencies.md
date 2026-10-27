# 同步控制与目录依赖 Implementation Plan

> **For agentic workers:** 使用现有分工代理执行独立任务，根代理集成和发布；不启动额外流程或新分支。

**Goal:** 消除健康重扫争用、错误目录淘汰及依赖恢复受阻，统一验证后一次发布。

**Architecture:** 分离分钟新鲜度与有明确观测时间的完整覆盖检查；目录替代按冻结查询覆盖判断；后台窗口给依赖读取保底，关联维护独立有界事务。

**Tech Stack:** Python/SQLModel/PostgreSQL18/Redis/Celery/React/Playwright。

**Spec:** ../specs/2026-10-28-sync-control-and-dependencies.md

## Global Constraints

- schema0045、四开关、Worker并发及物理端点配额不变；无新DDL或云扩容。
- 冻结tenant/BC/actor/route、观察版本、有效lease及完整发布不能放宽。
- 同task原payload，Redis进度不是业务完成事实；轻重结果不能相互倒退覆盖。
- company main仅最终统一发布；保留无关untracked，不自动新增分支。

## Review Focus

- actor/route/owned/rule/timezone改变后旧覆盖或旧请求能否误生效：health范围测试、supersede跨范围测试。
- Redis丢失/DB提交失败/交错事务导致假完整：health双频真实PG/Redis测试。
- 部分live是否吞掉include_deleted历史依赖：targeted覆盖及cleanup一致性测试。
- 签名超时使固定前50任务反复占队：maintenance保存点与跨事务轮转测试。
- targeted保底能否挤死全量/扩大总窗口：scanner两类保底/借用/tenant fairness测试。

## Task 1: 双频健康测量

Files: backend/app/modules/reporting/health.py、相关health tests；root负责schemas.py/api.py、ReportStatusBar.tsx、生成类型及API/浏览器测试。
Interfaces: measure_freshness/measure_coverage；metrics coverage_observed_at/coverage_completed_at/coverage_scope/coverage_status。row.observed_at只轻量时间；coverage_issues(metrics,now)复用过期规则。

- [x] 真实PG红灯：分钟调用不执行目录/关联SQL，management只一次；覆盖首轮/失败/过期均不能健康。
- [x] 实现轻重分离、3分钟触发/5分钟有效、120秒完整周期及v2进度，交错提交短锁合并。
- [x] 覆盖scope、旧cycle、Redis丢失/commit失败、边界与相应旧测试通过。
- [x] API输出两类时间，过期独立判定；页面显示覆盖观测与未确认告警，针对性浏览器测试通过。

## Task 2: 目录范围替代与背景保底

Files: ads/directory_supersession.py、sync.py/tasks.py、reporting/job_tasks.py cleanup及_dispatch_directories、相应ads/reporting tests。
Interface: superseding_directory_predicate(older,newer)共享SQL表达式，实例与alias均可。

- [x] 真实PG复现不相交live/删除范围/冻结身份误淘汰历史targeted。
- [x] 执行/发布/cleanup接入同一覆盖条件，现有观察版本保护不变。
- [x] 背景总8窗口targeted/full各4保底、空闲互借，类内年龄及租户轮转保持。
- [x] 测试scope与subset/parent/child/租约、两类公平、旧query计划及性能边界。

## Task 3: 关联维护不会互相阻塞

Files: reporting/link_work.py、job_tasks.py plan_due_runs维护调用、对应tests。
Interfaces: 原reconcile_link_work与wake_link_dependencies签名保持。

- [x] 真实PG复现候选扫历史、超时整批回滚、有效租约错误唤醒。
- [x] 物化active候选、PK重验，逐job SAVEPOINT和deadline，超时只更新复核游标。
- [x] wake与reconcile独立事务、先wake再归并、各1秒；异常不挡正常派发。
- [x] 仅读生产SQL对照验证访问量与时间，租户/route/claim/负证据测试通过。

- [x] 普通ad/creative观察版本与素材归属保护，存量误淘汰依赖按真实缺口安全恢复并保留触发集合；真实PG原子回滚验证通过。

## Task 4: 集成与单次发布

- [x] 三个修改面独立审查，针对性PG/Redis/API/前端验证及静态检查；只因相关变化重跑覆盖。
- [x] 更新运行契约与验收证据，明确完整覆盖时限与剩余来源限制。
- [x] 聚焦提交，暂停Jenkins避免重复发布，仅推company，精确干净checkout构建。
- [x] 保留正常退出宽限，11角色SHA/digest/config/HTTPS/单Beat/0045核对；恢复Jenkins。
- [ ] 真实完整coverage周期与固定历史/依赖样本推进验收，未达标不宣称全部修复。

## Task 5: 实际验收暴露的两处续步阻断

首次9576233a发布基础验收通过，业务验收未完成。query代理只负责health.py及相关tests；cleanup代理只负责spark_identity.py及相关tests；root负责集成文档、独立审查与最终补全发布。

- [x] 分钟失败与coverage调度解耦，保留当前scope/授权、失败指标与outbox原子性；真实PG失败路径先红后绿。
- [x] 水位按512账户分批，10秒总/5秒单句预算不变，不发布部分结果；一次生产只读验证实际成本。
- [x] Spark同scope资产摘要跨长队列续步保存，范围变化失效，真实唯一签名/大小匹配；2小时续步回归先红后绿。
- [ ] 两项独立审查、受影响测试、静态检查通过后一起补全，禁止每项单独发版。
- [ ] 验收后续完整coverage/分钟成功恢复、固定Spark真实进入帖子阶段、原固定目录继续推进，未达到项如实记录。
