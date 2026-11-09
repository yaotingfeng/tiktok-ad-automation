# Business Prefix Progress Implementation Plan

> **For agentic workers:** 在本会话原生按步骤执行；root 已指定禁止派生 agent、提交和生产变更，覆盖技能默认的额外确认与提交步骤。

**Goal:** 让经营调度快速安排候选，所有大快照和前缀复制由可恢复业务工作阶段处理。

**Architecture:** Control 创建轻 SNAPSHOT 候选；Reporting 单重型事务冻结输入，随后用小元数据和完整PK分页比较/复制，最终沿原发布围栏切换。冻结输入与正式发布保持原合同。

**Tech Stack:** Python、SQLModel/SQLAlchemy、PostgreSQL、Redis、Celery。

**Spec:** ../specs/2026-11-09-business-prefix-progress.md

## Global Constraints

- 无迁移/新队列/业务开关；最多四个指定源文件，不动 S/U。
- 不提交、不推送、不操作生产、不派生 agent；root 负责独立审查和消费者优先发布。
- 真实 PG/Redis 回归；所有平台调用为本地替身。

## Review Focus

- 大 JSON 不得通过 deferred ORM 意外重新加载，测量33MB样本SQL与RSS。
- 候选无变化不得发布空事实；源发布改变/actor变化不得继续复制。
- 每批崩溃与重放不得丢失事实或超前推进游标。
- COPY 两表均完成才进入计算和发布，原 BASE/SUBSIDY 保持可用。
- 定时调度不能反复 supersede 未完成候选，预算退出不得误标业务失败。

### Task 1: 轻调度与单重型快照

Files: projection.py、tasks.py、test_prefix_progress.py。

- [x] 写 scheduled 不调用 source_snapshot/不读大inputs、未变化候选保留 publication 的回归并取得旧红。
- [x] 为 request_rebuild 增加内部 defer_snapshot/coalesce_pending 参数（API默认保持），实现 SNAPSHOT 轻候选及单重型锁/预算。
- [x] 检验 actor/配置围栏、同候选合并、已存在BASE/SUBSIDY兼容。

### Task 2: 有界前缀准备与复制

Files: 新 prefix.py、projection.py、test_prefix_progress.py。

- [x] 用真实33MB输入测量候选SQL形态；选择不会重复解压整JSON的比较方法。
- [x] 实现窄加载、dirty判定、COPY_DAY/COPY_SUMMARY完整PK分页及原子进度。
- [x] 真实PG覆盖批间回滚/重放、源publication更换、两表完整才发布和dirty语义等价。

### Task 3: 针对性收尾

Files: tasks.py、上述测试与本计划。

- [x] 验证预算/锁竞争回滚但保留未完成候选，SNAPSHOT单重型并发及RSS/耗时边界。
- [x] 运行业务模块相关回归、Ruff、format、严格mypy与diff检查；只针对新失败扩大测试。
- [x] 更新证据与交接 root 审查；不提交、不执行部署。

验证入口使用独立本地 PG18 数据库和 Redis db11；精确命令及红绿结果随实现记录。源快照仍超预算或内存不可控时立即报告边界，不能以延长任务期限掩盖。

## 实施证据

原实现轻调度/未变候选/合并3项真实PG失败；实现后业务模块80项通过（51.31秒），独立审查发现的双管理员和长pending补源轮转均有真实PG回归。后续补源日期上界与取消保护定向结果在最终交接更新。

34,558,027字节输入、184,642账户日、4,859新增账户的真实PG样本：SNAPSHOT 2.641秒，进程峰RSS 664,387,584字节；精确dirty 0.899秒；128账号补源窗口0.133秒。它们是本地PG18/Python测试结果，不是生产性能承诺。Reporting仍保持12并发、原4CPU/6Gi限制；新SNAPSHOT全局事务锁限制单重型，root上线前另取当前RSS。

生产代码只有projection.py、tasks.py、新prefix.py；没有修改source.py。未做迁移、生产写入、提交或推送。消费者优先与新phase后禁止自动旧版本回退由root发布门禁负责。

最终补源日期上界+大JSON复测2 PASS（10.03秒），补源SQL取消不撤销候选1 PASS（4.14秒）。三源严格mypy通过；六个源/测试文件Ruff、format与git diff --check通过。最终src仍三文件，source.py未改。私有原始证据保留于.runtime/sync-production-20261031/business-prefix1109t-*.log。
