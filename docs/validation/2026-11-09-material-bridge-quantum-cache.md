# 素材补读量子内桥索引复用：本地验证

状态：本地实现和针对性验证完成；本文不代表 X 已提交或上线。生产观察来自 W `60302e3f`，与另行进行的 CPU 配额试验分开记录。

## 观察与范围

W 上线后 5 分钟 BG 日志有 156 次目录任务开始、157 次结束（156 SUCCESS、1 其他失败），未出现 `server closed` 或 claim 提交断连栈。旧 8 例大素材任务均在 W 上线前完成，不能将其完成归因于 W。

W 实际 active 样本中 8 个目录任务均为素材子任务，7 个含 36,000–82,140 条冻结素材。与日志精确关联后，它们全部 SUCCESS，大样本总时长约 25.2–37.0 秒，首个 HTTP 在约 4.6–8.7 秒；其中非 HTTP 时间包含数据库、上下文构造和准入，不能全部视为 CPU。另一个只读样本含 120,080 条素材，已在 W 期间推进到第 49 页。

旧实现每页先完整检查素材 owner，再完整建立桥索引；5 页量子重复 10 次全量遍历。此次只把该索引准备移至首次 claim 提交并退出数据库 session 后，量子内复用一次不可变索引。下个量子重新构建；普通直接调用仍自行准备索引。

## 保持的边界

- V 的全部冻结上下文 streaming 验证、DTO 构造仍完整执行，没有裁掉尾部或只验证当前页。
- 缓存要求源 ads/materials 元组对象身份完全相同，owner 集合相同；每页仍验证 tenant、account、owner 父身份、query 范围。
- 当前 owner 新鲜度、冻结 fingerprint、gen/token/head/route 及每次 HTTP 的 TransactionBoundaryReader 围栏保持原路径。
- 精确桥多义性和原生 material ID 冲突仍拒绝；素材状态与创意广告状态分层保留，未改变合并语义。
- 无跨任务、跨量子或全局缓存；无数据库迁移、生产数据写入、限时、资源、额度或开关调整。

## 本地真实 PostgreSQL / Redis

专用 PostgreSQL 18 数据库及 Redis DB 14，外部服务只在 transport 边界替身，无真实外部请求。

旧代码：新增两条行为用例 **2 FAILED**，5 页实际遍历 10 次；7 页分两量子实际 `[10, 4]`。新代码相同断言 **2 PASSED**，分别为 1 和 `[1, 1]`。

最终四文件针对性回归 **74 PASSED / 49.22 秒**：

- `test_material_quantum_bridges.py`：独立连接确认 claim 已提交、session 已退出；缓存每量子新建；相等但不同身份元组、owner、tenant、account、尾部外来桥拒绝；双通道原生 ID 冲突、多义桥拒绝与状态保留；内外两层 mapping 不可变。
- `test_material_quantum_context.py`：原 owner/frozen input 变化、新量子重载、过期上下文拒绝。
- `test_directory_quantum.py`：逐页资格与运行围栏。
- `test_material_directory_children.py`：原完整素材范围、身份、暂存和发布回归。

## 独立 CPU 对照与局限

合成数据、每组独立 Python 进程；构造 DTO 不计入下面的索引 CPU，使用 `process_time()`，没有 DB/Redis/网络。旧路径执行 5 次全量 owner 检查和建桥，新路径一次准备及 5 次源身份/owner 检查。

| 素材数 | 旧索引 CPU 秒 | 新索引 CPU 秒 | 旧/新峰 RSS MiB |
| --- | ---: | ---: | ---: |
| 36,000 | 0.124709 | 0.042281 | 75.9 / 78.9 |
| 82,140 | 0.338798 | 0.082700 | 111.8 / 118.1 |
| 120,080 | 0.577206 | 0.112375 | 144.9 / 155.2 |

此对照证明重复遍历减少，不能预测生产总耗时或吞吐。初始化仍 O(N) DTO/索引；不可变包装及跨页保留索引增加内存，12 万条样本约多 10 MiB 进程峰值。10 秒 idle / 45 秒事务预算原样保留；没有宣称任意规模都能在预算内完成。私有脚本和原始日志保存在 `.runtime/sync-production-20261031/material-bridges1109x-*` 及 `benchmark-material-bridges1109x.py`。

独立源审补强：`owners/entries` 改为 `init=False`，只能在构造时从源元组完整推导；禁止通过构造或 `dataclasses.replace` 注入派生索引，替换源素材必须重新验证。补充伪造空索引隐藏歧义及外来尾部桥的构造拒绝回归。Ruff、format、diff 检查通过。严格 mypy 在旧 `ads_read.py` 存在 33 项错误，本次为 32 项；按错误文本去除行号逐项比较，无新增错误（原未参数化 tuple 错误随索引类型细化消失），不声明全量类型检查通过。

最终合同补强后：新文件 **16 PASSED / 14.51 秒**；同时原 context 文件 11 项均通过（上一轮合计 25 PASS / 2 个异常类型断言失败，Python 3.14 的 `replace(init=False字段)` 实际抛 `TypeError`，已纠正断言并重跑全部 16 项）。没有把这两个测试断言错误归为产品故障。
