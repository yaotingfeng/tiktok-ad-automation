# 广告报表数据同步重新设计

日期：2026-10-12。状态：方向已在对话中获用户确认，本稿待用户审阅。

本稿替代 `2026-09-30-ads-management-reporting-design.md` 中第 6、7 节（指标合同、同步调度）及其对应的存储设计；该稿中的工作台交互、剧识别规则、批量操作业务规则继续有效。

## 1. 目标与已确认决定

目标：广告报表功能可用、数据准确、可扩展到 SaaS 规模（单应用 1 万个以上广告账户）。

用户已确认：

- 数据同步只走官方 API（SDK 通道），不走 MCP；不使用异步报表和订阅推送。
- 授权后自动开始同步，不需要先点"刷新"，不做按 BC 开关；`ADS_SYNC_ENABLED` 仍是实例级总开关。
- 只拉广告维度明细，系列、广告组、账户、剧四级均由广告数据汇总；账户维度展示值也用广告汇总。
- 用 BC 报表筛选有消耗的账户，只对活跃账户拉明细。
- 素材报表第一版一起交付，不分期。
- 以按天数据为准；只对系列层额外保留近期小时数据。
- 当前只有 Upgraded Smart+ 广告，不为普通广告单独设计；统一用 `ad_id_v2`，将来出现普通广告时自然包含。
- 业务口径按建议执行：日期按广告账户时区；另存一份 UTC 系列日数据供跨账户汇总和日报使用；回收指标为 D0、D3、D7、D14、D30 与总收入。

## 2. 平台事实与依据

以下均来自 TikTok API for Business v1.3 官方文档，或用新骏伯 BC 的只读实测（2026-10-11/12，账户 `7691249267022446613`）。

| 事实 | 依据 |
| --- | --- |
| 同步报表 `advertiser_ids` 最多 5 个，且须属于同一授权用户 | run-a-synchronous-report："Max size: 5" |
| 同步报表最多返回 2 万个广告，超出截断；按天维度一次最多 30 天，按小时一次最多 1 天 | 同上 |
| 默认不含已删除对象，需 `ad_status=STATUS_ALL` | 同上 |
| `ad_id_v2` 覆盖手动、Smart+、Upgraded Smart+ 广告级；`ad_id` 对 Upgraded Smart+ 是创意级 | 同上（`campaign_automation_type` 说明） |
| Native Growth 指标只能用同步基础报表 | basic-reports-supported-metrics："You can only use TikTok Native Growth metrics in synchronous basic reports" |
| 异步报表请求 D0/总收入时任务失败；只请求消耗、展示时成功 | 实测（task 7695433941737537557、7695434255086256149、7695434959003058183 失败；7695434252233474068 成功） |
| 订阅推送只含账户级 UTC 当日消耗/展示/点击/播放，需白名单 | subscribe-to-ad-account-webhook-events-via-subscription-api |
| 应用级限流：Basic 每秒 10 / 每分钟 600 / 每天 864,000，触发返回 40100，QPM 触发需等 5 分钟 | rate-limits |
| 实时数据延迟约 30 分钟，极端 2 小时以上；每天 UTC 12:00 有数据修正脚本 | data-latency-for-reports |
| BC 报表 `spend` 是分钟级实时数据；一次请求返回整个 BC 下账户的消耗 | data-latency-for-reports；实测一次返回 120 个账户 |
| 广告报表能自带 `campaign_id`、`adgroup_id`、三级名称和 `campaign_automation_type`（作为 metrics 请求），多账户合并时每行带 `advertiser_id`、`currency`、`timezone` | 实测（2 个账户合并，2026-10-12） |
| 广告级汇总 = 系列级 = 账户级：消耗、展示、点击、展示事件完全一致；收入因单条舍入差 0.01 | 实测 2026-10-05：广告 160 行汇总消耗 499.29，与账户、19 个系列逐个一致；D0 收入 135.84 对账户 135.85 |
| 素材分时报表支持"主素材 × 天"、整账户一次拉取，返回 D0/D7/D14/总收入（字段名与基础报表不同）；汇总消耗与账户一致 | supported-dimensions-for-upgraded-smart-creative-breakdown-reports；实测消耗 499.29、D0 收入 135.84 |
| 拉广告级报表时官方建议按系列分批，每批不超过 30 个系列；超时不要原样重试 | best-practices-to-reduce-timeouts-in-synchronous-reports |

## 3. 拉取方案

### 3.1 四类请求

| 请求 | 接口与参数 | 范围 |
| --- | --- | --- |
| 筛选 | `/report/integrated/get/`，`report_type=BC`，`bc_id`，维度 `advertiser_id`，指标 `spend` | 每个已绑定 BC，分页 1000 |
| 广告日明细 | 同步报表 `BASIC`，`data_level=AUCTION_AD`，维度 `ad_id_v2`+`stat_time_day`，过滤 `ad_status=STATUS_ALL`，`advertiser_ids` 最多 5 个 | 筛选出的活跃账户 |
| 系列小时 | 同步报表 `BASIC`，`AUCTION_CAMPAIGN`，维度 `campaign_id`+`stat_time_hour` | 活跃账户的今天与昨天 |
| 素材日明细 | `/smart_plus/material_report/breakdown/`，维度 `main_material_id`+`stat_time_day`，单账户 | 活跃账户 |

另有两类每日一次的请求：

- UTC 系列日：同步报表 `AUCTION_CAMPAIGN`，`multi_adv_report_in_utc_time=true`，用于日报和跨账户 UTC 汇总。
- 对账：同步报表 `AUCTION_ADVERTISER` 按天，只用于和广告汇总比对，不进入展示。

广告日明细请求的 metrics：`spend`、`impressions`、`clicks`、`native_growth_total_ad_impression_event_count`、`native_growth_ad_revenue_value_d0`、`native_growth_ad_revenue_value_d2`、`native_growth_ad_revenue_value_d6`、`native_growth_ad_revenue_value_d13`、`native_growth_ad_revenue_value_d29`、`native_growth_total_ad_impression_value`、`campaign_id`、`adgroup_id`、`campaign_name`、`adgroup_name`、`ad_name`。平台 `dN` 表示点击后 N+1 天累计，故 D3/D7/D14/D30 分别对应 `d2/d6/d13/d29`。

素材日明细 metrics：`spend`、`impressions`、`clicks`、`onsite_total_ad_impression`、`onsite_ad_impression_ad_revenue_day0`、`onsite_ad_impression_ad_revenue_day6`、`onsite_ad_impression_ad_revenue_day13`、`onsite_total_ad_impression_value`、`main_material_name`。素材报表没有 D3、D30，这两列在素材维度显示"平台未提供"。

### 3.2 活跃账户筛选

- 热刷：BC 报表查当天（账户时区由平台按各账户返回），`spend > 0` 的账户为活跃。
- 回刷：BC 报表查最近 32 天区间，区间内 `spend > 0` 的账户进入回刷。D1–D30 收入归属点击发生日，当天必有消耗，因此不会漏收入。
- 历史回补：不筛选，所有账户从开户日起按 30 天切片拉一次；没有数据的切片返回空，成本很低。
- 授权用户没有 BC 报表权限时（授权时检查并记录），该 BC 退回用账户级报表每次 5 个账户筛选。

### 3.3 防截断

- 广告日明细按账户上次返回行数自适应合并：5 个账户上次合计行数 × 日期数估计超过 15,000 行时，减少合并账户数，直到单账户一批。
- 单账户仍可能触顶（返回 `total_number ≥ 20,000`）时，用上次结果中的系列 ID 按每批 30 个过滤重拉；首次没有系列 ID 时，先拉一次该账户的系列日报表取 ID。
- 触顶且无法拆分时，该批次标记失败、不发布，页面显示缺口，绝不把截断结果当完整数据。

### 3.4 调度节奏

| 档位 | 内容 | 频率 | 优先级 |
| --- | --- | --- | --- |
| 当天热刷 | 筛选 + 广告日（今天；UTC 12:00 前含昨天）+ 系列小时（今天、昨天） | 每 30 分钟 | 1 |
| 素材热刷 | 素材日（今天，UTC 12:00 前含昨天） | 每 60 分钟 | 2 |
| 手动刷新 | 指定账户立即执行一次当天热刷 | 按需，短时间内重复点击合并 | 1 |
| 每日回刷 | 筛选 32 天 + 广告日、素材日最近 32 天 + UTC 系列日 + 对账 | 每天 UTC 12:30 后 | 3 |
| 历史回补 | 开户日至 32 天前的广告日、素材日、UTC 系列日 | 授权后一次，按 30 天切片 | 4 |

- 32 天前的数据视为定稿，之后不再自动回拉；用户手动刷新历史区间时按需重拉。
- 新账户授权完成或新 BC 绑定完成后，立即创建该账户的计划；解除授权后停止计划，已存数据保留但不再可见于失去授权的 BC。
- 后台身份：计划记录完成授权或账户发现的操作者；该操作者失去租户权限时计划停用并提示重新授权。

### 3.5 调用量估算（1 万账户、2,000 活跃、50 个 BC）

| 每 30 分钟一轮 | 请求数 |
| --- | --- |
| BC 筛选 | 约 50 |
| 广告日 | 约 600（400 批 + 翻页） |
| 系列小时 | 约 500 |
| 素材日（每小时，折算每轮） | 约 1,000 |
| 合计 | 约 2,150 |

每天约 10 万次热刷，加回刷约 2 万次，历史回补约 11 万次一次性摊入数日。均在 Basic 额度内。

## 4. 限流与多租户公平

- 复用现有 `TIKTOK_CALL_POLICIES` 与 Redis 额度准入，配置成应用级共享令牌桶（按应用额度，预留 20% 给管理、搭建、目录读取）。
- 报表任务按租户轮转领取，单租户在一轮中最多占用其账户比例的份额，大租户不能饿死小租户。
- 收到 40100：QPM 触发等待 5 分钟，QPD 触发等到次日 UTC 0 点；任务延后而不失败，不计入失败重试次数。
- 平台读取错误（超时、5xx、传输错误）按 60s、120s、240s…最多 5 次退避；超时重试时按官方建议缩小范围（减少合并账户数或改按系列分批）。
- 参数或合同错误（40002 等）立即失败并告警，不重试。
- 每轮记录实际调用数和额度余量；余量不足 30% 时热刷间隔自动拉长到 60 分钟，素材拉长到 120 分钟，并在页面显示"数据更新放缓"。

## 5. 数据存储

### 5.1 原则

- 报表事实按"租户 + 广告账户"存放，不挂在 BC 下；BC 能看到哪些数据由它当前对账户的授权决定。
- 一行一个对象一个时间桶，各指标各占一列。
- 整份替换：某个"账户 × 报表类型 × 日期段"全部分页成功后，在一个事务内删除该段旧行再写入新行；中途失败保留旧数据并标记过期。平台未返回的行表示当天确实没有数据，删除即正确。
- 缺失与 0 分开：平台返回 `-` 存 NULL。
- 金额用 `numeric`，保留平台精度，显示层才舍入。
- 名称、状态、预算、目标 ROAS 仍由广告目录（`ad_object`）提供；报表行带回的名称只在目录尚未同步时临时显示。

### 5.2 表结构

`ad_report_day`（唯一明细来源，按月分区）

| 列 | 说明 |
| --- | --- |
| `tenant_id`, `advertiser_id`, `stat_date`, `ad_id` | 主键；`stat_date` 是账户时区日期，`ad_id` 存 `ad_id_v2` |
| `campaign_id`, `adgroup_id` | 上级关系 |
| `campaign_automation_type` | 广告类型 |
| `currency`, `timezone` | 账户币种、时区 |
| `spend`, `impressions`, `clicks`, `ad_impression_events` | 基础指标 |
| `revenue_d0`, `revenue_d3`, `revenue_d7`, `revenue_d14`, `revenue_d30`, `revenue_total` | 收入 |
| `ad_name`, `campaign_name`, `adgroup_name` | 报表带回的名称，仅兜底显示 |
| `fetched_at`, `sync_batch_id` | 拉取时间与批次 |

`campaign_report_day`：主键 `tenant_id, advertiser_id, stat_date, campaign_id`，指标列同上，由 `ad_report_day` 在同一事务内汇总写入，作为查询加速表，不独立拉取。

`campaign_report_hour`：主键 `tenant_id, advertiser_id, stat_hour, campaign_id`（`stat_hour` 带时区时间戳），指标为消耗、展示、点击、展示事件、D0 收入；保留最近 30 天，每日清理。

`campaign_report_day_utc`：主键 `tenant_id, advertiser_id, stat_date_utc, campaign_id`，指标同 `campaign_report_day`；供日报和 UTC 跨账户汇总。

`material_report_day`（按月分区）：主键 `tenant_id, advertiser_id, stat_date, main_material_id`；列 `main_material_type`、`main_material_name`、`spend`、`impressions`、`clicks`、`ad_impression_events`、`revenue_d0`、`revenue_d7`、`revenue_d14`、`revenue_total`、`fetched_at`、`sync_batch_id`。

`account_reconciliation_day`：主键 `tenant_id, advertiser_id, stat_date`；存账户报表原值与广告汇总值及差额，仅用于对账提示。

`report_sync_state`：主键 `tenant_id, advertiser_id, report_kind, stat_date`；列 `status`（COMPLETE / STALE / FAILED）、`row_count`、`last_success_at`、`error_code`、`attempts`。页面据此显示每个账户数据更新到几点、哪些日期缺失。

`report_sync_job`：调度与执行队列（一次请求一行）。列：任务类型、优先级、租户、BC、账户列表、日期段、分页游标、状态、下次执行时间、领取租约、重试次数、冻结路由。完成 7 天后清理。

### 5.3 索引与规模

- 分区表索引：`(tenant_id, stat_date, advertiser_id)`、`(tenant_id, campaign_id, stat_date)`、素材另加 `(tenant_id, main_material_id, stat_date)`。
- 1 万账户、2,000 活跃、每账户每日 100 条广告：`ad_report_day` 约每年 7,300 万行，按月分区每区约 600 万行；1 年以上的分区可按需归档。

### 5.4 删除的旧结构

`report_fact`、`report_coverage`、`report_staged_page`、`report_observation`、`report_sync_run`、`sync_schedule` 及其代码路径，MCP 报表适配器、异步报表任务路径，按广告类型拆分的报表合同、创意级报表。迁移不保留旧数据，上线后由历史回补重新拉取。

## 6. 查询与汇总

- 账户、系列、广告组、广告、剧五个维度都从 `ad_report_day` / `campaign_report_day` 用 SQL 汇总、排序、分页；素材维度从 `material_report_day`。
- 剧维度：系列名按既有规则解析出"版权方 + 剧名"，对 `campaign_report_day` 汇总。
- 派生指标查询时计算：各 ROAS = 合计收入 / 合计消耗；点击率 = 点击 / 展示；单次展示事件成本 = 消耗 / 展示事件；分母为 0 时为空。
- 某指标在所选范围内存在 NULL 时，合计为空并标明缺失，不显示部分合计。
- 所有列支持升降序：在 SQL 中排序，空值始终排末尾，`row_key` 作为并列值保证翻页稳定。
- 不同币种不合计：汇总按币种分组，混币种时分币种展示。
- 账户维度显示广告汇总值；若当日对账差额超过 0.01，行上显示对账提示及账户报表原值。
- "全部匹配结果"选择与批量操作：沿用现有快照与冻结选择合同，快照只保存行键与筛选摘要，过期快照由定时任务清理。
- 页面每行显示数据更新时间；所选范围内有 STALE/FAILED 日期时显示"数据不完整"及缺失日期。

## 7. 错误处理与可观测

- 每批次结果写入 `report_sync_state`；连续失败、截断风险、对账差额、额度余量不足均记录并在运维页显示。
- 授权失效或账户失去授权：停止该账户任务，页面提示，已存数据对失去授权的 BC 不可见。
- 计划生成、领取、发布都在数据库事务内，任务可中断续接，重复投递幂等。

## 8. 实施阶段

1. 存储与同步核心：新表与迁移、官方 API 报表读取（BC 筛选、广告日、系列小时、UTC 系列日、对账）、整份替换发布、调度与限流、授权后自动建计划。验收：用新骏伯 BC 在 staging 跑通，广告汇总与账户报表逐日核对。
2. 素材报表：素材日拉取、存储、素材维度查询与素材使用位置关联。
3. 查询层与页面切换：SQL 汇总与全列排序、数据完整性提示、对账提示；前端改用新接口。
4. 清理：删除旧表与旧代码路径，更新部署手册与功能开关说明。

四个阶段全部完成并在 staging 验收后才发布；素材不单独延后。

## 9. 验收标准

- 新骏伯 BC 全部授权账户，最近 30 天每天：广告汇总消耗 = 账户报表消耗；收入差额不超过 0.01 × 当日广告数；系列逐个一致。
- 已删除广告的历史消耗出现在汇总中。
- 模拟单账户 2 万行触顶：自动拆分为按系列拉取，结果完整；无法拆分时标记失败而不发布。
- 模拟 1 万账户（合成数据与传输层替身）：一轮热刷调用数、额度准入、租户公平和退避行为符合第 3.5、4 节。
- 所有列排序正确、空值排末尾、翻页不重不漏。
- 授权新 BC 后无需点击刷新即开始同步。

## 10. 待验证事项

- 官方 API 通道（非 MCP）拉 BC 报表所需的授权范围，以及 staging 应用是否已有该权限。
- 素材报表单账户结果是否也受 2 万行截断，以及是否需要按素材类型分批。
