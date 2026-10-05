# 广告报表数据接入重新评估（2026-10-05）

## 结论

当前测试环境的“数据缺失”首先是运行链路没有开启，不是 TikTok 已经返回了空报表：staging 发布记录明确写着 `ADS_SYNC_ENABLED=false`，`ads-directory` 和 `ads-reporting` 服务单元保持 inactive，也没有调用真实 TikTok API 或 MCP。前端查询只读本地已发布事实，页面 GET 不会临时向 TikTok 查询；本地没有 `ReportFact` 或 `ReportCoverage` 时，页面只能把指标显示为缺失。

现有代码适合作为“本地事实库 + 有界拉取”的基础，但还不能满足“可查看历史所有投放数据”。需要先完成一次真实只读联调和全历史回补，再开启周期同步。当前方案有四个范围缺口：

1. 测试环境实际没有运行同步任务。
2. 历史计划只做初始 30 天、归因窗口和滚动 90 天，没有无限历史回补或已完成区间清单。
3. 异步报表虽然有 SDK/MCP 适配器，调度却没有把普通报表切换到异步任务；当前运行通常走同步分页。
4. 报表过滤器没有显式把 `campaign_status`、`adgroup_status`、`ad_status` 设为 `STATUS_ALL`。TikTok 官方说明基本报表默认只返回未删除对象，所以暂停/删除后的历史对象可能不会被首次回补发现。

## 官方接入方式

TikTok API for Business 的 Reporting 是查询型接口。官方文档把报表分为同步和异步两条路径：

- 同步：`GET /open_api/v1.3/report/integrated/get/`，按 `report_type`、`data_level`、`dimensions`、`metrics`、日期范围、过滤条件、`page`、`page_size` 查询，读取 `list` 和 `page_info`。
- 异步：`POST /open_api/v1.3/report/task/create/` 创建任务，随后 `GET /report/task/check/` 查询状态，成功后 `GET /report/task/download/` 获取结果文件或下载地址。
- 官方 SDK 仓库也把这几个操作分别暴露为 `reportIntegratedGet`、`reportTaskCreate`、`reportTaskCheck`、`reportTaskDownload`；不能把 SDK 的 `async_req` 本地线程包装误当成 TikTok 的异步报表任务。

官方 API for Business 首页和 Webhook 说明把推送场景描述为线索、广告审核、订单等事件通知；报表页仍是同步/异步查询。当前没有足够的官方报表订阅字段合同证明“历史性能指标会以可重放的完整行推送”，因此报表数据必须以拉取为准。若后续开通 Reporting Subscription API，可把它作为刷新触发或变更提示，仍要用查询回读并保留周期校正，不能替代历史回补和断点恢复。

TikTok 官方还明确说明报表数据不是固定实时值：普通基本报表的多数指标通常约 30 分钟延迟；UV、现金/券类指标或特定维度通常约 16 小时；部分受众维度可到 24 小时；Business Center 的税费可到 48 小时。实时数据会在次日修正，官方建议 UTC 12:00 之后再次拉取以取得修正结果。因此“每 30 分钟拉一次”只能作为普通基础指标的刷新周期，不能作为所有指标已经最终一致的承诺。

## 当前代码与实际需求的对照

| 领域 | 当前实现 | 评估 |
| --- | --- | --- |
| 页面读取 | `modules/reporting/queries.py` 从本地快照、`ReportFact`、`ReportCoverage` 读取，不在 GET 中访问 TikTok | 方向正确，避免页面请求被平台延迟拖住；前提是后台同步真的有成功发布 |
| 目录与报表 | 目录和报表分队列、事实与覆盖分表、页链和 claim generation 有恢复保护 | 结构合理，适合长期运行 |
| 测试环境 | `ADS_SYNC_ENABLED=false`；报表 worker inactive | 当前不可用的直接原因 |
| 周期 | 目录 3 小时、核心报表 30 分钟、近 7 日 3 小时、归因/历史按日或周 | 可作为目标节奏，但没有真实平台联调和实际耗时证据 |
| 历史 | 初始 30 天、归因窗口（默认 35 天）、滚动 90 天 | 不满足“所有历史”；必须做可继续的全量回补 |
| 异步 | SDK/MCP 有 create/check/download 适配器，但报表运行的 `task_status` 默认为空，调度没有选择异步 | 大账户或长历史仍可能同步超时 |
| 删除对象 | 目录同步有 `include_deleted`，报表 filtering 没有显式 `STATUS_ALL` | 不能保证历史暂停/删除对象可见 |
| 分片 | 有 `plan_report_shards` 工具，但调度路径没有实际使用它；报告过滤 ID 可能过大 | 需要按官方过滤器上限和响应警告拆分 |
| 页面刷新 | 由当前页面行反推 `advertiser_ids` 和 refs | 目录本身缺失时无法发起第一次有效回补，形成“没有数据所以不能刷新”的循环 |

## 建议的新接入方案

### 1. 先做一次真实只读接通

在选定的 BC 和授权账户上完成 API 与 MCP 各一条报告的对照读取：账户级、系列级、广告级各取一个已投放账户，日期取最近一个完整自然日和一个已知历史日期。保存请求参数、TikTok `request_id`、HTTP/业务 code、页数、返回行数和覆盖范围；不做广告写入。API/MCP 结果必须按同一账户、日期、时区、维度和指标比较，不能只验证 HTTP 200。

### 2. 以“目录快照 + 报表事实”分层回补

先拉取全部层级目录，包含暂停和删除对象，保留对象 ID、名称、父子关系、广告类型、创建时间和状态。再以账户为边界做历史报表回补：

- 基础指标用同步查询处理小窗口，用异步任务处理大窗口或超过同步超时风险的窗口。
- 日期按平台已验证上限切片，过滤 ID 按平台上限切片；每个分片有唯一键、页游标、请求序号和覆盖记录，失败只重试该分片。
- 对 campaign/adgroup/ad 级报告显式传 `campaign_status=STATUS_ALL`、`adgroup_status=STATUS_ALL`、`ad_status=STATUS_ALL`（字段与维度匹配），避免官方默认过滤掉删除对象。
- 回补不要只写“最近一次值”。同一分片允许后续版本覆盖，保留观察时间和请求证据，以应对次日数据修正。
- 回补计划按账户记录 `requested_start/end`、`completed_start/end`、缺失分片和最后错误；任务重启后从未完成分片继续。

### 3. 回补完成后再做周期同步

- 普通基础投放指标：每 30 分钟刷新近期窗口；窗口至少重叠最近 2–3 个完整自然日，以吸收延迟和次日修正。
- 每日 UTC 12:00 之后：重拉前一日及受影响的离线/UV指标。
- 受众、搜索、特殊维度：按官方 16–24 小时延迟安排，不要显示为“实时”。
- 每周：重拉滚动 90 日并检查历史修正；全历史只在新增账户、用户点击“历史回补”或发现覆盖缺口时回补。
- 目录：保持 3 小时刷新，目录成功是报表行名称/状态可用的必要条件，但不代替报表拉取。

### 4. 推送只做辅助

如果 TikTok 账户实际具备 Reporting Subscription API，接入它的通知落库为“刷新提示”，收到通知后仍走上述查询并做幂等回读。没有订阅能力、通知丢失或通知只给变更摘要时，周期拉取仍是兜底；不能以推送到达率代表历史报表完整率。

### 5. 页面和运维验收改为显示真实状态

页面至少区分：未启用同步、等待首批数据、平台返回空结果、覆盖不完整、平台读取失败、指标平台不提供、完整可用。刷新接口应允许直接选择账户 ID 发起目录/报表同步，不应依赖当前页面已经有 refs。后台要展示每个账户的最后成功时间、最近失败 code、待处理分片、覆盖日期和数据源（API/MCP）。

## 验收标准

在重新宣称“广告报表可用”前，必须满足：

1. API、报表 worker、Beat、Redis、PostgreSQL 使用同一份非空调用策略和同一版本配置，且测试环境开关已明确开启。
2. 选定 BC 的全部授权账户都能完成目录读取；至少一个账户的历史回补跨越 90 天并成功展示账户、系列、广告组、广告层级。
3. 删除/暂停对象在目录和报表中保留可追溯 ID；空结果与未覆盖不能混成“数据缺失”。
4. 同步分页、异步任务、下载文件、超时、限流、业务 code 非零和半文件各有真实或受控边界证据。
5. 同一日期/账户/维度/指标在 API 与 MCP 的差异有记录；差异属于平台延迟时显示数据时间和重拉时间。
6. 30 分钟周期至少观察一个完整工作日，历史回补吞吐、失败率、数据库写入量和队列积压都有量测。

## 需要保留的官方资料

- [TikTok API for Business Reporting 入口](https://business-api.tiktok.com/portal)：官方说明报表支持同步和异步模式。
- [官方 Python/JS SDK Reporting API 文档](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/ReportingApi.md)：同步报表、异步创建/检查/下载/取消接口。
- [Data latency for reports](https://business-api.tiktok.com/portal/docs/data-latency-for-reports/v1.3)：30 分钟、16 小时、24 小时、48 小时等延迟类别及次日修正说明。
- [Rate limits](https://business-api.tiktok.com/portal/docs/rate-limits/v1.3)：全局和 endpoint-specific 限制；部署时按实际 app/account 额度配置，不把本地默认值当作平台额度。
- [Reporting performance improvements](https://business-api.tiktok.com/portal/docs/reporting-performance-improvements/v1.3)：同步报表性能、超时和 `X-Tt-Ads-Throttle` 响应头等说明。
- [TikTok API v2.0 guide](https://business-api.tiktok.com/portal/docs?id=share)：只对有字段/枚举变化的接口提供 v2.0 增量说明，不能仅凭路径替换宣称已完成迁移。

## New Junbo 只读联调与本轮修正

用户已明确选择 New Junbo，本轮使用 `tiktok-ads-new-junbo` 连接完成只读核验：

- `user_info` 返回主体 `7683801818308428818`；`bc_get` 返回唯一 BC `7683817908149272592`（麦斯国际运营522），状态 ENABLE。
- `auth_advertiser_get` 返回 150 个授权广告账户。账户 `7691249596315533313` 的 `advertiser_info` 返回 USD、`America/Caracas` 和 `create_time=1790758652`。
- 同一账户调用同步 `report_integrated_get`（BASIC、AUCTION_ADVERTISER、2026-10-04、spend/impressions/clicks/conversion）成功返回一行：spend 176.93、impressions 417、clicks 241、conversion 131。
- 同一账户调用异步 `report_task_create` 成功取得任务 `7693121677747552276`；后续 `report_task_check` 从 PROCESSING 变为 SUCCESS，`report_task_download` 返回带 campaign、日期和指标列的 CSV（2026-10-03 至 2026-10-04 共 8 行）。没有调用任何创建广告、更新预算、状态启停或其他广告写接口。

代码已据此修正：账户目录保存 `create_time` 作为全历史回补起点；空账户列表会解析当前 BC 的全部已授权账户；报表过滤器显式保留 STATUS_ALL；报表过滤 ID 按 100 条分片；历史账户级报表使用官方异步任务，系列/广告组/广告历史保留同步路径以支持状态过滤；首次报表页为空时前端直接启动 history 回补，不再因没有本地行而拒绝刷新。

### New Junbo staging 发布更新（2026-10-05）

以上“待完成”事项已进入 staging 验收，但历史回补仍在后台继续，不能把当前排队量写成已完成：

- 目标只使用 `tiktok-ads-new-junbo`：BC `7683817908149272592`（麦斯国际运营522），150/150 个有效授权账户已完成目录发布，150/150 个账户已保存官方 `create_time`；最新目录运行 `COMPLETE`。
- 已应用 `reporting_account_history_start` 迁移。历史请求 `0ac76dbc-2d6d-59ba-8c21-d2e1f89f0afe` 创建了 1,080 个持久报表分片，日期按每个账户的官方创建时间到当前日生成；查询页、任务状态和覆盖记录都不会把排队误报为成功。
- API 应用对 `report/task/create` 返回官方 40118（异步报表仅限白名单），所以 New Junbo 当前冻结路由为 `OFFICIAL_API` 时使用同步分页；异步 create/check/download 适配器仍保留给实际具备白名单的 MCP 路由。该选择与真实回执一致，不再让全部账户历史进入必失败的异步路径。
- 素材报表请求已改为官方支持的维度：overview 使用 `advertiser_id + main_material_id`，breakdown 使用 `main_material_id + stat_time_day/hour`；`main_material_type` 从返回行读取，不再作为请求维度。Smart+ 创意级基础报表使用 `UPGRADED_SMART_PLUS` 过滤值，具体 `UPGRADED_SMART_PLUS_CREATIVE` 只作为返回语义，避免 40002。
- 报表 worker 已实际收到同步任务并发布事实；截至 13:24（Asia/Shanghai）数据库中 New Junbo 有 `367` 个 `COMPLETE`、`1,171` 个 `COMPLETE_EMPTY`、`18` 个 `RUNNING`、`2,903` 个 `QUEUED/PENDING`，无 `FAILED` 分片。已发布事实为 268,440 行、覆盖 46 个账户，时间范围到 2026-10-05；剩余分片由 worker 持续排空。
- 调度器已限制每轮最多处理 50 个到期计划、报表积压超过 500 个时暂停生成周期任务；本次 `reporting.scan_due` 实测约 0.4 秒成功，未再触发原先 25 秒软超时。所有 API/Worker/Beat 服务 active，Celery 7 个节点 ping 通过，`check-bootstrap.py` 通过。
- 本轮发布前完整备份批次至少包括 `/var/backups/tt-ada-staging/20261005T131434Z/`，项目归档 checksum 通过；当前运行 release 为 `155c000bdc23b08a3a09c191c77165c76b0a005e`。未调用广告创建、预算、状态启停或其他广告写接口。

因此当前状态是“接入和持续回补已正常运行，历史数据尚未全部排空”。普通页面会读取已发布本地事实；在覆盖完成前仍会显示覆盖中/排队状态，而不是把未完成分片伪装成完整数据。

### New Junbo staging 最终验证（2026-10-05 22:50 CST）

- 测试环境当前 release 为 `93d718f`，发布前备份 `/var/backups/tt-ada-staging/20261005T174700Z/` 的项目归档 SHA256 校验通过；发布后 API、全部 Worker、目录/报表 Worker 和 Beat 均为 `active`。
- `check-bootstrap.py` 通过；Celery 资源、结果、目录、控制、广告管理、搭建和报表 7 个节点全部 `pong`。
- 通过测试环境登录态调用本地报表 API：`account 200 5`、`campaign 200 5`、`material 200 0`。素材接口不再触发 OOM/502；它现在按授权账户分批读取事实，并只加载事实实际引用的素材映射。空结果作为正常空覆盖处理，不再错误显示为解析失败。
- New Junbo BC `7683817908149272592` 的最新数据库快照为 `COMPLETE=4396`、`QUEUED=569`、`FAILED=2`；按报表运行所属 BC 隔离后的已发布事实为 `422,658` 行，时间范围 `2026-07-08` 至 `2026-10-05`。历史回补仍在排队，未将队列宣称为完成。
- 修复了同步报表 `ad_id_v2` 的过滤字段（使用官方接受的 `ad_ids_v2`），并兼容官方 MCP 对合法零行结果返回空对象的情况。以上改动只影响报表读取和本地持久化，没有调用广告创建、预算、状态启停或素材上传接口。
- 报表事实与覆盖读取现在通过 `ReportSyncRun.bc_id` 绑定所选 BC；同一租户内多个 BC 共享广告账户时，不再把其他 BC 的历史事实混入 New Junbo 查询。
- 剩余 2 个失败分片均为素材 overview 的 `report_page_conflict`，已在测试环境重试仍可复现；账户、系列、广告组、广告主报表分片没有同类失败。该问题是两个账户的素材报表分页回执不符合当前页合同，属于局部卡点，不能把本轮回补宣称为 100% 完成。
