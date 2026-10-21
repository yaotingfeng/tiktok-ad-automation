# 2026-10-21 目录健康查询本地调查

结论：**未复现线上偶发 5 秒超时，根因未确定，未修改业务查询。** 没有证据支持新增索引、放宽超时或改变缺口判断。不得将以下本地结果表述为线上问题已修复。

## 范围与数据来源

- 线上现象来自用户说明及已保存在本机的只读材料；本轮未连接线上数据库、未调用 TikTok。
- 原始目录规模取自 `.runtime/sync-reduction-probes/out2.json` 的 `dir_objects`，SHA-256 为 `6afc7ca6b04d5f9e77b864268b1ca2655c27809d9c042bad78f4baa4cbfa2959`。该快照是历史规模参考，不冒充故障瞬间快照。
- `.runtime/sync-reduction-release/claude-eval3.jsonl` 保存了主 BC 01:18 的 `MONITOR_QUERY_FAILED`。这些材料没有故障时的 EXPLAIN、阻塞 PID、逐查询等待事件与数据库资源指标，不能据此区分计划、锁等待或资源争用。
- 特别排除无关证据：`preflight-output.log` 的超时 SQL 是 `ad_material_reference` 计数，不是本次目录三重连接查询，不能拿它证明本问题根因。

## 本地真实 PostgreSQL 实验

使用现有本机 Docker PostgreSQL **18.6 / aarch64**，专用测试库 `sync_health_test`，独立 `health_profile` schema。用 `LIKE public.<table> INCLUDING ALL` 复制现有目录/事实列定义及已有索引；事实表按月分区，建 2026-09/10/11 三个月的分区。没有新增候选索引或修改应用表结构，不涉及迁移。

| 合成数据 | 行数 |
| --- | ---: |
| 账户身份（目录/事实中的 advertiser_id） | 870 |
| 今日有消耗广告事实 | 6,000 |
| 前 32 日历史事实 | 192,000 |
| ad_object / ad | 173,992 |
| ad_object / adgroup | 78,234 |
| ad_object / campaign | 70,659 |
| ad_object / creative | 5,268,737 |
| ad_object 合计 | 5,591,622 |

所有数据都是匿名合成数据；没有复制生产业务行。目录三个父子层均覆盖今日事实。测试时刻固定为 UTC 2026-10-21 01:18；账户事实时区为 Etc/GMT。目录 configuration 为小型合成 JSON，不能模拟生产行宽、膨胀、磁盘缓存和 CPU/IO 竞争。未额外建立完整账户业务表；本实验单独执行缺口 SQL，其账户范围由 870 项数组传入。

服务器参数：shared_buffers=128 MiB，work_mem=4 MiB，effective_cache_size=4 GiB，random_page_cost=4，max_parallel_workers_per_gather=2。每次测量保持 `statement_timeout='5s'`；没有为取得计划延长超时。建数事务不使用此查询超时，测量事务才设置 5 秒，与应用查询一致。

脚本从当前 `health.py` AST 提取缺口 SQL，避免手抄查询漂移。以显式服务器 PREPARE/EXECUTE 分别测试三种 plan_cache_mode，每种连续七次；每次保存 `EXPLAIN (FORMAT JSON)` 和 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。业务代码仍保留原有 `force_custom_plan`，其他模式仅为实验对照。

| 实验 | 次数 | Execution Time 范围（ms） |
| --- | ---: | ---: |
| force_custom_plan | 7 | 72.668–151.374 |
| auto（预备语句复用） | 7 | 68.861–88.084 |
| force_generic_plan 对照 | 7 | 68.899–75.173 |
| 重新 ANALYZE 后的定制计划 | 3 | 78.956–102.237 |
| 持有 6,000 条广告 UPDATE 未提交事务时，4 路并发查询 | 4 | 93.862–101.580 |

共 28 次执行，全部完成、无超时、缺口结果均为 0。首轮不是人为清空 OS 缓存的冷启动：只能确认 EXPLAIN 实际有 shared reads，不能称为严格冷缓存测试。

## 执行计划证据及判断

首轮定制计划摘要：

```text
Nested Loop Left Join（最终缺口过滤，返回 0）
  Nested Loop Left Join（6000 行）
    Nested Loop Left Join（6000 行）
      Bitmap Heap Scan ad_report_day_2026_10
        Bitmap Index Scan (tenant_id, stat_date, advertiser_id)
      Index Scan ad_object_pkey，loops=6000
    Index Scan ad_object_pkey，loops=6000
  Index Scan ad_object_pkey，loops=6000
Execution Time: 151.374 ms
Shared Hit Blocks: 85113; Shared Read Blocks: 5137
```

- 三层目录连接均用已有 `(tenant_id, advertiser_id, kind, remote_id)` 主键精确探测，每次实际 1 行，没有扫描 526 万 creative，也没有对目录作重复全表扫描。**本地结果不支持“缺少目录索引”的修复方向**，但没有线上 schema/故障计划，不能据此保证线上不存在差异。
- 定制计划以日期索引裁剪到当月分区；粗日期范围读出今日及前一天 12,000 个索引项，精确当地日过滤后 6,000 行。当地日表达式导致估算约 61 行、实际 6,000 行，存在估算偏差，但本次仍选中有效主键探测，未造成超时。不能仅凭估算偏差改查询。
- 通用计划将事实侧改为分区 Append + 主键扫描，目录侧仍为三次主键探测；首轮约 69.810 ms，shared hits=104901、shared reads=0。三次独立 ANALYZE 也没有出现导致超时的计划变化。**未复现破坏性能的计划切换**。
- 并发测试保持写事务不提交再执行四路读，全部约 94–102 ms 完成；MVCC 下普通行更新没有阻塞该 SELECT。**未复现正常行更新造成的锁超时**。没有模拟 DDL 的 ACCESS EXCLUSIVE 锁，亦没有故障现场锁采样，不能排除线上不同类型的阻塞。

## 保留证据与可复跑入口

原始脚本、日志及所有 JSON 计划保留在忽略目录 `.runtime/sync-health-fixes/`，不提交本地环境配置或数据库。脚本只接受 loopback 上名称含 test 的 PostgreSQL 库；复用本轮本地环境时，从仓库根运行：

```bash
python3 .runtime/sync-health-fixes/run.py uv run python ../.runtime/sync-health-fixes/profile.py
```

现有 schema 被复用；若需重新建数，应另外创建专用本地测试库并执行当前迁移，不应对业务库运行本脚本。首次建数因绑定参数 text/varchar 推断冲突而整笔回滚，修正脚本显式 text cast 后重跑成功；失败轮不计为实验结果。

关键证据 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| profile.py | `2ca52191375502575b92fa5703120d9876847c673d1dbfe696ba18f1adb3f71a` |
| profile.log | `6d32d846ca2bd80a30d44c380e4168103e4af3312f54d223b36df55cd04700ee` |
| plan-force_custom_plan-0.json | `949149e914137cd03b3ad13aef03146bc0ada1b89525af85bfb3b422a874f83b` |
| plan-force_generic_plan-0.json | `4b9f300c4d5cc2eb1370e5635737e0cfd10d1541c89a8906c42f026750795953` |

## 未解决项与门禁

本次第三阶段按用户“无法复现时如实写明，不猜测性修改”的要求，提交调查记录，**没有第三项业务修复**。下一步确定线上根因需要故障当时的执行计划、pg_stat_activity/pg_blocking_pids、查询等待事件、CPU/IO 与统计信息时间；本轮没有为取证越过“不连接线上”的限制。

第三阶段仅增加文档，应用代码与 `db997831` 完全相同，沿用该提交刚完成的真实 PostgreSQL/Redis 三模块全量 **1064 passed**、Ruff/ty/改动文件格式通过、mypy 499 条且无新增；不将规模实验算作 pytest 项数。查询超时、业务结果、索引和迁移均未改变。
