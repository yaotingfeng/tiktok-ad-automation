# 单轮同步服务耗时修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 缩短真实账户事实从待办到完整入库的耗时，纠正只缩规划周期、未验证全实例容量的问题。

**Architecture:** 当前623活跃账户共用SDK应用配额，事实和素材关联混用4个数据库派发窗口、完成后经两次outbox跳转补位。将当前事实、关联、历史分为有界独立派发lane，事实8、关联1、历史1；完成后直接执行原本地领取事务，仍保留outbox和租户公平。已有零花费非空事实必须继续真实取数，不能只确认零或永久跳过。

**Tech Stack:** PostgreSQL / SQLModel, Redis / Celery prefork, 原官方TikTok SDK, ACK。

**Spec:** 用户本轮明确要求降低“单次同步所需要的时间”；生产证据与边界见 docs/validation/2026-10-18-realtime-sync-reliability.md。

## Global Constraints

- Junbo业务对账使用现有冻结OFFICIAL_API，跨租户只做共享基础设施容量审计，不执行其它BC广告变更。
- 保留原规则阈值、版本、5M PAUSE及1H恢复，四开关true和现有TikTok平台调用策略；不清队列、不伪造水位。
- 2分钟是规划周期。当前素材端点3次/秒，623账户即使每户仅1页，理论下限208秒，不能承诺2分钟全覆盖。
- 数据库在途窗口不等于Worker硬预留；共享8槽仍有规则执行。发布后按实际单轮耗时/P95和待办斜率验收。
- 用户已授权本次事故紧急发布，过慢PG全备/隔离恢复豁免继续准确留证；保留#28已验证全备及本批快速快照。

## Review Focus

- 关联任意日期不能重新占实时事实或历史唯一窗口；事实窗口必须受同一PG锁约束。
- retry due_at只决定是否可领取，不能让老事实每次退避重排到新任务后。
- 完成补位不得重新筛选账户或调用平台；本地领取/写outbox失败不得改变已提交结果。
- 缺时区、跨日、其它tenant及未owned账户不能因新helper进入热刷。
- 分页部分结果不计完整成功；真实0花费展示行须保留并重新读取。

### Task 1: 修复零花费事实覆盖

**Files:** planner.py、health.py及各自真实PG测试；job_tasks.py调用名由Task2统一修改。

- [x] 写入广告零花费/素材零花费事实、跨日/时区/租户/ownership及health提前发现失败回归。
- [x] 真PG确认失败；实现known_reporting_accounts，当前当地日有广告或素材实际事实就进入热刷。
- [x] health复用该范围，禁止以筛查0替代完整报表；对应测试与静态检查通过。

### Task 2: 事实与关联容量及补位

**Files:** jobs.py、lanes.py、job_tasks.py及报表调度/并发回归。

- [x] 真PG回归先证明旧links占满live、旧links占cold、退避重排和完成需要额外Control hop。
- [x] 独立facts8 / links1 / cold1；原窗口锁、租户游标、冻结路由及claim令牌保持。
- [x] 事实可领取后按稳定请求时刻排序；完成直接本地_dispatch_pending，不在report worker里扫描broker或flush所有租户。
- [x] 对重复/旧claim、失败、回滚、并发领取及冷预算运行回归；修改Task1 helper调用名。

### Task 2b: 按真实生产样本补齐事实类型公平

8bd35045上线后的连续样本发现：大BC在74秒内广告鲜90→420，而素材仅153→161，素材P95反升。原tenant公平不能防tenant内单一created FIFO造成广告/素材成批轮流等待；因此本次事故修复还未达到持续覆盖验收。

- [x] 保留同一事实8窗口、tenant持久轮转、kind内created FIFO及due仅准入；以每tenant+kind有效RUNNING数量加候选顺序作为虚拟负载，批量和limit1补位都交错事实类型，不新增表/迁移/配额。
- [x] 真实PG先红后绿覆盖批量、单槽补位、单类型填满、多tenant及旧claim；327 reporting通过，复查共享窗口锁。
- [ ] 精确候选发布后重新按完整自然周期验收，不能把kind交错测试代替实测5分钟覆盖。

### Task 3: 集成、发布及实际服务能力验收

- [x] 真PG/Redis领域组合、Ruff/app ty、部署资产及K8s校验通过；独立审查。
- [x] 记录准确候选8bd35045、公司main构建、原发布锁/排空/快速快照及PG豁免，保留四开关与平台额度；Control2持久参数随版，11角色独立验收通过。
- [ ] 生产记录623账户的created/completed每分钟、facts/links/cold在途、oldest/P50/P95、完整事实新鲜度、PG/RSS。
- [ ] 用同BC、账户、日期、币种官方SDK重新对账用户2800；筛选未确认前不能以不同日期解释或否定用户金额。
- [ ] 至少两轮自然事实与自然5M规则验收；报告残留缺口，不能用Ready或一次0/0宣称完全修复。
