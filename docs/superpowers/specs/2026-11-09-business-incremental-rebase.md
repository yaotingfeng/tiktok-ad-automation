# 经营候选按真实来源增量追赶

Root 工程选型已批准；用户已授权自行处理，不再逐项请求批准。

两个大 BC 连续三轮在 SUBSIDY 结束后因来源签名变化退役，正式 Q4 停在 11/07。冻结输入精确比较证明只有 11/08 的八个账号日变化，旧季度前缀完全相同；不得忽略版本/观测时间变化来发布。

新增 VERIFY、REBASE 阶段，无迁移、新队列或开关。VERIFY 和 SNAPSHOT 共用重型单例、18 秒总预算，一次 work 仅一次重型尝试。真实当前快照与旧冻结输入比较：tenant/BC/actor/授权/scope revision、calculation/catalog/account 元数据（除 complete_through）及其它结构字段完全相同才允许保留候选；否则 SUPERSEDED。每个账号日任何状态字段变化及 complete_through 覆盖资格变化均计入最早 dirty。

同事务保存真实新 inputs/signature 与 REBASE 游标。仅删除候选 run/tenant/BC 下 dirty 及之后的 day/summary，每批至多 500 个 PK、普通 3 秒量子，清理和游标原子提交。清理完成后 BASE/SUBSIDY 重算整个尾部；再次 VERIFY 稳定才切正式指针。dirty 前已证明相同的行保持不变。rows_count 继续原累计 BASE 处理量，另 coverage 记录追赶量，不冒称净事实行数。

实际 33–35 MB / 185k 状态 fixture 测量旧 JSON 加当前 snapshot、差异比较和持久化整链的峰值 RSS 与时间；额外内存保守上限 2 GiB。回归须证明结果等价独立全量构建、重复变化不发布、结构变化退役及清理中断回滚。

发布由 root 负责：Reporting 消费者先升级、所有旧 Pod/fork 退出后才升级生产者。启动新消费者前设置 forward-only；出现新 phase 后不能自动回退旧代码。本任务不提交或操作生产。
