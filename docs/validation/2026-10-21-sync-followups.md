# 同步精简后续修复交接（2026-10-21）

**更新：** 用户在ff0bb7b5后批准同时修复五处金额判定，阶段4产品提交340108c7已经推送并发布生产（Jenkins #101 SUCCESS）；本地1034项全通过，CI后端1017/前端626通过。原46个等待中44个无新增请求自然完成，详见[发布验收](2026-10-21-sync-followups-release.md)。以下原审计保留为当时决策证据。

阶段 1–3 已完成本地实现和验证；阶段 4 按用户“发现依赖即停止、不修改”的条件停在审计。**第四次提交仅记录阻断，不代表阶段 4 产品行为已修复。** 未推送、未发布、未调用真实 TikTok、未连接线上数据库；没有 schema 变更。

## 提交与文件

| 阶段 | 提交 | 产品与测试文件 |
| --- | --- | --- |
| 1 双时钟 | `53a5888f` | `backend/app/modules/reporting/job_tasks.py`；`backend/tests/modules/reporting/test_job_tasks.py` |
| 2 规则前置相位 | `23abca2d` | `backend/app/modules/reporting/planner.py`、`directory_refresh.py`；`backend/tests/modules/reporting/test_planner.py` |
| 3 当地昨日收尾 | `d2d78398` | `backend/app/modules/reporting/planner.py`、`job_tasks.py`、`jobs.py`、`lanes.py`；新增 `backend/tests/modules/reporting/test_day_closeout.py`，扩展 `test_job_tasks.py` |
| 4 依赖审计，实施停止 | 本交接文档所属 `reporting: 记录零金额素材关联依赖并暂停范围收缩` 提交 | 仅本交接文档、`docs/implementation-progress.md` 和执行计划；无产品或测试源码变更 |

各阶段同步更新 `docs/implementation-progress.md` 和 `docs/superpowers/plans/2026-10-21-sync-followups.md`。阶段 1 同时新增用户指定设计 `docs/superpowers/specs/2026-10-d`（按字面路径，无扩展名）。原第四次审计提交为 `ff0bb7b5`。

## 实现与测试覆盖

1. **双时钟**：进入轮次时冻结热报是否到期；素材独自到期不能补广告日报。热报筛选失败仍为已知活跃/当日有事实账户兜底；原热报相位同时推进热报和素材。更新失败/退避/权限失败测试；新增错开 ±7 分钟、完成退出在途去重、连续三轮的回归，净增 2 项。
2. **规则前置**：复用 `directory_refresh.rule_accounts`，有启用规则的 owned 账户所在 BC 对齐 UTC :12/:27/:42/:57，`RULE_SYNC_LEAD=3min`。原相位存在已有 `screening_progress.rule_sync_original_phase`，停用规则后恢复并清理该键。无规则不改相位，不改规则执行、阈值、新鲜度或账户范围。新增 10 项覆盖 ALL/BC/ACCOUNTS、无归属账户、启停、刻度边界、多轮迟到与失败退避。
3. **昨日收尾**：按账户当地昨日事实选范围；每种事实成功水位晚于当地零点则跳过。现有收尾任务记录防止下一周期重建，包括 WAITING/FAILED/COMPLETE；原任务自行重试，每日回刷仍能覆盖。新 `closeout` 实时档 priority=0、独立 2 个在途名额，今日先派发且保留原 24 个名额。过期到前天则自动转 backfill，无新队列/服务。新增 12 项收尾测试，原 PostgreSQL 并发上限/租户轮转测试增加 3 项；覆盖实际发布、后续每日修正、夏令时、45 分钟时区、筛选失败、无事实、所有权与旧热报不能吞掉收尾。
4. **依赖审计**：未增加或更改产品测试文件；用现有规则夹具在专用本地 PG 的回滚事务中复现零金额关联依赖，详见下文。

## 门禁的实际结果

所有命令在 `backend/`，通过仅继承 PATH/HOME 的清洁环境运行，`APP_ENV_FILE=/dev/null`。数据库为本机 PostgreSQL 17.5 `127.0.0.1:55483/tkada_sync_followups_test`，Redis 8.0.1 `127.0.0.1:56483`，测试 DB 1 与应用 DB 0 分离。并发测试使用真实 PG 多连接，外部读取在传输边界替代。

固定命令：

```bash
uv run ruff check app tests && uv run ty check app
uv run mypy app
uv run ruff format --check <本阶段所有改动和新增的 Python 文件>
uv run pytest tests/modules/reporting tests/modules/automation_rules tests/modules/ads -q
```

| 阶段 | 完整 pytest | Ruff / ty | 改动 Python 格式 | mypy app |
| --- | --- | --- | --- | --- |
| 1 | 965 passed，45.78s | 通过 | 2 文件通过 | 505 条/74 文件，与上个提交无新增 |
| 2 | 975 passed，46.38s | 通过 | 3 文件通过 | 502 条/74 文件，减少 3 条，无新增 |
| 3 | 990 passed，46.73s | 通过 | 6 文件通过 | 502 条/74 文件，无新增 |
| 4 审计 | 990 passed，46.09s | 通过 | 无 Python 变更，不适用 | 502 条/74 文件，与阶段 3 无新增 |

mypy 并非全量通过，退出码仍为 1；比较方法为按“文件路径＋错误文本＋错误代码”的多重集对比上个提交，仅规范化源码位置及诊断文本里的行号，防止挪动行号被误报为新增。没有忽略新增类型错误。日志与临时验证脚本在本机 `/tmp/tkada-sync-followups/`，不提交运行环境或数据库文件。

修复前先复现：阶段 1 定向 5 failed/1 passed；阶段 2 定向 5 failed/3 passed；阶段 3 初始 6 项失败，后续“终态也不得重复规划”再复现 2 failed/1 passed。补一次性规划时漏填 SQL 参数曾导致专项失败，已修复并重新完整回归；没有把失败跳过或当作通过。

## 阶段 4：为什么当时关联零金额素材

已读取 `8bd35045d4dcecebf21e5dad6ff42d434ae76e18` 中的 `material_links.py`、发布关联回归及对应原始计划/验证文档；同时核对当前调用链。

- [当时计划](../superpowers/plans/2026-10-18-sync-service-time.md) 的业务起因是“只按正消耗筛选”遗漏了有展示/素材事实的零花费账户，不能以空桶冒充成功，也不能隐藏这些事实。该计划要求真实非空事实继续取数。
- [当时验证](2026-10-18-sync-service-time.md) 明确记载：素材发布后，仅空窗口或已映射窗口不创建关联，零花费与 NULL VID 仍补链。
- `8bd35045` 新增的 `needs_material_links` 注释明确“零消费也需要对照”；其 `test_material_publish_only_queues_real_link_gaps` 用 spend=0 行要求建链。这是有意保留的完整事实映射合同，并非意外漏写金额条件。文档没有量化长期零金额补链的请求成本，不能据此推断零金额关联已经没有业务依赖。

## 依赖已经确认，不是猜测

**规则依赖一：完全未定位帖子。** [material_inputs.py](../../backend/app/modules/automation_rules/material_inputs.py) 的 `unlinked_report_accounts` 对规则日期窗口内任意未找到 `MaterialPostLink` 的事实标记账户，不限定消耗/收入；`material_entities` 将其转为 `MATERIAL_REPORT_UNLINKED`，阻止该账户素材规则判断。

**规则依赖二：所属广告已知但缺 VID。** 同文件把 `platform_material_id is None` 的关联加入 `unresolved_ads`，将对应广告素材标为 `MATERIAL_LINK_UNRESOLVED`，同样没有金额过滤。

复现复用 `test_material_inputs.py` 的 `setup/use/post/result` 与 `sync_conftest.sync_seed`，用随机租户在本机专用 PG 单事务内完成，最终 rollback，无外部接口调用：

| 合成场景 | 规则命中数 | 原因 |
| --- | --- | --- |
| 已映射素材花费 30、收入 15，原规则 | 1 | 正常命中 |
| 加入 spend=0、revenue_total=0 的未定位帖子 | 0 | `MATERIAL_REPORT_UNLINKED` |
| 给零金额帖子补广告归属，但 VID 留空 | 0 | `MATERIAL_LINK_UNRESOLVED` |

原始结果保存在本机 `stage4-audit.log`，断言均通过。脚本首次单独启动未导入完整模型注册，发生 `NoReferencedTableError`；补齐与 pytest 相同的 `app.main` 模型加载后复现通过。这是本地验证脚本初始化问题，没有修改产品模型或数据。

**页面/报表也消费映射。** [row_set.py](../../backend/app/modules/reporting/row_set.py) 的 `_material_rows` 使用 `POST_VIDEO_SQL`，已映射帖子按视频归并，未映射帖子保留独立行；[material_query.py](../../backend/app/modules/reporting/material_query.py) 用同一映射归并事实/展示指标，不以 spend>0 为前提。因此“零消耗零收入”不等于没有展示/点击等指标，也不能声称停止补链对页面分组没有影响。

**只改用户列出的三个关联判定会造成的后果：** 零金额帖子永久不补关联，而现行规则仍把它当缺数，可长期阻止正常素材止损/恢复。由于这是实际依赖，按本轮明确停止条件，不实施范围收缩，不把这些行伪标为已关联，也不顺手改规则缺数语义。

## 负证据实际存放位置

[store_models.py](../../backend/app/modules/reporting/store_models.py) 的 `MaterialLinkScan` 对应现有 `material_link_scan` 表，主键是 tenant/advertiser/query_scope；保存 `scanned_at`、`unresolved_ids`、`evidence_signature`。`query_scope` 是 `_scan_scope(start,end,overview_ad_ids)` 的摘要。`material_links.refresh_material_links` 在完成扫描后持久化证据；缓存使用集合覆盖、当前签名匹配及 `NEGATIVE_SCAN_TTL=6h` 共同判断，不能只见一条扫描记录就算平台缺口。

另外，等待任务的 `progress` 保存 `waiting_ids`、`dependency_signature`、`dependency_checked_at` 等依赖复查信息。`MaterialLinkScan` 的 unresolved 集合来源于 `_unlinked`（无广告归属），不天然等价于全部“缺 VID”集合。后续如获准继续，必须区分已扫描找不到归属与已知归属但缺视频证据，不能把错误码或等待时长当作平台负证据。目前没有为此新增 schema，也没有声称现有证据能覆盖所有缺 VID 场景。

## 偏离与验收风险

- 最重要的偏离：只完成三阶段产品修复；第四阶段仅完成审计并停止，原因是用户明确的依赖停止条件已成立。`needs_material_links`、`missing_video_ids`、`dependency_changed`、健康分类均未改动，生产等待成本与平台缺口告警仍待处理。
- 用户提供的 pytest 字符串有截断，恢复为 reporting、automation_rules、ads 三模块；设计文件名按用户字面保留。未启动分支、工作树或子代理。
- 初始未跟踪文件未被本会话修改。另一个会话在阶段 1 期间创建 `98c664c8`（素材控损设计文档及其进度），本会话没有撤回、混入或重提交它；后续追加进度保留了其内容。
- 规则同步有意在 :12/:27/:42/:57 集中，需上线后验证 BC 数量增长时的准入与 p95；3 分钟是根据用户提供生产时延选择，不能用本地测试保证每轮都及时。无规则 BC 维持独立相位。
- 收尾独立 2 个名额保护今日数据库在途容量，但不是物理 Worker 隔离；同样共享 Worker、SDK 配额和 PG。重点验收当地切日后今日新鲜度、收尾完成耗时、队列与 PG 查询负载。收尾终态失败后不会每轮重建，只能由原有重试/每日回刷补偿。
- 新 priority=0 和 JSON 相位键没有 schema 迁移；以后若发布，要按调度/持久化任务合同变更检查冻结、旧任务自然消费及回滚行为。本轮没有发布授权，不能直接上线。
- 继续阶段 4 前，需要用户确认是否把“规则缺数判定与关联范围保持一致，以及零金额帖子的页面展示/归并语义”纳入变更范围；在此之前保持现行规则安全判断，不绕过缺数保护。


## 阶段 4 追加批准后的实现与验证

历史审计结论不撤回：8bd35045原本要保留非空零花费事实并完整映射，规则和页面确有依赖。用户现在认定零金额缺关联导致止损失效是缺陷，批准同时收窄规则保护；未改变页面归并算法，未映射帖子继续独立展示。健康分类因负证据只覆盖广告归属而取消，待上线后观察剩余缺口。

产品文件：新增 `backend/app/modules/reporting/material_value.py`；修改 reporting 下 material_links.py、jobs.py、directory_refresh.py、material_identity.py、spark_identity.py；automation_rules 下 material_inputs.py、inputs.py、data_issues.py；前端 dataIssues.ts。五处判定共用窗口合计，执行候选同步过滤；规则付费指标排除零金额曝光，忽略计数复用原运行摘要。未新增schema或修改health.py、row_set.py、material_query.py。

测试文件：新增 reporting/test_material_value.py；扩展 reporting/test_material_resume.py、test_material_links.py、test_backlog_control.py、test_backlog_dependencies.py、automation_rules/test_material_inputs.py；前端 automation-rule-form.spec.ts。原审计三个场景现均命中1，零金额缺口分别记忽略数1；有消耗/仅收入缺口仍命中0并保留缺数原因。跨日合计抵消为0则不追，合计0.01则追；多个规则窗口分别判断，租户/账户/日期隔离；旧WAITING经真实PG依赖重查、领取和正常执行COMPLETE，不发平台请求，不创建假映射。运行记录跨日期重复帖子按账户+帖子计数2并可回读。

实际验证：三模块 **1034 passed / 58.68s**（净增44），Ruff/ty通过，15个改动Python格式通过；mypy499条对上个提交502条无新增，存量错误仍在。前端两文件Biome、应用tsc通过，Playwright文案1项通过。过程失败及修复细节见实施进度，不将初轮失败记为通过。所有后端测试使用独立本机PG/Redis，日志仅在/tmp，不写生产或调用TikTok。

范围说明：补链候选函数的同步过滤是防止“有金额任务夹带零金额补链”的必要实现；inputs.py付费聚合落实“无消耗曝光不计付费效率”，不改20分钟新鲜度或规则执行刻度。用户追加生产发布授权取代原不推送/不发布约束，其余隔离、真实映射与不手改任务状态要求保持。生产容量、旧任务自然收敛速度与剩余健康缺口以实际发布后观测为准。
