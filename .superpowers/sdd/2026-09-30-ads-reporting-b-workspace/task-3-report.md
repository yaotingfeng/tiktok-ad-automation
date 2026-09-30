# B3 任务报告：快照查询、跨页选择、详情与本地 API

## 实现

- 新增 `reporting/queries.py`：本地报表行在查询创建时冻结到 `QuerySnapshot`/`QuerySnapshotRow`，分页游标绑定 snapshot、筛选摘要、租户、操作者和 BC；limit 约束为 1–100，过期快照返回 409。
- 新增 `reporting/selection.py`：`EXPLICIT` 与 `ALL_MATCHING` 均只消费冻结快照；账户/剧展开系列引用，组/广告保留自身 `EntityRef`，素材保留具体 `MaterialUseRef`（含 `ad_material_id`）。冻结目标独立保存，供后续管理任务读取。
- 新增 `reporting/detail.py`：详情消费本地 `ads.directory.locate`，展示父级停用/审核/排期限制、外部素材使用和已观察操作记录。
- 新增 `reporting/api.py` 并注册到 `app/api/main.py`：`/tenants/{tenant_id}/ads`、`ads/{kind}/{remote_id}`、`reports/trend`、`ad-selections`。所有路由 tags 为 `ads_reporting`，operation ID 唯一且以 `ads_reporting-` 开头；不发起 TikTok/MCP 请求。

## TDD 与验证

RED 测试已先写入 `tests/modules/reporting/test_queries.py`、`test_selection.py`、`test_detail.py` 和 `test_api.py`。指定命令：

```text
uv run --frozen pytest tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_detail.py tests/modules/reporting/test_api.py -q
```

当前环境在收集 conftest 时被测试数据库守卫拒绝：`Tests require DATABASE_URL naming a dedicated PostgreSQL test database`；未将环境配置问题计为实现 RED。可执行的 GREEN 检查已通过：

随后使用仓库提供的隔离 `test.env`（未输出其中任何值）运行真实 PostgreSQL/Redis 测试环境；补充了一致读、账户选中时点、游标篡改、撤权、改名及跨请求 HTTP 回归。

```text
uv run --frozen ruff check app/modules/reporting/queries.py app/modules/reporting/selection.py app/modules/reporting/detail.py app/modules/reporting/api.py app/api/main.py tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_detail.py tests/modules/reporting/test_api.py
All checks passed!

uv run --frozen python -m compileall -q app/modules/reporting app/api/main.py tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_detail.py tests/modules/reporting/test_api.py

OpenAPI route check: reporting routes: ads_reporting-query_ads, ads_reporting-ad_detail, ads_reporting-report_trend, ads_reporting-freeze_ad_selection

隔离环境结果：`uv run --frozen pytest tests/modules/reporting -q` → **76 passed**。
```

## 提交

实现提交：`待更新`（`reports: add consistent queries and frozen cross-page selections`）。未执行真实 TikTok/MCP 请求、广告写入、部署或推送。
