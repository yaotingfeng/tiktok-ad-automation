# 素材补链续步复用本地验证

当前状态：本地实现与验证，尚未提交或上线；生产只读观察与本地测试分开。

## 根因证据

主执行者的完整只读审计发现 848 个起止日期及完整查询身份相同的后继承接关系。本轮复用其 canonical 预检候选，均匀抽取 32 对、去重 48 父任务进行标量查询。2026-11-09 02:36:22 UTC，Y `18b19574`，REPEATABLE READ / READ ONLY、单 SQL 3 秒、事务最多 55 秒，实际总 0.059 秒：

- 48 个均无 ReportSyncPage、无 snapshot_observed_at。
- 18 个是无父/head 有效租约的 WAITING/error=None 已续步任务。
- 样本包含真实 next_page 3/6、locate_post/locate_queries，以及扫描与身份检查续点。

这不是总体比例估计。私有原始证据：`probe-link-continuation1109z.py/.log`。没有调用 SDK、创建任务或修改生产数据库。

源码确定的生产入口是素材日报成功发布后的缺口检查与 enqueue；每次新请求使用当前时间，提升 desired revision。`planner.MATERIAL_INTERVAL` 是 15 分钟；10 分钟是依赖退避基数，不能据此声称每 10 分钟生成父任务。健康修复另有入口，但当前日相同范围待领父已有 covered 过滤。样本不能反推每个历史父的确切调用来源。

## 本地回归

独立 PostgreSQL 18 / Redis DB 14；无真实外部请求。三个最初用例在旧实现 **3 FAILED**：概览、定位、身份续步的新需求均创建第二父。新实现首轮三个用例及原 dependency-only 回归 **8 PASSED**。

嵌套校验补强后新文件 **31 PASSED / 3.20 秒**，包括：

- 连续两次新需求仍一个父、原游标/到期时间不变，新引用推进但 satisfied 不变。
- 两次真实 material_day 领取、发布、enqueue 后同一个 material_links 父保留第 6 页。
- 快照、私有页、RUNNING、领取字段、有效 head、旧 owner、未知或非法进度、其他 kind、路由/日期/完整账户/campaign 范围变化拒绝。
- locate 空对象/负索引/零页、checked 内对象、错误 entries/pending、错误内容证据拒绝；合法完整与部分分页证据保持。
- 新事实到达旧 overview_complete/checked 后，实际执行先 WAIT，新需求未满足、原扫描时间不变、变化签名不再作为负缓存；真实正向关系补齐后再次执行才 COMPLETE，新版本满足且 observed_at 不早于新需求。

单文件严格 mypy 通过。最终相关回归结果在下方追加；不把历史生产父自动取消或压缩作为本次效果。

最终七文件针对性真实 PostgreSQL/Redis 回归 **112 PASSED / 10.06 秒**：新 continuation、原 dependency-only reuse、requirements、material_links、spark_content_identity、link_query_reuse、link_evidence_age。覆盖目录/身份变化后重新检查、完整集合缓存、原观察起点和需求满足边界。Ruff、format、diff 检查通过。

集合构造性能补强：候选广告集合和成员缓存目标集合分别预计算一次，多个栈节点/缓存条目只做子集检查。真实 PostgreSQL 中存入合法合成进度（2,000 候选、5,000 目标、16 栈节点、128 完整缓存及一个部分缓存）：旧版实际构造候选/目标集合 **17 / 129 次**，新断言旧红；新版各 **1 次**。同一次 enqueue 连同锁定、读取、完整进度校验实测 **0.0306 秒**，原样保留游标；3 秒用例边界通过。旧计时 0.0472 秒只是本地单次对照，不外推生产吞吐。最终相关七文件 **113 PASSED / 11.92 秒**；mypy、Ruff、format、diff 通过。
