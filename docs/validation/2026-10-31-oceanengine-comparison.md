# 2026-10-31：巨量参考项目与 TK-ADA 同步/查询对照

## 范围与结论

按用户指定，只读审阅 `/Users/yaotingfeng/Documents/ytf/ytf-ad-skill/ad_automation`，参考提交 `8b2e9ee1b`；TK-ADA 本地基线 `12d24fa3`，最近核验生产 `0642ad3d`。本轮没有修改两套应用、运行迁移、访问巨量生产或调用广告平台写接口。TK-ADA 生产仅做限时只读元数据和非执行 EXPLAIN。完整目标和实施依赖见[总体方案](../superpowers/specs/2026-10-31-sync-query-redesign.md)、[实施计划](../superpowers/plans/2026-10-31-sync-query-redesign.md)。

结论不是“巨量所有代码都优于 TikTok”，而是三类差异：

1. **来源能力不同**：参考系统当前用 SPI 通知 + 连山 RDS `(update_time,id)` 增量读取；TK-ADA 主要用 TikTok 分页接口。没有证据表明 TikTok 提供同等官方 RDS，不能承诺复制后得到同样吞吐。
2. **可以学习的设计**：持久高水位、热同步/晚到修正/历史修复分层、数据库筛选聚合、先取页面身份再补宽详情、避免重复通知击穿最低间隔。
3. **TK-ADA 自身的成本必须整改**：在线首屏承担全结果成员冻结；素材页仍有跨全日期范围映射；读平台期间持业务事务；大量等待任务进入执行后才争额度；调度与历史流水混在大工作集中。

版本补注：文档收尾时，[另一发布流程最终回执](2026-10-31-bulk-production-release.md)记录北京时间02:06完成12d24fa3/0069及BASIC配置上线。本页生产查询计划与吞吐属于此前0642ad3d，不作为新版本性能结果；后续实施重新建立当前基线。

“巨量系统很快”是用户实际体验，作为调查线索成立；两系统当前硬件、数据分布、请求负载没有做受控对照，不能给跨系统加速倍数。

## 逐项对照

用户补充要求参考官方存储设计后，追加[连山 RDS 源表结构审阅](2026-10-31-lianshan-rds-schema-review.md)：逐项核对参考仓库保存的2026-07-18源MySQL字段、主键、索引、时间分表及游标，不将下列应用本地PG实现等同于官方源表。

下列 `OE:` 文件路径相对参考项目；`TK:` 相对本仓库，行号以以上提交为准。

| 主题 | 参考项目证据 | TK-ADA 现状与采用决定 |
| --- | --- | --- |
| 数据来源 | OE `backend/app/main.py:2343`；`services/oe_spi_processor.py:237`；`api/routes.py:2536`：通知唤醒，RDS 读事实 | 学统一入库和唤醒边界；不假设 TikTok 支持远端 update_time 游标 |
| 源游标 | OE `integrations/lianshan_rds.py:50`：复合时间/ID排序；`services/rds_report_sync.py:302`：提交批次后推进 | 保留 TikTok 分页/快照证据；本地抓取时间不能伪装远端变更水位 |
| 修正窗口 | OE `docs/production-ops/current-production-environment.md:161`：7天热回扫、14天每日对账、300秒重叠；`rds_report_sync.py:336` 不回退高水位 | 学分层，不直接复制天数；TikTok 的归因窗口、晚到金额和当地跨日必须保留 |
| 唯一键/版本 | OE `db/report_repository.py:2289,2321,2384,2420,3012`：小时身份、op_ts/RDS版本防旧覆盖 | TK 已有日事实身份、覆盖和目录 observed_at 防旧覆盖，不重新发明；移除跨HTTP锁时要补围栏CAS |
| 事实/详情 | OE `services/report_data_ingestion.py:100`、`db/report_repository.py:2955`：缺详情定向补档；旧事实重放可重试依赖 | TK material_day 已发布后独立补链；继续完善四类覆盖展示，不把“事实未入库”与“图片尚不可展示”混用 |
| 事件和调度 | OE `jobs/rds_report_sync_daemon.py:23,48,66,82,126`：通知合并、最低间隔、每轮有界订阅、全量账户低频 | 学调度入口合并；其进程锁不能复制到多 Worker；TK 继续用持久租约/outbox |
| 批量读取/写入 | OE RDS 默认2000行/批；`report_data_ingestion.py:61,88`、`report_repository.py:2312` 仍逐条upsert | 学有界读取，不把它说成 COPY/多行批量写；TK 现有批量事实/目录写入应保留 |
| 普通报表查询 | OE `report_repository.py:3669,3737,3747`：PG聚合、稳定排序、LIMIT/OFFSET；仍有两次CTE汇总 | 学窄列查询，不复制深OFFSET；TK 已有序号cursor，也有日汇总与SQL聚合 |
| 素材快路径 | OE `report_repository.py:3913–3973,6177`：事实聚合后补本页名称，减少JSON/资产JOIN | TK `material_query.py:64` 每页全范围post_video_map应改成由本页身份反查事实，保持跨账户金额完整 |
| 广告管理列表 | OE `report_repository.py:4313,4439,4527,4533`：page_keys后加载详情 | TK 快照构建中全候选关联JSON和页面详情应拆开；指标筛选和排序仍要先于LIMIT |
| 查询计划驱动 | OE migration `pg_migrations.py:3302`；提交`b59725128`：实际JOIN前导列索引、局部JIT | 只复制验证方法；不复制全局jit设置或无证据添加索引 |
| 前端请求 | OE `frontend/src/main.tsx:14`、`ReportDataPage.tsx:149,174,203,225,245`：30秒缓存、signal、可见性控制 | TK `useAdsQuery.ts:18,30` 已有15秒缓存/取消；需检验后端SQL实际取消、多标签页重复构建和publication自动刷新 |

## 参考项目有哪些真实性能证据

| 证据 | 结果 | 适用范围 |
| --- | --- | --- |
| OE `docs/reports/production-business-weekly-2026-07-27-to-2026-08-02.md:14,110` | 当时4类RDS游标约落后1分钟，SPI 121396条成功，源端迟到修订尾差0.14元 | 有日期的历史运行证据，不是当前所有报表SLA |
| OE `b59725128` 提交 | 当天账户查询约2.4秒；960次LATERAL约36.7万buffer hits，JIT约占一半 | 特定生产计划分析，没有完整同负载改后耗时，不能推算倍数 |
| OE `a73360355` 提交 | 特定审计页面项目查询约8秒→20毫秒，总览10秒→3秒 | 集合JOIN及5分钟缓存；不能外推普通实时页面 |
| OE `docs/production-ops/data-read-memory-audit-20261020.md:44` | 5万条宽剧目合成数据，11.440秒→0.217秒，RSS 1020.7→70.9MiB | 本机隔离PG合成测试，不是生产全系统吞吐 |

同一内存审计记录也写明生产超过6GiB MemoryHigh后API无响应；因此应学习它修复无界取数的方法，而不是把参考实现当成没有问题的标准答案。

参考仓库 AGENTS/旧部署手册仍保留“鸿源报表”文字，已与当前 routes/main 及后续生产记录不符；7/18割接记录是待执行模板。本文以当前源码和有日期实证为准，不用模板证明上线。

## TK-ADA 新增生产只读证据

北京时间10/31 01:45，选账户数最大的既有同步scope（4859账户），固定结束日2026-10-30，`include_empty=false`、按spend降序；调用现有 `_ctes` 生成纯SELECT：

```sql
EXPLAIN (FORMAT JSON)
WITH ... -- 由 row_set._ctes 原样生成
SELECT row_key, refs, material_uses FROM ordered ORDER BY seq LIMIT 50;
```

每条在只读事务中执行，statement timeout 4秒、lock timeout 300毫秒。**没有 ANALYZE，没有执行真正报表扫描或INSERT快照**；这是成员选择部分，未覆盖完整API的summary、写入和hydrate。原始参数留本地 `.runtime/sync-overall-design-20261031/query-plans.jsonl`，不提交账户标识。

| 维度/日期 | 优化器估算主要事实行 | root cost（不是毫秒） | 观察 |
| --- | ---: | ---: | --- |
| 系列1日 | 4319 | 19444 | 用日期索引；多层聚合/排序 |
| 素材1日 | 167308 | 1990480 | 用月分区和索引；大量映射/使用关系/对象LATERAL和JSON聚合 |
| 系列7日 | 见原始计划 | 97474 | 工作随日期范围增加 |
| 素材7日 | 1308384 | 7158417 | 即使最终LIMIT50，前面的事实与关联集合成本仍大 |

计划生成约0.07–0.15秒，**不是页面耗时**；estimated rows/cost不能转成实际倍率。统计视图当时query_snapshot仅9条、query_snapshot_row约13716条，不能把此次慢归因于查询快照表历史堆积巨大。

既有生产事实见[全链路审计](2026-10-31-sync-chain-audit.md)。01:32后续状态（异步采样，非单一事务）：同一12393旧任务批次未完成11772→6788，约75分钟；全量待处理素材日报8592、补链1767；live目录运行7505，最老约47分钟；background目录22372。约5分钟素材拆分2816次HTTP尝试、2684成功、127次40100；本地endpoint_rate拒绝3282次。该证据证明既有修复有收益、系统仍有共同瓶颈，不能当成新方案已验证。

## 下一步必须补齐的实验

- 页面首屏/翻页/汇总/状态轮询分阶段耗时及查询指纹；当前没有完整生产P50/P95，用户体验不能被EXPLAIN估算替代。
- 同一脱敏数据/请求trace的旧版、新版、只读、只同步、混合负载五组对照；数据库缓存、连接预算、硬件、日期/授权范围一致。
- 固定50个素材行，将无关事实、命名历史和终态任务放大10倍，检查页面/调度成本是否与无关历史脱钩。
- 将来源额度、有效行/请求、到达率和消化率分别测量，解释改善来自减少工作、减少重复成本还是增加合法容量。

方法依据：[PostgreSQL EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html)说明估算成本与实际执行统计不同；[Celery 容量说明](https://docs.celeryq.dev/en/stable/userguide/optimizing.html)要求关注任务到达与处理能力，单看队列配置不足以证明可持续。
