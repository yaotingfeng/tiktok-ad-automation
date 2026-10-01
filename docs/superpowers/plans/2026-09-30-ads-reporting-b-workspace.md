# 六维广告工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前租户和 BC 内提供账户、系列、组、广告、素材、剧六维报表、搜索、趋势、详情、冻结选择与导出。

**Architecture:** 消费阶段 A 发布的目录/事实，统一查询服务生成短期查询快照，列表、汇总、翻页、导出和选择使用同一快照。剧按系列命名前两段归组，素材按实际投放引用计量；React 页面只访问本地 API。

**Tech Stack:** Python 3.14、FastAPI、SQLModel/PostgreSQL、Celery、React、TanStack Query/Router/Table、shadcn/ui、Bun、Playwright。

**Spec:** [已确认设计](../specs/2026-09-30-ads-management-reporting-design.md)；[总计划与共享合同](2026-09-30-ads-reporting-roadmap.md)。本文件所有步骤尚未执行。

## Global Constraints

- 阶段 A 通过后执行；根端拥有共享模型注册、迁移、路由注册、客户端生成与最终整合。
- 当前 BC 内按“版权方＋剧名”归组，系列下级继承此归属。
- 不区分原剧名和改名后的剧名，不建立人工剧关联流程或独立业务剧目录。
- 本模块不包含分成、返点、赠款、赔付及经营净收益指标。
- 所有 ROAS 按合计收入/合计消耗计算，不平均各行 ROAS。分母为零时为无值。
- 初始回补近 30 天；选择更早区间时发起历史回补，在回补完成前显示不完整覆盖。
- 页面搜索、筛选和轮询不直接调用 TikTok；只在显式刷新/回补入口调度 A 的同步任务。
- 使用现有黑色主按钮和 shadcn；不手写生成客户端，不运行会自动改写全仓的 `bun run lint`。
- 提交、测试环境与真实联调约束见总计划；以下命令默认在标注目录运行。

## Review Focus

- 搜索 `%`、`_`、反斜杠、多空白时按字面、多关键词 AND 查询，不能变成 SQL 通配或只筛当前页：B1。
- 翻页期间报表修正或系列改名，旧快照保持同一结果，全选不能混入新对象：B3。
- 同 VID 跨账户/广告使用，或同素材跨剧，不能凭名称/本地素材 ID 复制或吞掉金额：B2。
- 空报表、全零、缺分页、无权限及不支持的素材收入是不同状态：B2/B5。
- 导出排队后撤权、过期、CSV 公式前缀，以及切 BC 后旧请求迟到：B4/B5。

## File Structure 与交付合同

新文件按职责集中：

- `backend/app/modules/reporting/{schemas,filters,aggregation,trends,query_models,queries,selection,detail,preferences,exports,export_tasks,api}.py`：DTO、过滤、指标、趋势、查询快照、查询、选择、详情、个人偏好、导出及本地 API。
- `backend/tests/modules/reporting/{conftest,test_filters,test_aggregation,test_trends,test_queries,test_selection,test_detail,test_exports,test_api}.py`：消费 A 的事实/目录 fixtures，新增可复用的查询场景。
- `frontend/src/features/ads/{AdsWorkspace,AdsFilters,AdsTable,AdsSummary,AdsTrend,AdsDetails,ReportCoverageNotice}.tsx`、`useAdsQuery.ts`、`search.ts`：单一工作台，子文件各自负责视图部分。
- `frontend/src/routes/_layout/tenants.$tenantId.ads.tsx`，`frontend/tests/{ads-workspace,ads-reporting}.spec.ts`、`frontend/tests/utils/adsBoundary.ts`。
- 修改既有 `backend/app/api/main.py`、`backend/app/alembic/env.py`、`frontend/src/routes/_layout.tsx`、`frontend/playwright.config.ts`；生成 `frontend/openapi.json`、`frontend/src/client/`、`frontend/src/routeTree.gen.ts`。
- 新迁移 `backend/app/alembic/versions/reporting_queries.py`，revision `reporting_queries`，接 A 的 `ads_reporting_data`；实施前由根端核对 head，若已有他人迁移，只调整本轮新迁移父节点，禁止重写历史。

`schemas.py` 固定以下 Pydantic 合同，日期为 `date`、时点为带时区 `datetime`、金额为 `Decimal`（JSON 使用字符串）：

- `ReportingFilter`：`dimension: Literal['account','campaign','adgroup','ad','material','drama']`、`start_date/end_date`、`advertiser_ids/ids: tuple[str,...]`、`query`、`ad_types/operation_statuses/review_statuses/budget_modes`、`created_from/to`、`naming_status`、`min/max_spend`、`min/max_d0_roas`、`min/max_target_roas`、`sort_by`、`sort_direction`。端点和保存视图共用此类型。
- `MetricVector`：`currency`、`timezone`、`attribution`、`optimization_goal`（当前必须为 `None`）、`values: dict[str,Decimal|None]`、`availability: dict[str,Literal['AVAILABLE','MISSING','UNAVAILABLE','UNSUPPORTED','FAILED']]`；指标键沿用 A 的 canonical 名称（例如 `native_growth_ad_revenue_value_d0`），B2 按这些口径分桶后聚合。
- `ReportRow`：`row_key`、显示字段、`refs: tuple[EntityRef,...]`、`material_uses: tuple[MaterialUseRef,...]`、`metric_buckets`、`capabilities`、`directory_versions`、`membership_digest`；使用 A 的 EntityRef/MaterialUseRef，不重新定义。
- `QuerySnapshotPublic(snapshot_id, expires_at, filters, publication_versions)`；`AdsQueryPage(snapshot, items, total, summary, coverage, next_cursor)`。
- `SelectionRequest(snapshot_id, mode: Literal['EXPLICIT','ALL_MATCHING'], row_keys: tuple[str,...], excluded_row_keys: tuple[str,...])`；`FrozenSelection(selection_id, snapshot_id, refs, material_uses, membership_digest, expires_at)` 供 C 消费。

查询快照包含有序结果行及其必要指标、对象选择投影、已聚合趋势和版本，存 PostgreSQL；默认 15 分钟有效。快照过期返回 `409 query_snapshot_expired`，不能用新数据续旧游标。已导出/提交的任务持有自己的冻结副本，不随快照清理失去目标。整个新查询在短事务的一致读视图中生成快照，不跨浏览器请求保留数据库事务。趋势 GET 读取快照中的分桶趋势，不再查询较新的事实覆盖旧图。

## Task 1 (B1)：统一筛选、查询快照模型与读取隔离

**Files:** Create `schemas.py`、`filters.py`、`query_models.py`、`test_filters.py`；Modify A 已创建的 `tests/modules/reporting/conftest.py`、`app/alembic/env.py`；Create 上述迁移。路径均相对 `backend/` 的相应模块根。

**Interfaces:** 消费 A 的 AdObject/ReportFact/ReportCoverage。产生 `compile_filter(filters:ReportingFilter)->CompiledFilter`；`QuerySnapshot`、`QuerySnapshotRow`、`FrozenSelectionRecord`、`SavedReportView`、`ReportExport` 五个持久模型。所有快照保存 tenant/BC/actor、筛选摘要、报告与命名版本，快照行唯一键为 snapshot＋row_key。

- [x] **RED：** 写 `test_filter_literals_and_scope`，创建含名称 `MAX_% 甲` 和其他租户同名对象，断言 `compile_filter(ReportingFilter(dimension='campaign', start_date=date(2026,9,30), end_date=date(2026,9,30), query='MAX_% 甲')).keywords == ('MAX_%','甲')`，SQL 参数转义 `%/_/反斜杠`；通过 SQL 执行结果断言只命中当前 BC 的完整关键词对象。`conftest.py` 创建 `report_case` fixture，提供 `session/context/other_context/bc_id/headers/other_headers` 及 `seed_campaign(name,advertiser_id,spend,d0_revenue,status='ENABLE') -> EntityRef`，默认事实日期 2026-09-30、币种 USD、时区 UTC。
- [x] **运行 RED：** `uv run --frozen pytest tests/modules/reporting/test_filters.py -q`（backend）；预期新模型/筛选能力缺失失败，不以环境连接失败充当 RED。
- [x] **实现：** 使用现有 require_tenant 和 BCAccountAccess 限定账户，SQL 参数化；名称多词 AND，ID 集合精确 IN；日期倒置、未知排序字段、NaN/Infinity 阈值拒绝。索引覆盖快照所有者/到期、row_key及稳定序号；迁移不修改 A 的实体主键。
- [x] **GREEN：** 上述 pytest 通过，增加 `other_context` 读取快照 404、viewer 读取允许、非法数值 422 断言；专用库运行 `uv run --frozen alembic check` 无新增差异。
- [x] **提交：** 仅本任务文件，`git commit -m 'reports: define scoped filters and query snapshots'`；按总计划执行显式暂存和提交前检查。

## Task 2 (B2)：指标、六维聚合和观测趋势

**Files:** Create `aggregation.py`、`trends.py`、`test_aggregation.py`、`test_trends.py`。

**Interfaces:** `aggregate_metrics(rows:Sequence[MetricVector])->MetricVector` 只接受同口径桶；`build_dimension_rows(session,*,context,bc_id,filters:ReportingFilter)->tuple[ReportRow,...]`；`build_trend(session,*,context,bc_id,filters:ReportingFilter,grain:Literal['hour','day','observation'])->TrendPublic` 由 B3 在创建快照的一致读事务内调用。`TrendPublic` 定义在 schemas，含分桶 points、coverage、actual_interval_minutes、delta_reason。

- [x] **RED：** `test_roas_uses_summed_revenue` 用 USD/UTC/同归因/IAA 的两行 `(spend,d0_revenue)=(10,30),(90,90)`，断言 `aggregate_metrics(rows).values['d0_roas'] == Decimal('1.2')`；零消耗断言 `is None`。`test_material_usage_not_double_counted` 用同 VID 两广告实际 3/7，断言同账户素材合计 10、按两剧分别 3/7，多素材广告父金额不得复制；跨账户 VID 相同仍保留两份实际事实。
- [x] **运行 RED：** `uv run --frozen pytest tests/modules/reporting/test_aggregation.py tests/modules/reporting/test_trends.py -q`；预期聚合/趋势能力缺失失败。
- [x] **实现：** 六维分别选择已确认报告合同；剧用系列事实，素材用实际素材事实，缺失目录保留 ID 行；按币种/时区/归因/目标分桶，区分零与不可用。趋势从 A 的 ReportObservation 系列事实按最新名称投影重算；成员/grouping_revision 或日期变更则 `delta_reason='SCOPE_CHANGED'`/`'DATE_CHANGED'`、delta 为 None，不把迁组当效果变化；备注修改且筛选成员未变时仍可比较。
- [x] **GREEN：** 上述 pytest 通过，并测试暂停/删除金额仍在、同名不同版权方分开、空结果与缺页状态不同、混币种拆桶、负差值保留、缺失素材 D0 返回 UNSUPPORTED、无剧 ID 的外部系列正常归组；最终 reporting+adapter 专项回归 79 项通过。
- [x] **提交：** 实现及三轮修复已提交，最终独立复审记录于 `.superpowers/sdd/2026-09-30-ads-reporting-b-workspace/task-2-rereview-3.md`，Verdict APPROVED。

## Task 3 (B3)：快照查询、跨页选择、详情与本地 API

**Files:** Create `queries.py`、`selection.py`、`detail.py`、`api.py`、`test_queries.py`、`test_selection.py`、`test_detail.py`、`test_api.py`；Modify `app/api/main.py`。

**Interfaces:** `query_ads(session,*,context,bc_id,filters:ReportingFilter,snapshot_id:UUID|None=None,cursor:str|None=None,limit:int=50)->AdsQueryPage`；`freeze_selection(session,*,context,bc_id,request:SelectionRequest)->FrozenSelection`；`get_ad_detail(session,*,context,bc_id,ref:EntityRef)->AdDetailPublic`。详情消费 A 的 `ads.directory.locate`；C 用 selection_id 取得不可变目标。

- [x] **RED：** `test_all_matching_freezes_snapshot` 用 report_case 种 51 个匹配系列和 1 个不匹配系列，limit=50 取两页，新增第 52 个匹配系列后冻结旧快照，断言 `len(selection.refs)==51`、新 ID 不在其中、分页总数/汇总不变。`test_material_selection_keeps_usage_ids` 断言两个广告内素材 ID 都在 `selection.material_uses`，不能只返回素材库 VID。
- [x] **运行 RED：** `uv run --frozen pytest tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_detail.py tests/modules/reporting/test_api.py -q`。
- [x] **实现：** 快照存有序行与指标，游标绑定 snapshot/filter/actor/BC，稳定 row_key 处理并列排序，limit 1–100。账户/剧选择展开快照时的系列 refs；组/广告保留本身 refs；素材保留具体 MaterialUseRef。注册 `/api/tenants/{tenant_id}/ads`、`ads/{kind}/{id}`（必传 advertiser_id/bc_id）、`reports/trend` 和 POST `ad-selections`，只查本地数据；路由 tags 为 `["ads_reporting"]`，各 operation_id 唯一并使用 `ads_reporting-` 前缀。
- [x] **GREEN：** 测试无额外 TikTok 请求，伪造其他 BC 游标/快照/ID 404，15 分钟过期 409；改名后旧快照不变、新查询按新名归组；详情显示父停用/审核/排期原因，展示外部素材及历史操作但不要求本地素材映射。真实 Engine 跨请求 API→cursor→selection 回归通过。
- [x] **提交：** `c7ac4d1`、`79825c0`、`e04783e`；最终独立复审记录于 `.superpowers/sdd/2026-09-30-ads-reporting-b-workspace/task-3-rereview-1.md`，Verdict APPROVED。

## Task 4 (B4)：个人视图、异步导出与手动刷新

**Files:** Create `preferences.py`、`exports.py`、`export_tasks.py`、`test_exports.py`；Modify `api.py`、`test_api.py`、`app/jobs/celery_app.py`、`app/jobs/tasks.py`；新增导出对象存储前缀由既有存储客户端配置提供，不复制真实配置。

**Interfaces:** `save_view(session,*,context,bc_id,name:str,filters:ReportingFilter,columns:tuple[str,...])->SavedViewPublic`；`create_export(session,*,context,bc_id,snapshot_id:UUID,idempotency_key:str)->ExportPublic`；`run_export(export_id:UUID)->None`；`csv_safe_text(value:str)->str`。导出冻结快照内容副本，`ExportPublic` 含 id/status/coverage/expires_at，下载走鉴权 API，产物有效 24 小时。

显式刷新消费 A 的 `request_sync(session,*,context,request:SyncRequest)->UUID`；先使用既有 `accounts.routing.freeze_route(session,*,context,bc_id,connection_id=None)` 固定当前通道，服务端核对账户后构造 SyncRequest。HTTP 在 `/api/tenants/{tenant_id}` 下增加 POST/GET `ad-sync-runs`、GET `ad-sync-runs/{run_id}`、POST/GET `report-exports`、GET `report-exports/{export_id}` 与其 `/download`、POST/GET `report-views` 和 PATCH/DELETE `report-views/{view_id}`，均限定当前用户和 BC。

- [x] **RED：** `test_export_reuses_snapshot_and_checks_current_access` 种 51 行后创建导出，再插入新行，断言 CSV 仍 51 条；撤销 BC 访问后请求产物断言 403。参数化 `'=1+1','+SUM(A1)','-cmd','@x','\t=1'` 文本，断言 `csv_safe_text` 输出以单引号保护；金额类型的 `Decimal('-2')` 仍按数字 -2 导出。
- [x] **运行 RED：** `uv run --frozen pytest tests/modules/reporting/test_exports.py tests/modules/reporting/test_api.py -q`。
- [x] **实现：** 每个用户私有保存筛选/列，租户或 BC 变化不能泄漏偏好目标。CSV 含表头、币种/时区/覆盖/抓取时间；生成及下载复核权限，超过 24 小时返回过期，清理只操作专属导出产物。导出用本地后台 `ads-reporting` 队列，不调用 TikTok。显式刷新/超出历史覆盖的请求转交 A 的 `request_sync`，返回 run_id；GET 轮询只读本地，普通查询不隐式触发平台请求。
- [x] **GREEN：** 测试重复导出/刷新幂等、产物过期、个人视图不可互读、不支持的列拒绝、失败导出不暴露半文件；当前日期窗口一致，多币种分桶写入；导出/API 9 项、reporting 83 项通过。
- [x] **提交：** `61b20d7`、`940d53f`、`688d869`；最终独立复审 `task-4-rereview-2.md`/`task-4-rereview-3.md` 均 APPROVED。

## Task 5 (B5)：六维工作台、趋势、详情与状态体验

**Files:** Create 前述 `frontend/src/features/ads/`、路由及 `tests/utils/adsBoundary.ts`、`tests/ads-workspace.spec.ts`、`tests/ads-reporting.spec.ts`；Modify `src/routes/_layout.tsx`、`playwright.config.ts`；由脚本生成客户端及路由树。

**Interfaces:** `AdsWorkspace(): React.ReactElement`，`useAdsQuery(search:AdsSearch)` 返回 query/snapshot/selection state；`AdsSearch` 在 search.ts 定义并映射后端 ReportingFilter。`AdsDetails({ref,onClose})`、`AdsTrend({trend})`、`ReportCoverageNotice({coverage})`；C 在 AdsWorkspace 接入自己的 BulkActionBar。

- [x] **RED：** Playwright 覆盖外部广告六维切换、搜索、分页快照和 BC 切换迟到响应；测试使用合成 HTTP 边界，不依赖 build/material 本地 ID。
- [x] **运行 RED：** 环境未安装 Bun，改用 `npm exec playwright test ... --project=workspace` 收集并执行；未把未收集误报为通过。
- [x] **实现：** 已接入生成客户端、TanStack 查询键 tenant/BC/filter/snapshot、BC 切换取消旧请求、六维表格/趋势/详情/coverage、真实刷新/导出/保存/跨页冻结 API、外部广告和官方固定跳转门禁。后端 `reporting_write` action 保护所有持久化端点，viewer 只能读。
- [x] **GREEN：** 最终 Playwright 8 项、TypeScript、Vite build、Biome 15 路径、Ruff、compileall、diff-check 通过；target ROAS 未发布时显示目录未同步，COMPLETE_EMPTY 显示无数据，多桶指标不静默取第一桶。专用 PostgreSQL API pytest 因环境缺少 `DATABASE_URL` 未收集，已记录，不伪造通过。
- [x] **提交：** B5 实现及修复提交为 `3d239ee`、`a2a9f82`、`e9ab5dc`、`1aed8d5`、`c1b10e7`、`c926f61`、`172b470`、`cca9801`、`15bb0a2`、`15f9a9b`、`da0067c`、`2ebd88b`；最终独立复审 `task-5-rereview-permissions.md` APPROVED。

## Task 6 (B6)：阶段验收与查询容量证据

**Files:** Create `backend/tests/acceptance/test_reporting_workspace.py`、`docs/validation/2026-09-30-ads-reporting-workspace.md`（实施日如变化，用实际日期）；Modify `docs/implementation-progress.md`。

**Interfaces:** 通过真实 PostgreSQL 上的 FastAPI 和生成客户端合同验证 B1–B5，外部平台调用计数始终为零；不新增产品接口。

- [x] **RED：** 验收先暴露分页断言、真实 HTTP 合同缺失、冻结全选未挑战、容量回读不足和未知 BC 外键 500；均在 B6 范围内修正。
- [x] **运行检查：** 专用 PostgreSQL `tkada_ads_reporting_final_20260930_test` 上 acceptance 4 passed；容量实测 1,000 campaign/10,000 ad、20 SQL，未发现 N+1。全 reporting+acceptance 81 passed，另有 1 个既有 TestClient.delete 基线失败及 6 个未注入 TEST_REDIS_URL 的既有 setup errors。
- [x] **实现修正：** 补齐真实 TestClient 列表/游标/趋势/详情、TikTok gateway 0 调用哨兵、快照后新增成员不进入 ALL_MATCHING/export；查询入口在快照创建前校验 BC/usable grants，未知或未授权 BC 返回 404。
- [x] **GREEN：** Ruff、compileall、测试文件 ty、OpenAPI 15 条 operation 合同、`git diff --check` 通过；全 app ty 的 45 条诊断、Bun 缺失和 Redis 环境限制如实记录。容量数据是合成量测，不代表真实业务规模；未调用外部平台。
- [x] **提交：** `8c2e1b1`、`820550c`、`b6a3ef0`、`b094905`、`f7d0830`、`14bd4a5`；最终复审 `task-6-rereview-final-2.md` APPROVED。
