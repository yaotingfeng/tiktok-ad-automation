# 经营来源刷新后的同候选尾部追赶验证

本记录是本地候选验证，尚未提交、推送或发布。Root 已选定方案；没有新增业务开关、队列或迁移，也没有执行生产数据写入、制造任务或调用 SDK。

## 问题与行为

只读线上核验发现两个大 BC 连续三轮在 SUBSIDY 末尾因来源变化退役，Q4 正式版本仍止于 11/07。数据库内精确比较相邻冻结输入：只有 11/08 各八个账号日变化；版本、updated、observed 均变化，其中一组另有两条 row_count 变化。此前日期、结构配置及账号元数据不变。不能省略版本检查来发布，但反复复制约六十万行未变前缀会拖延正常追赶。

新增 VERIFY 与 REBASE。VERIFY 与 SNAPSHOT 共用 PostgreSQL 重型单例、18 秒总预算，单次 business.work 只执行一次重型单元。结构、actor、授权、修订、计算口径、catalog、账号元数据一致时，逐账号日精确比较所有冻结状态字段，并计入 complete_through 覆盖资格变化。存在真实变化则在同候选内保存新快照、签名及清理状态，按最早 dirty 日重算；结构不一致仍退役。

REBASE 仅处理候选的 run/tenant/BC，按完整主键每批至多 500 行依次清理 day、summary，沿普通 3 秒量子提交，再真实执行 BASE/SUBSIDY。dirty 前原样保留，dirty 后已消失的系列和合成零消费行不会残留。完整重算并再次核验稳定前，正式指针与旧事实保持不变。rows_count 保留原累计 BASE 处理量口径；coverage._rebase 记录轮次、清理阶段和删除量，均不宣称净事实新增数。

## 验证

使用本机 PostgreSQL 18.6 的独立临时数据库 `sync_business_rebase_test_1109k3` 和 Redis 11，无外部 API。私有 runner 每次新建并删除数据库。

```sh
.venv/bin/python .runtime/sync-production-20261031/run-test-business-rebase1109k3.py tests/modules/business/test_incremental_rebase.py -q
.venv/bin/python .runtime/sync-production-20261031/run-test-business-rebase1109k3.py tests/modules/business -q -s
.venv/bin/python .runtime/sync-production-20261031/run-test-business-rebase1109k3.py tests/modules/business/test_incremental_rebase.py::test_cleanup_never_exceeds_500_even_with_larger_base_batch -q
```

- 旧实现：两个核心测试因候选直接 SUPERSEDED 而失败，2 RED / 3.93 秒。
- 新实现：核心扩展 15 PASS / 14.40 秒；随后补充 summary 清理、VERIFY 原子回滚、work 重型单次预算。
- 全部 business 相关测试：101 PASS / 144.49 秒，包含上述 18 项新核心回归、性能测试及原 API、导出、并发等测试。
- 最后新增的 1,001 行边界测试：1 PASS / 2.92 秒；即使调用方 BASE_BATCH=1000，一批也仅删除 500 行。无需因这一纯新增边界测试重复已通过全套。
- 三个源文件严格 mypy 无问题；五个改动 Python 文件 Ruff、format 检查及源 diff whitespace 检查通过。

核心行为验证包括：候选结果与独立新快照全量 BASE 构建的 day/summary 逐行相同；未完成前正式指针不变；单纯 version、updated、observed、count、status 变化均触发精确 dirty；第二次变化仍不发布；结构/catalog/actor/revision/账号元数据变化退役；complete_through 资格变化；清理两阶段与真实新 snapshot 保存中断时行和进度共同回滚；VERIFY 不与另一个重型快照并发。

## 大输入性能

由 SQL 创建 184,642 个真实账户日报状态，冻结 JSON 为 **34,558,027 字节**。profile 在独立物理事务重新读取旧 JSON，执行真实 source_snapshot、精确比较、持久化新 inputs/signature/REBASE 并真正提交，不只是计时纯 Python helper。

| 路径 | 整链时间 | 旧 JSON 读取 | 当前快照 | 精确差异 |
| --- | ---: | ---: | ---: | ---: |
| 单独变更 profile | 5.925 秒 | 0.539 秒 | 2.518 秒 | 0.453 秒 |
| 全套中变更 profile | 8.141 秒 | 0.558 秒 | 4.106 秒 | 0.383 秒 |
| 全套中稳定最终发布 | 3.456 秒 | 0.354 秒 | 2.112 秒 | 签名相同，无需差异遍历 |

全套进程峰值 RSS **835,534,848 字节（约 797 MiB）**，这是整个测试进程峰值，不能误写为生产额外内存。低于 18 秒及保守 2 GiB 上限。初次独立 profile 显式提交 fixture 外层事务后出现一条已知 teardown 重复 rollback 警告；后续仅为该性能测试精准过滤该警告，完整测试无其它 warning。真实结果等价由独立核心测试验证；性能 fixture 专注大输入链，不冒充完整线上报表验收。

私有原始证据保存在 `.runtime/sync-production-20261031/business-followup1109k3/`，主要文件为 `rebase-red.log`、`rebase-green1.log`、`rebase-green2.log`、`rebase-profile.log`、`business-suite.log`、`rebase-delete-bound.log`、`mypy-final.log`。原始生产 UUID 和查询输出未写入本文。

## 发布约束

Root 在启动新消费者前设置 forward-only。Reporting 消费者先升级并确认全部旧 Pod/fork 退出，再升级 API、Control、Beat 生产者；出现 VERIFY/REBASE 后禁止自动回退不了解新阶段的代码。旧 BASE/SUBSIDY 候选仍从持久进度继续进入 VERIFY。

上线验收必须看固定候选身份、REBASE 游标、真实 BASE/SUBSIDY 提交及正式指针/end_date 前进，不能只看 COPY 计数、Celery SUCCESS 或 next_check。观察到持续来源变化时允许继续追赶，不能伪造水位或跳过最终核验。本地性能不替代上线时 Reporting 当前 RSS 和真实 VERIFY 预算检查。
