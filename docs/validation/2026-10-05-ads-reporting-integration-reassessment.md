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

本评估没有调用任何 TikTok Ads MCP 连接，也没有执行广告写入。真实只读联调下一步需要先明确使用 Xingyu、Junbo 还是 New Junbo BC，再按该 BC 的账户授权范围执行。
