# 素材目标消息补派：精确键候选（本地完成，尚未发布）

## 问题与工程选择

W 上线窗口的 `materials.repair_dispatches` 出现 target 3 次、source 5 次 `QueryCanceled`。它们各自触发原有 10 秒 SQL 上限；保存点保证失败类别不撤销其他类别，但这一轮对应类别无法补派丢失消息。

只读生产探针确认 outbox 约 2523 万行。原 target 已命中 `(tenant_id, payload.operation_id, task_name)` 索引，并非缺少索引或全表扫描；30 个活跃目标的代数、领取、到期条件仍须回表过滤。同一目标样本逐个丢弃 950–1097 条历史消息，冷页探测约 217–316 毫秒。身份库存仅约 34 毫秒，慢在随后反复检查消息历史。

工程选择只优化 target：给原候选查询增加 `_target_dispatch` 已有七类键的限制，使用现有 `(tenant_id, task_key)` 唯一索引。原 tenant、operation、distribution、revision/claim、due、seed、source-owned、未替代及核查终态条件全部保留；最终加锁、到期 EPQ 复验和 published_at 置空逻辑不变。不扩大额度、超时、批量，不变更表结构、任务身份或新增持久字段。

| 已有消息类别 | 键构成（均保留原 payload 资格复验） |
| --- | --- |
| 普通准备 | `material-target:{distribution}:{operation}:{revision}:prepare` |
| 普通核实 | `material-target:{distribution}:{operation}:{revision}:verify` |
| 只读核实 | 上一项加 `:read:{operation}` |
| 领取恢复 | `material-target-recover:{distribution}:{claim}` |
| 只读领取恢复 | 上一项加 `:read:{operation}` |
| 来源观察 | `material-target-observe:{distribution}` |
| 只读观察 | 上一项加 `:read:{operation}:{revision}` |

生产仅执行等价候选 SELECT 的只读 EXPLAIN ANALYZE：30 个活跃目标走 `uq_dispatch_tenant_key`，总约 111 毫秒，每个身份约过滤 1 行。原查询在 3 秒探针预算内取消。六个固定身份同一 RR 快照的原/候选结果均为空且相等；非空和边界语义由下面真实 PG 回归验证。该证据不是产品代码已经上线。

## 本地真实回归

独立 PostgreSQL 18 数据库 `sync_material_exact_test_1109y`，Redis db13；不调用真实 SDK。新增 `test_material_dispatch_exact_keys.py`：

- 七类真实生产者消息都可恢复，ID、task_key、payload、available_at 与父状态保持；来源观察专门使用已成功但仍由来源拥有的操作，验证独立 observe 分支。
- future due、120 秒 grace、外租户、错误分发/操作、旧代数、已换 owner、未过期 claim、错误 claim、非来源拥有观察消息均不恢复。
- 当前身份有 4000 条旧代消息时，实际 EXPLAIN 扫描记录数必须小于 32；不依赖索引名称或机器耗时。

使用完整精确 Y `18b19574826f6f92e5fdd21e5d6e19e942dbe800` 的 Git 归档建立独立私有副本，只叠加本次 distribution.py 差异与新增测试，未带入工作区其他未发布代码。归档逐文件核对：旧文件仅 distribution.py 发生授权差异。

精确 Y 旧代码：**1 FAIL + 17 PASS / 4.24 秒**，唯一失败实际读取 4002 行。新测试在修复后全部通过（工作区单文件 **18 PASS / 4.55 秒**，精确 Y 也全部包含于下述通过结果）。

精确 Y + target 的相关九文件回归：**81 PASS + 1 fixture ERROR / 35.86 秒**。唯一错误是 `test_dispatch_repair_history.py::test_repair_only_rearms_current_authorized_generation`，在 `isolated_strategy_database` 创建策略时即报“请先更新可用投放身份”，尚未执行补派。完整精确 Y 原样归档、未叠任何修改时，同项仍 **1 ERROR / 3.92 秒**。没有修改无关策略 fixture，也不将此次结果称为全绿。其余回归包含真实并发到期 EPQ、seed 等待、只读核查、来源待投递/claim/终态边界。

Ruff、格式及 diff 检查通过。精确 Y backend 中运行 `mypy app/modules/materials/distribution.py` 并以 `--shadow-file` 对比修复前源码，两个输出逐字相同：**35 文件共 256 个既有错误，新增 0**，没有将基线类型错误称为通过。distribution.py 自身仍是两个既有问题（`live_message` 缺返回注解、`seed_dependency_settled` 未类型化调用）；未扩大范围清理旧类型问题。

## 明确未解决

source 分支仍可能扫描同一操作数千至两万代历史；本次完全不改该分支或其超时。原/反向扫描的固定 RR 只读实验中，三个慢来源身份原向均 3 秒取消；反向确实使用 Index Scan Backward 且没有 Sort，但其中两个仍 3 秒取消。另一个返回 0 仍过滤全部 5438 行，并受此前扫描预热影响。没有可恢复的当前消息时反向也要证伪历史，不能作为可靠修复，更不能按时间猜测跳过旧消息。来源问题留待独立设计。

证据仅在私有 `.runtime/sync-production-20261031/material-repair1109y/` 与同前缀探针日志中保留；原始业务 UUID、查询参数不进入本文。此次工作未提交、推送、部署或写入生产。Y 的 X/X2 发布由根线程独立处理，不包含此未提交候选。
