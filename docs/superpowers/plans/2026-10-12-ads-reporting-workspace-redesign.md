# 广告报表工作台重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把广告报表页改造成投手的一站式工作台：官方指标名、固定列与清晰的表格、表头直出的批量操作、带趋势和完整配置的详情面板，以及正确的汇总口径。

**Architecture:** 后端在 `modules/reporting` 内新增指标字典、派生指标、部分合计规则和本地事实趋势接口，不新增任何 TikTok 调用；前端拆分 `features/ads` 为字典驱动的表格、工具栏、汇总趋势和详情面板模块，批量操作复用现有冻结选择与管理预览流程。

**Tech Stack:** FastAPI + SQLModel + PostgreSQL（uv、pytest）；React + TypeScript + TanStack Query/Router + shadcn/ui（Bun、Biome、Playwright）；新增 shadcn chart（recharts）。

**Spec:** `docs/superpowers/specs/2026-10-12-ads-reporting-workspace-redesign-design.md`；指标依据 `docs/contracts/tiktok-basic-report-metrics/`。

## Global Constraints

- 所有面向用户的指标名称取自 `docs/contracts/tiktok-basic-report-metrics/v1.3.zh.md`，不自创名称；表头省略"Native Growth"前缀，悬停说明与导出使用完整官方中文名。
- 平台字段 `dN` 显示为"第 N 天"；本地存储列 `revenue_d3/d7/d14/d30` 对应官方第 2/6/13/29 天，存储列名不改。
- 素材维度使用素材报表官方名称；平台不提供的列（第 2 天、第 29 天）在素材维度隐藏。
- 所有比率由合计原始量重算，不平均各行比率；分母为 0 时无值。
- 不新增 TikTok/MCP 调用；开发与测试期间不做真实广告写入。
- 不保留旧行为的兼容分支或死代码；本轮涉及的废弃代码（`BulkActionBar.tsx`、`AdsDetails.tsx`、`officialPlatformUrl`、`rowDrama`、`ID_COLUMN_BY_DISPLAY_COLUMN` 等）同步删除。
- 非平凡业务逻辑写中文注释说明规则与原因。
- 前端只用 shadcn/ui 组件，遵循 `docs/superpowers/plans/2026-09-10-workspace-visual-rollout.md` 视觉规范；TS/TSX 两空格缩进，Python 四空格。
- 租户、BC 范围在每条数据路径上保留；越权账户不得读到任何数据。
- Git：只 `git add` 明确路径，禁止 `git add .`；每个任务一次聚焦提交，提交信息结尾带 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`；不推送，不建分支。
- 后端测试需要命名含 `_test` 的专用 PostgreSQL 数据库（见 `backend/tests/database.py`）；没有时如实报告未执行，不宣称通过。

## Review Focus

1. **混合币种 BC**：汇总、表格、趋势都必须按币种分桶，不能把 USD 与其他币种相加；页面趋势一个币种一条线或分图。由 Task 5 的趋势测试和 Task 9 的前端用例覆盖。
2. **全部账户都未同步完**：部分合计规则下，所有成员都缺失时必须是"—"而不是 0。由 Task 3 测试 `test_summary_all_missing_stays_empty` 覆盖。
3. **只读成员**：工具栏不出现任何动作按钮，详情面板不出现单对象操作。由 Task 11、Task 12 前端用例覆盖。
4. **切换 BC 时已打开的详情和预览**：必须关闭，迟到的趋势/详情响应不能显示到新 BC 下。由 Task 12 用例"切 BC 关闭详情"覆盖。
5. **回收窗口跨时区**：所选结束日是某账户本地"今天"但另一个账户已过窗口，表头仍要标灰。由 Task 7 单元逻辑 `isRecoveryOpen` 的测试覆盖。

---

## 文件结构

**后端（`backend/app/modules/reporting/`）**

| 文件 | 操作 | 职责 |
| --- | --- | --- |
| `metric_catalog.py` | 新建 | 指标字典：键、各维度平台字段、官方中英文名、单位、可加性、适用维度 |
| `metrics.py` | 修改 | `_BASIC`/`_MATERIAL` 字段映射改由字典生成 |
| `schemas.py` | 修改 | 新增 `cpm`、`cpc` 键；`Availability` 增加 `PARTIAL`；趋势与字典响应模型 |
| `dimension_query.py` | 修改 | 派生 `cpm`/`cpc`；`aggregate_metrics(partial=True)`；`display` 增加 `secondary_status`、`account_status` |
| `queries.py` | 修改 | 汇总部分合计与不完整账户计数；列表响应裁剪账户行 `refs` |
| `filters.py` | 修改 | 排序字段包含 `cpm`、`cpc` |
| `exports.py` | 修改 | CSV 表头使用官方中文名 |
| `preferences.py` | 修改 | 视图允许列由字典生成 |
| `trend.py` | 新建 | 本地事实趋势聚合 |
| `detail.py` | 修改 | 操作记录改读 `ManagementTaskItem` |
| `api.py` | 修改 | 新增 `GET /ads/metric-catalog`、`GET /ads/trend` |

**前端（`frontend/src/features/ads/`）**

| 文件 | 操作 | 职责 |
| --- | --- | --- |
| `metricCatalog.ts` | 新建 | 读取字典、按维度生成列定义、格式化、回收窗口判断 |
| `statusLabel.ts` | 新建 | 主状态中文映射 |
| `columnPrefs.ts` | 新建 | 列显示与顺序的本地偏好 |
| `AdsNameCell.tsx` | 新建 | 名称、ID、上级路径、下钻按钮、不完整提示 |
| `AdsTable.tsx` | 重写 | 字典驱动的表格、固定列、排序 |
| `AdsTableToolbar.tsx` | 新建 | 未选中：列设置；选中：批量动作 |
| `AdsSummary.tsx` | 重写 | 7 张汇总卡片、部分合计提示、切换趋势指标 |
| `AdsTrendChart.tsx` | 新建 | 趋势折线（页面与详情共用） |
| `useAdsTrend.ts` | 新建 | 趋势接口查询 Hook |
| `details/AdsDetailSheet.tsx` | 新建 | 详情抽屉外壳与页签 |
| `details/OverviewTab.tsx`、`TrendTab.tsx`、`SettingsTab.tsx`、`MaterialsTab.tsx`、`HistoryTab.tsx` | 新建 | 各页签 |
| `details/configFields.ts` | 新建 | 配置字段中文标签与格式化 |
| `AdsWorkspace.tsx` | 修改 | 组装、默认账户维度、动作分发 |
| `search.ts` | 修改 | 默认值、删除废弃函数 |
| `BulkActionBar.tsx`、`AdsDetails.tsx` | 删除 | |
| `../ad-management/ManagementPreviewSheet.tsx` | 修改 | 接收预选动作、调整方式、修复 ENABLE |

---

## 阶段一：后端数据口径

### Task 1: 派生指标 cpm、cpc

**Files:**
- Modify: `backend/app/modules/reporting/schemas.py:19-40`
- Modify: `backend/app/modules/reporting/dimension_query.py:50-87`、`473-507`
- Test: `backend/tests/modules/reporting/test_dimension_query.py`

**Interfaces:**
- Produces: `CANONICAL_METRIC_KEYS` 包含 `"cpm"`、`"cpc"`；`metric_vector()` 返回的 `values` 含 `cpm = spend / impressions × 1000`、`cpc = spend / clicks`；排序字段自动支持（`METRIC_SORT_FIELDS = CANONICAL_METRIC_KEYS`）。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/modules/reporting/test_dimension_query.py` 末尾追加：

```python
def test_cpm_and_cpc_are_recomputed_from_totals(session, sync_seed):
    _seed(session, sync_seed)
    camp = {
        r.display["remote_id"]: r
        for r in build_rows(
            session,
            context=sync_seed.context,
            bc_id="bc-a",
            filters=_filters("campaign"),
        )
    }
    values = camp["c1"].metric_buckets[0].values
    # ad_row 每行固定 impressions=10、clicks=1；c1 合计消耗 15、展示 20、点击 2。
    assert values["cpm"] == Decimal("15.00") / Decimal("20") * 1000
    assert values["cpc"] == Decimal("15.00") / Decimal("2")


def test_cpm_sorts_desc(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=_filters("ad", sort_by="cpm", sort_direction="desc"),
    )
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_dimension_query.py -k "cpm or cpc" -v`
Expected: FAIL，`KeyError: 'cpm'` 或排序字段 422 校验失败。

- [ ] **Step 3: 实现**

`schemas.py` 的 `CANONICAL_METRIC_KEYS` 集合中，在 `"ctr",` 后加入：

```python
        "cpm",
        "cpc",
```

`dimension_query.py` 的 `metric_vector` 中，把派生指标列表改为：

```python
    for name, numerator, denominator, scale in [
        *(
            (f"roas_{d}", f"revenue_{d}", "spend", 1)
            for d in ("d0", "d3", "d7", "d14", "d30", "total")
        ),
        ("ctr", "clicks", "impressions", 1),
        # 千次展示成本与平均点击成本只由同口径合计重算，不平均各行的比率。
        ("cpm", "spend", "impressions", 1000),
        ("cpc", "spend", "clicks", 1),
        ("cost_per_ad_impression_event", "spend", "ad_impression_events", 1),
    ]:
        a, b = values.get(numerator), values.get(denominator)
        values[name] = a / b * scale if a is not None and b else None
```

（循环体内 `states[name] = ...` 部分保持不变。）

`_sort_value` 中金额类集合加入 `"cpm"`、`"cpc"`，因为它们以币种计价：

```python
            "cost_per_ad_impression_event",
            "cpm",
            "cpc",
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting/test_dimension_query.py tests/modules/reporting/test_queries.py tests/modules/reporting/test_exports.py -v`
Expected: 全部 PASS（导出测试此时仍用旧表头，不受影响）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/schemas.py backend/app/modules/reporting/dimension_query.py backend/tests/modules/reporting/test_dimension_query.py
git commit -m "reporting: 报表新增千次展示成本与平均点击成本"
```

### Task 2: 指标字典、字典接口、导出与视图列

**Files:**
- Create: `backend/app/modules/reporting/metric_catalog.py`
- Modify: `backend/app/modules/reporting/metrics.py:13-35`
- Modify: `backend/app/modules/reporting/schemas.py`（新增 `MetricDefinitionPublic`）
- Modify: `backend/app/modules/reporting/exports.py:271-350`
- Modify: `backend/app/modules/reporting/preferences.py:20-44`
- Modify: `backend/app/modules/reporting/api.py`（新增路由）
- Test: `backend/tests/modules/reporting/test_metric_catalog.py`（新建）、`test_exports.py`

**Interfaces:**
- Produces:
  - `METRIC_CATALOG: tuple[MetricDefinition, ...]`，`MetricDefinition(key, unit, additive, basic_field, material_field, basic_name_zh, material_name_zh, basic_name_en, material_name_en)`；`unit ∈ {"money","count","ratio","multiple"}`；`basic_field`/`material_field` 为平台字段名或 `None`（派生或不支持）。
  - `metric_name_zh(key: str, dimension: str) -> str`：返回该维度的官方中文名（含 Native Growth 前缀的完整名）。
  - `GET /api/tenants/{tenant_id}/ads/metric-catalog` → `tuple[MetricDefinitionPublic, ...]`，operationId `ads_reporting-metric_catalog`。
  - `MetricDefinitionPublic` 字段与 `MetricDefinition` 相同，外加 `basic_label`、`material_label`（表头用，去掉 "Native Growth " 前缀）。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/modules/reporting/test_metric_catalog.py`：

```python
from pathlib import Path

from app.modules.reporting import metrics
from app.modules.reporting.metric_catalog import METRIC_CATALOG, metric_name_zh
from app.modules.reporting.schemas import CANONICAL_METRIC_KEYS

DOC = (
    Path(__file__).resolve().parents[4]
    / "docs/contracts/tiktok-basic-report-metrics/v1.3.zh.md"
).read_text()


def test_catalog_covers_every_canonical_metric():
    assert {item.key for item in METRIC_CATALOG} == set(CANONICAL_METRIC_KEYS)


def test_platform_fields_and_names_exist_in_official_doc():
    # 字典里的每个平台字段及其中文名都必须能在官方原文中找到，防止自创名称。
    for item in METRIC_CATALOG:
        for field, name in (
            (item.basic_field, item.basic_name_zh),
            (item.material_field, item.material_name_zh),
        ):
            if field is not None:
                assert field in DOC, field
                assert name in DOC, name


def test_metrics_module_reads_catalog_fields():
    assert metrics._BASIC["revenue_d3"] == "native_growth_ad_revenue_value_d2"
    assert metrics._MATERIAL["ad_impression_events"] == "onsite_total_ad_impression"
    assert "revenue_d3" not in metrics._MATERIAL


def test_names_follow_official_day_numbering():
    assert metric_name_zh("roas_d3", "campaign") == "Native Growth 第 2 天广告收益 ROAS"
    assert metric_name_zh("revenue_total", "material") == "广告曝光总价值"
```

在 `test_exports.py` 末尾追加。`ReportExport` 定义在 `app/modules/reporting/query_models.py:120`（继承 `FrozenQueryScope`），构造方式照抄同文件 `test_csv_keeps_decimal_amount_numeric` 里的写法，把 `frozen_rows` 设为空列表、过滤条件里的维度设为 `campaign`，然后断言：

```python
def test_csv_header_uses_official_names():
    export = _export_like_existing_test(frozen_rows=[], dimension="campaign")
    header = _csv_rows(export).decode().splitlines()[0].split(",")
    assert "消耗" in header
    assert "Native Growth 第 2 天广告收益 ROAS" in header
    assert "roas_d3" not in header
```

`_export_like_existing_test` 是从现有用例中抽出的本地构造函数（同文件内新增，并让原用例改用它），避免两处重复构造。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_metric_catalog.py tests/modules/reporting/test_exports.py -v`
Expected: FAIL，`ModuleNotFoundError: app.modules.reporting.metric_catalog`。

- [ ] **Step 3: 实现字典**

新建 `backend/app/modules/reporting/metric_catalog.py`：

```python
"""报表指标字典。

名称与平台字段一律取自 docs/contracts/tiktok-basic-report-metrics/v1.3.zh.md
（官方 v1.3 基础报表支持指标）。本地存储列 revenue_d3/d7/d14/d30 对应官方
第 2/6/13/29 天（dN = 点击后 (N+1)×24 小时），展示必须用官方天数。
素材报表是另一套字段和名称，不能套用基础报表的叫法。
"""

from dataclasses import dataclass
from typing import Literal

Unit = Literal["money", "count", "ratio", "multiple"]
_PREFIX = "Native Growth "


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    unit: Unit
    additive: bool
    basic_field: str | None
    material_field: str | None
    basic_name_zh: str
    material_name_zh: str | None
    basic_name_en: str
    material_name_en: str | None


def _m(key, unit, additive, basic, material, zh, mzh, en, men):
    return MetricDefinition(key, unit, additive, basic, material, zh, mzh, en, men)


METRIC_CATALOG: tuple[MetricDefinition, ...] = (
    _m("spend", "money", True, "spend", "spend", "消耗", "消耗", "Cost", "Cost"),
    _m("revenue_total", "money", True, "native_growth_total_ad_impression_value", "onsite_total_ad_impression_value", "Native Growth 广告展示总价值", "广告曝光总价值", "Native Growth total ad impression value", "Total ad impression value"),
    _m("roas_total", "multiple", False, "native_growth_ad_revenue_roas", "onsite_ad_impression_ad_revenue_roas", "Native Growth 广告收益 ROAS", "广告收入 ROAS (TikTok)", "Native Growth ad revenue ROAS", "Ad revenue ROAS (TikTok)"),
    _m("revenue_d0", "money", True, "native_growth_ad_revenue_value_d0", "onsite_ad_impression_ad_revenue_day0", "Native Growth 第 0 天 广告收益金额", "第 0 天广告收入", "Native Growth day 0 ad revenue value", "Day 0 ad revenue"),
    _m("roas_d0", "multiple", False, "native_growth_ad_revenue_roas_d0", "onsite_ad_impression_ad_revenue_roas_day0", "Native Growth 第 0 天广告收益 ROAS", "第 0 天广告收入 ROAS", "Native Growth day 0 ad revenue ROAS", "Day 0 ad revenue ROAS"),
    _m("revenue_d3", "money", True, "native_growth_ad_revenue_value_d2", None, "Native Growth 第 2 天广告收益金额", None, "Native Growth day 2 ad revenue value", None),
    _m("roas_d3", "multiple", False, "native_growth_ad_revenue_roas_d2", None, "Native Growth 第 2 天广告收益 ROAS", None, "Native Growth day 2 ad revenue ROAS", None),
    _m("revenue_d7", "money", True, "native_growth_ad_revenue_value_d6", "onsite_ad_impression_ad_revenue_day6", "Native Growth 第 6 天广告收益金额", "第 6 天广告收入", "Native Growth day 6 ad revenue value", "Day 6 ad revenue"),
    _m("roas_d7", "multiple", False, "native_growth_ad_revenue_roas_d6", "onsite_ad_impression_ad_revenue_roas_day6", "Native Growth 第 6 天广告收益 ROAS", "第 6 天广告收入 ROAS", "Native Growth day 6 ad revenue ROAS", "Day 6 ad revenue ROAS"),
    _m("revenue_d14", "money", True, "native_growth_ad_revenue_value_d13", "onsite_ad_impression_ad_revenue_day13", "Native Growth 第 13 天广告收益金额", "第 13 天广告收入", "Native Growth day 13 ad revenue value", "Day 13 ad revenue"),
    _m("roas_d14", "multiple", False, "native_growth_ad_revenue_roas_d13", "onsite_ad_impression_ad_revenue_roas_day13", "Native Growth 第 13 天广告收益 ROAS", "第 13 天广告收入 ROAS", "Native Growth day 13 ad revenue ROAS", "Day 13 ad revenue ROAS"),
    _m("revenue_d30", "money", True, "native_growth_ad_revenue_value_d29", None, "Native Growth 第 29 天广告收益金额", None, "Native Growth day 29 ad revenue value", None),
    _m("roas_d30", "multiple", False, "native_growth_ad_revenue_roas_d29", None, "Native Growth 第 29 天广告收入 ROAS", None, "Native Growth day 29 ad revenue ROAS", None),
    _m("impressions", "count", True, "impressions", "impressions", "展示量", "展示量", "Impressions", "Impressions"),
    _m("clicks", "count", True, "clicks", "clicks", "点击量（目标页面）", "点击量（目标页面）", "Clicks (destination)", "Clicks (destination)"),
    _m("ctr", "ratio", False, "ctr", "ctr", "点击率（目标页面）", "点击率（目标页面）", "CTR (destination)", "CTR (destination)"),
    _m("cpm", "money", False, "cpm", "cpm", "千次展示成本 (CPM)", "千次展示成本 (CPM)", "CPM", "CPM"),
    _m("cpc", "money", False, "cpc", "cpc", "平均点击成本（目标页面）", "平均点击成本（目标页面）", "CPC (destination)", "CPC (destination)"),
    _m("ad_impression_events", "count", True, "native_growth_total_ad_impression_event_count", "onsite_total_ad_impression", "Native Growth 广告展示事件总数", "广告曝光事件总数", "Native Growth total ad impression events", "Total ad impression events"),
    _m("cost_per_ad_impression_event", "money", False, "native_growth_cost_per_ad_impression_event", "onsite_cost_per_ad_impression", "Native Growth 单次广告展示事件成本", "广告曝光事件平均成本", "Native Growth cost per ad impression event", "Cost per ad impression event"),
)
_BY_KEY = {item.key: item for item in METRIC_CATALOG}
# 存储列：直接写入事实表的可加原始量；比率类在查询时重算，不落库。
STORED_KEYS = (
    "spend",
    "impressions",
    "clicks",
    "ad_impression_events",
    "revenue_d0",
    "revenue_d3",
    "revenue_d7",
    "revenue_d14",
    "revenue_d30",
    "revenue_total",
)


def metric_name_zh(key: str, dimension: str) -> str:
    item = _BY_KEY[key]
    if dimension == "material":
        return item.material_name_zh or item.basic_name_zh
    return item.basic_name_zh


def table_label(name: str) -> str:
    """表头省略 Native Growth 前缀；完整名放在悬停说明和导出。"""
    return name.removeprefix(_PREFIX)


def basic_fields() -> dict[str, str]:
    return {key: _BY_KEY[key].basic_field for key in STORED_KEYS}  # type: ignore[misc]


def material_fields() -> dict[str, str]:
    return {
        key: field
        for key in STORED_KEYS
        if (field := _BY_KEY[key].material_field) is not None
    }
```

注意 `roas_d30` 的中文名按官方原文为"第 29 天广告收入 ROAS"（官方 d20 以后改称"收入"），已照抄；不要"统一"成"收益"。

运行 `uv run ruff format app/modules/reporting/metric_catalog.py` 让长行按项目风格换行。

- [ ] **Step 4: metrics.py 改用字典**

把 `metrics.py` 中 `_BASIC = {...}` 和 `_MATERIAL = {...}` 两个字面量替换为：

```python
from app.modules.reporting.metric_catalog import basic_fields, material_fields

_BASIC = basic_fields()
_MATERIAL = material_fields()
```

（`COLUMNS = tuple(_BASIC)` 及其后代码不变。）

- [ ] **Step 5: 字典接口**

`schemas.py` 末尾追加：

```python
class MetricDefinitionPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    unit: Literal["money", "count", "ratio", "multiple"]
    additive: bool
    basic_field: str | None
    material_field: str | None
    basic_name_zh: str
    material_name_zh: str | None
    basic_name_en: str
    material_name_en: str | None
    basic_label: str
    material_label: str | None
```

`api.py` 在 `ad_detail_route` 之前加入（必须在 `/ads/{kind}/{remote_id}` 之前注册，否则会被当成 kind）：

```python
@router.get(
    "/ads/metric-catalog",
    response_model=tuple[MetricDefinitionPublic, ...],
    name="metric_catalog",
)
def metric_catalog_route(
    tenant_id: UUID, session: SessionDep, user: CurrentUser
) -> tuple[MetricDefinitionPublic, ...]:
    _context(session, user, tenant_id)
    return tuple(
        MetricDefinitionPublic(
            **asdict(item),
            basic_label=table_label(item.basic_name_zh),
            material_label=table_label(item.material_name_zh)
            if item.material_name_zh
            else None,
        )
        for item in METRIC_CATALOG
    )
```

并补充导入：`from dataclasses import asdict`、`from app.modules.reporting.metric_catalog import METRIC_CATALOG, table_label`、`MetricDefinitionPublic`。

- [ ] **Step 6: 导出表头用官方名**

`exports.py` 的 `_csv_rows` 中，保留内部 `fields` 作为字典键，写表头时换成官方名。`metric_fields` 改为字典顺序：

```python
    dimension = str((export.filters or {}).get("dimension") or "campaign")
    metric_fields = [item.key for item in METRIC_CATALOG]
    fields = [
        "row_key", "name", "advertiser_id", "currency", "timezone",
        "coverage", "fetched_at", *metric_fields,
    ]
```

素材维度不支持的指标（`material_field is None` 且是存储列）从 `metric_fields` 中去掉。`writer.writeheader()` 替换为：

```python
    headers = {key: key for key in fields} | {
        key: metric_name_zh(key, dimension) for key in metric_fields
    }
    writer.writerow(headers)
```

维度从 `export.filters["dimension"]` 读取（`FrozenQueryScope.filters`，`query_models.py:47`）。

- [ ] **Step 7: 视图允许列由字典生成**

`preferences.py` 的 `SUPPORTED_VIEW_COLUMNS` 改为：

```python
# 列键与报表行一致：显示字段 + 指标字典键；与工作台、导出共用同一口径。
SUPPORTED_VIEW_COLUMNS = frozenset(
    {"row_key", "name", "advertiser_id", "status", "review_status", "ad_type",
     "currency", "timezone", "coverage", "budget", "target_roas", "optimization_goal",
     "balance"}
    | {item.key for item in METRIC_CATALOG}
)
```

在 `test_metric_catalog.py` 追加：

```python
def test_saved_view_accepts_catalog_keys():
    from app.modules.reporting.preferences import validate_columns

    assert validate_columns(("name", "roas_d0", "cpm")) == ("name", "roas_d0", "cpm")
```

- [ ] **Step 8: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting -v && bash scripts/lint.sh`
Expected: 全部 PASS。若旧测试断言了旧导出表头或旧视图列，按新口径更新断言（不要保留旧键）。

- [ ] **Step 9: 提交**

```bash
git add backend/app/modules/reporting/metric_catalog.py backend/app/modules/reporting/metrics.py backend/app/modules/reporting/schemas.py backend/app/modules/reporting/exports.py backend/app/modules/reporting/preferences.py backend/app/modules/reporting/api.py backend/tests/modules/reporting/test_metric_catalog.py backend/tests/modules/reporting/test_exports.py
git commit -m "reporting: 指标名称统一为 TikTok 官方口径并提供指标字典"
```

### Task 3: 汇总部分合计

**Files:**
- Modify: `backend/app/modules/reporting/schemas.py:18`
- Modify: `backend/app/modules/reporting/dimension_query.py:89-111`、`780-783`
- Modify: `backend/app/modules/reporting/queries.py:95-107`
- Test: `backend/tests/modules/reporting/test_queries.py`

**Interfaces:**
- Produces:
  - `Availability` 增加 `"PARTIAL"`。
  - `aggregate_metrics(vectors, *, partial: bool = False) -> MetricVector`：`partial=True` 时，值为 `None` 的成员不参与求和；只要有一个成员有值，结果为合计且标记 `PARTIAL`（全部成员都有值时仍为 `AVAILABLE`）；全部缺失则为 `None`/`MISSING`。全部成员 `UNSUPPORTED` 时为 `UNSUPPORTED`。派生比率由合计重算，状态跟随分子或分母中较差的一个。
  - `_summary(rows)` 返回 `{"row_count", "buckets", "incomplete_advertiser_count", "incomplete_dates"}`。

- [ ] **Step 1: 写失败测试**

在 `test_queries.py` 追加（复用该文件已有的导入；缺什么补什么）：

```python
from decimal import Decimal

from app.modules.reporting.dimension_query import aggregate_metrics, metric_vector


def _vec(spend, revenue):
    return metric_vector(
        {"spend": spend, "revenue_total": revenue, "impressions": None,
         "clicks": None, "ad_impression_events": None, "revenue_d0": None,
         "revenue_d3": None, "revenue_d7": None, "revenue_d14": None,
         "revenue_d30": None},
        currency="USD",
        timezone="UTC",
    )


def test_summary_sums_available_members_and_marks_partial():
    total = aggregate_metrics(
        [_vec(Decimal("10"), Decimal("5")), _vec(None, None)], partial=True
    )
    assert total.values["spend"] == Decimal("10")
    assert total.availability["spend"] == "PARTIAL"
    assert total.values["roas_total"] == Decimal("0.5")
    assert total.availability["roas_total"] == "PARTIAL"


def test_summary_all_missing_stays_empty():
    total = aggregate_metrics([_vec(None, None), _vec(None, None)], partial=True)
    assert total.values["spend"] is None
    assert total.availability["spend"] == "MISSING"


def test_strict_aggregate_unchanged_for_rows():
    total = aggregate_metrics([_vec(Decimal("10"), Decimal("5")), _vec(None, None)])
    assert total.values["spend"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_queries.py -k "summary or strict" -v`
Expected: FAIL，`TypeError: aggregate_metrics() got an unexpected keyword argument 'partial'`。

- [ ] **Step 3: 实现**

`schemas.py:18`：

```python
Availability = Literal[
    "AVAILABLE", "PARTIAL", "MISSING", "UNAVAILABLE", "UNSUPPORTED", "FAILED"
]
```

`dimension_query.py` 中 `metric_vector` 增加可选参数 `partial: set[str] = frozenset()`，在 `states` 计算原始列时，`name in partial` 且值非空的标记为 `"PARTIAL"`；派生循环中状态改为：

```python
        states[name] = (
            "UNSUPPORTED"
            if numerator in unsupported or denominator in unsupported
            else "MISSING"
            if values[name] is None
            else "PARTIAL"
            if numerator in partial or denominator in partial
            else "AVAILABLE"
        )
```

`aggregate_metrics` 改为：

```python
def aggregate_metrics(
    vectors: Sequence[MetricVector], *, partial: bool = False
) -> MetricVector:
    """合计同币种向量。

    行内合计（同一对象多天、剧下多系列）用严格模式：任一成员缺失则合计为空，
    避免把不完整的对象当成完整数字。汇总卡片用 partial 模式：缺失成员不参与
    求和、也不当成 0，合计结果标 PARTIAL，由页面提示"N 个账户数据不完整"。
    """
    if not vectors or len({v.currency for v in vectors}) != 1:
        raise ValueError("incompatible currencies or empty metrics")
    values: dict[str, Decimal | None] = {}
    unsupported: set[str] = set()
    partial_names: set[str] = set()
    for name in METRIC_COLUMNS:
        states = [v.availability.get(name) for v in vectors]
        if all(s == "UNSUPPORTED" for s in states):
            unsupported.add(name)
            values[name] = None
            continue
        present = [
            cast(Decimal, v.values.get(name))
            for v in vectors
            if v.values.get(name) is not None
            and v.availability.get(name) in ("AVAILABLE", "PARTIAL")
        ]
        complete = len(present) == len(vectors) and "PARTIAL" not in states
        if complete:
            values[name] = sum(present, Decimal(0))
        elif partial and present:
            values[name] = sum(present, Decimal(0))
            partial_names.add(name)
        else:
            values[name] = None
            if "UNSUPPORTED" in states:
                unsupported.add(name)
    zones = {v.timezone for v in vectors}
    return metric_vector(
        values,
        currency=vectors[0].currency,
        timezone=next(iter(zones)) if len(zones) == 1 else "MIXED",
        unsupported=unsupported,
        partial=partial_names,
    )
```

（注意：原实现中"任一成员 UNSUPPORTED 即整列 UNSUPPORTED"；新实现改为"全部成员不支持才是 UNSUPPORTED，部分不支持按缺失处理"，与设计 7.3 一致。）

`queries.py` 的 `_summary`：

```python
def _summary(rows: tuple[ReportRow, ...]) -> dict[str, Any]:
    """汇总按币种分桶；缺覆盖的成员不参与合计并单独计数，不当成 0。"""
    buckets: dict[tuple[Any, ...], list[MetricVector]] = {}
    incomplete_advertisers: set[str] = set()
    incomplete_dates: set[str] = set()
    for row in rows:
        if row.coverage.get("status") not in (None, "COMPLETE", "COMPLETE_EMPTY"):
            incomplete_advertisers.add(str(row.display.get("advertiser_id") or ""))
            incomplete_dates.update(
                cast(list[str], row.coverage.get("stale_dates") or [])
            )
        for vector in row.metric_buckets:
            buckets.setdefault(bucket_key(vector), []).append(vector)
    totals = [
        aggregate_metrics(vectors, partial=True).model_dump(mode="json")
        for vectors in buckets.values()
    ]
    totals.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return {
        "row_count": len(rows),
        "buckets": totals,
        "incomplete_advertiser_count": len(incomplete_advertisers - {""}),
        "incomplete_dates": sorted(incomplete_dates),
    }
```

`dimension_query.py:781` 的行内分组合计保持 `aggregate_metrics(values)`（严格模式），不改。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting tests/acceptance/test_reporting_workspace.py -v`
Expected: PASS。若既有测试断言"任一成员 UNSUPPORTED 则整列不支持"，按新规则改断言并在测试名中体现"部分不支持按缺失处理"。

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/schemas.py backend/app/modules/reporting/dimension_query.py backend/app/modules/reporting/queries.py backend/tests/modules/reporting/test_queries.py
git commit -m "reporting: 汇总卡片合计已同步部分并标注不完整账户"
```

### Task 4: 账户行口径回归、响应裁剪与状态字段

**Files:**
- Modify: `backend/app/modules/reporting/queries.py:74-92`（`_row_public`）
- Modify: `backend/app/modules/reporting/dimension_query.py:690-702`（`display.update`）
- Test: `backend/tests/modules/reporting/test_dimension_query.py`、`test_queries.py`、`test_selection.py`

**Interfaces:**
- Consumes: `_row_public(row: QuerySnapshotRow) -> ReportRow`。
- Produces:
  - `_row_public(row: QuerySnapshotRow, *, for_listing: bool = False) -> ReportRow`：`for_listing=True` 且 `row.dimension == "account"` 时 `refs` 为空元组。`EntityKind` 只有 `campaign/adgroup/ad/creative`，没有 account，所以不构造账户引用；前端用 `display.account_id` 识别账户。快照表 `QuerySnapshotRow.refs` 不变；`selection.py:132` 继续调用 `_row_public(row)`（默认 `False`），冻结选择照旧展开全部系列。
  - `display` 新增 `secondary_status`（对象行，来自 `AdObject.delivery_status`）与 `account_status`（账户行，来自 `AdvertiserAccount.remote_status`）。

- [ ] **Step 1: 写失败测试**

`test_dimension_query.py` 追加：

```python
def test_account_row_equals_sum_of_its_campaigns(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camps = [
        r
        for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("campaign"))
        if r.display["advertiser_id"] == "acc-1"
    ]
    acct = next(
        r
        for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("account"))
        if r.display["remote_id"] == "acc-1"
    )
    for key in ("spend", "impressions", "clicks", "revenue_d0", "revenue_total"):
        assert acct.metric_buckets[0].values[key] == sum(
            c.metric_buckets[0].values[key] for c in camps
        )
    assert "account_status" in acct.display
```

在 `test_queries.py` 追加一个经过分页查询入口（该文件现有用例使用的函数）的测试：账户维度第一页每行 `refs == ()`；随后用同一快照调用 `freeze_selection`（参照 `test_selection.py` 中现有的账户维度用例），断言冻结出的 `refs` 包含该账户下全部系列。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_dimension_query.py::test_account_row_equals_sum_of_its_campaigns tests/modules/reporting/test_queries.py -v`
Expected: 合计断言 PASS（口径已正确），`account_status` 与 `refs` 断言 FAIL。

- [ ] **Step 3: 实现**

`dimension_query.py` 的 `display.update(...)` 增加：

```python
            secondary_status=entity.delivery_status if entity else None,
```

`if kind == "account":` 分支里增加：

```python
            display["account_status"] = accounts[adv].remote_status
```

`queries.py` 的 `_row_public` 改为：

```python
def _row_public(row: QuerySnapshotRow, *, for_listing: bool = False) -> ReportRow:
    display = dict(row.display)
    coverage = display.pop("__coverage__", {})
    # 快照内的账户行保留其下全部系列引用，供冻结选择展开；列表响应不返回这些引用，
    # 避免每个账户行带回上百条。冻结选择调用时 for_listing 保持 False。
    refs = () if for_listing and row.dimension == "account" else row.refs
    return ReportRow.model_validate(
        {
            "row_key": row.row_key,
            "display": display,
            "refs": refs,
            ...  # 其余字段保持原样
        }
    )
```

`_read_page` 中改为 `items = tuple(_row_public(row, for_listing=True) for row in rows)`。原注释里关于 B1 迁移兼容的说明保留。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting tests/modules/ad_management -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/queries.py backend/app/modules/reporting/dimension_query.py backend/tests/modules/reporting/test_dimension_query.py backend/tests/modules/reporting/test_queries.py
git commit -m "reporting: 账户行裁剪列表引用并补充投放状态与账户状态"
```

### Task 5: 趋势接口

**Files:**
- Create: `backend/app/modules/reporting/trend.py`
- Modify: `backend/app/modules/reporting/schemas.py`（`TrendPoint`、`TrendPublic`）
- Modify: `backend/app/modules/reporting/api.py`
- Test: `backend/tests/modules/reporting/test_trend.py`（新建）

**Interfaces:**
- Consumes: `authorized_grants(session, context=, bc_id=, advertiser_ids=)`（`filters.py:167`）、`metric_vector`、`aggregate_metrics`、`bucket_key`。
- Produces:
  - `build_trend(session, *, context, bc_id, level, advertiser_ids, ids, start, end, granularity) -> TrendPublic`。
  - `TrendPoint(at: str, buckets: tuple[MetricVector, ...], coverage: str)`；`at` 为日粒度 `YYYY-MM-DD`，小时粒度为账户本地 `YYYY-MM-DDTHH:00`（混合时区时为 UTC ISO 字符串并在 `TrendPublic.timezone` 标 `MIXED`）。
  - `TrendPublic(level, granularity, timezone, points: tuple[TrendPoint, ...])`。
  - `GET /api/tenants/{tenant_id}/ads/trend?bc_id&level&advertiser_ids&ids&start_date&end_date&granularity`，operationId `ads_reporting-ad_trend`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/modules/reporting/test_trend.py`：

```python
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.trend import build_trend
from tests.modules.reporting.sync_conftest import ad_row

D1 = date(2026, 10, 4)
D2 = date(2026, 10, 5)


def _seed(session, seed):
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong")},
        start=D1,
        end=D2,
        rows=[
            ad_row("a1", D1, campaign="c1", spend="4.00", d0="1.00", total="2.00"),
            ad_row("a1", D2, campaign="c1", spend="6.00", d0="3.00", total="3.00"),
            ad_row("a2", D2, campaign="c2", spend="1.00", d0="1.00", total="1.00"),
        ],
    )


def _trend(session, seed, **kw):
    params = dict(
        context=seed.context, bc_id="bc-a", level="campaign", advertiser_ids=(),
        ids=(), start=D1, end=D2, granularity="day",
    ) | kw
    return build_trend(session, **params)


def test_day_trend_for_one_campaign(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, ids=("c1",))
    assert [p.at for p in trend.points] == ["2026-10-04", "2026-10-05"]
    assert [p.buckets[0].values["spend"] for p in trend.points] == [
        Decimal("4.00"), Decimal("6.00"),
    ]
    assert trend.points[1].buckets[0].values["roas_total"] == Decimal("0.5")


def test_account_level_sums_campaigns_per_day(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, level="account", advertiser_ids=("acc-1",))
    assert trend.points[1].buckets[0].values["spend"] == Decimal("7.00")


def test_ad_level_uses_ad_facts(session, sync_seed):
    _seed(session, sync_seed)
    trend = _trend(session, sync_seed, level="ad", ids=("a2",))
    assert [p.buckets[0].values["spend"] if p.buckets else None for p in trend.points] == [
        None, Decimal("1.00"),
    ]


def test_day_range_limit(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(session, sync_seed, start=D2 - timedelta(days=90), end=D2)
    assert err.value.status_code == 422


def test_unauthorized_advertiser_is_forbidden(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(session, sync_seed, bc_id="bc-b", level="account", advertiser_ids=("acc-1",))
    assert err.value.status_code == 403


def test_hour_only_for_account_and_campaign(session, sync_seed):
    with pytest.raises(HTTPException) as err:
        _trend(session, sync_seed, level="ad", granularity="hour", start=D2, end=D2)
    assert err.value.status_code == 422
```

（`sync_seed` 中 acc-1 只属于 bc-a，因此在 bc-b 下请求 acc-1 应为 403。`publish_ad_day` 会同时写入系列日事实，`test_dimension_query.py` 的账户合计用例已证明这一点。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_trend.py -v`
Expected: FAIL，`ModuleNotFoundError: app.modules.reporting.trend`。

- [ ] **Step 3: 模型**

`schemas.py` 追加：

```python
TrendLevel = Literal["account", "campaign", "adgroup", "ad", "material"]


class TrendPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: str
    buckets: tuple[MetricVector, ...] = ()
    coverage: Literal["COMPLETE", "INCOMPLETE", "MISSING"] = "MISSING"


class TrendPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: TrendLevel
    granularity: Literal["day", "hour"]
    timezone: str
    points: tuple[TrendPoint, ...] = ()
```

- [ ] **Step 4: 实现 build_trend**

新建 `backend/app/modules/reporting/trend.py`：

```python
"""本地事实趋势。

页面趋势与详情趋势共用：按层级和对象 ID 从本地日/小时事实聚合，不调用
TikTok。2026-10-05 曾为首屏性能把趋势移出快照，因此趋势单独按需读取。
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select as sql_select
from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.modules.reporting.dimension_query import (
    aggregate_metrics,
    bucket_key,
    metric_vector,
)
from app.modules.reporting.filters import authorized_grants
from app.modules.reporting.schemas import TrendPoint, TrendPublic
from app.modules.reporting.store_models import (
    METRIC_COLUMNS,
    AdReportDay,
    CampaignReportDay,
    CampaignReportHour,
    MaterialReportDay,
    ReportSyncState,
)

MAX_DAY_SPAN = 90
MAX_HOUR_SPAN = 2
MAX_IDS = 200
_DAY_MODELS = {
    "account": (CampaignReportDay, None, "ad_day"),
    "campaign": (CampaignReportDay, "campaign_id", "ad_day"),
    "adgroup": (AdReportDay, "adgroup_id", "ad_day"),
    "ad": (AdReportDay, "ad_id", "ad_day"),
    "material": (MaterialReportDay, "main_material_id", "material_day"),
}


def _invalid(detail: str) -> HTTPException:
    return HTTPException(422, detail=detail)


def build_trend(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    level: str,
    advertiser_ids: tuple[str, ...],
    ids: tuple[str, ...],
    start: date,
    end: date,
    granularity: str,
) -> TrendPublic:
    if end < start:
        raise _invalid("trend_range_invalid")
    span = (end - start).days + 1
    if len(ids) > MAX_IDS:
        raise _invalid("trend_ids_too_many")
    if granularity == "hour":
        if level not in ("account", "campaign"):
            raise _invalid("trend_hour_level_unsupported")
        if span > MAX_HOUR_SPAN:
            raise _invalid("trend_range_too_long")
    elif span > MAX_DAY_SPAN:
        raise _invalid("trend_range_too_long")
    grants = {
        g.advertiser_id
        for g in authorized_grants(
            session, context=context, bc_id=bc_id, advertiser_ids=advertiser_ids or None
        )
    }
    # 显式请求的账户必须全部在当前 BC 授权范围内；不在范围直接拒绝，不返回部分数据。
    if advertiser_ids and not set(advertiser_ids) <= grants:
        raise HTTPException(403, detail="trend_scope_forbidden")
    model, id_col, state_kind = _DAY_MODELS[level]
    if granularity == "hour":
        model, time_col = CampaignReportHour, CampaignReportHour.stat_hour
    else:
        time_col = model.stat_date
    stmt = (
        sql_select(
            col(time_col).label("at"),
            col(model.currency),
            func.min(col(model.timezone)).label("zone_min"),
            func.max(col(model.timezone)).label("zone_max"),
            *[func.sum(col(getattr(model, name))).label(name) for name in METRIC_COLUMNS],
            *[
                func.count(col(getattr(model, name))).label(f"n_{name}")
                for name in METRIC_COLUMNS
            ],
            func.count().label("n_rows"),
        )
        .where(
            model.tenant_id == context.tenant_id,
            col(model.advertiser_id).in_(grants),
        )
        .group_by(col(time_col), col(model.currency))
        .order_by(col(time_col))
    )
    if granularity == "hour":
        # 小时事实以 UTC 时刻存储；按日期窗口前后各放宽一天，再由账户本地日过滤。
        stmt = stmt.where(
            col(time_col) >= start - timedelta(days=1),
            col(time_col) < end + timedelta(days=2),
        )
    else:
        stmt = stmt.where(col(time_col) >= start, col(time_col) <= end)
    if ids:
        stmt = stmt.where(col(getattr(model, id_col or "campaign_id")).in_(ids))
    rows = session.connection().execute(stmt).mappings().all()
    states = session.exec(
        select(ReportSyncState).where(
            ReportSyncState.tenant_id == context.tenant_id,
            col(ReportSyncState.advertiser_id).in_(grants),
            ReportSyncState.report_kind == state_kind,
            ReportSyncState.stat_date >= start,
            ReportSyncState.stat_date <= end,
        )
    ).all()
    complete_days = defaultdict(set)
    for state in states:
        if state.status == "COMPLETE":
            complete_days[state.stat_date].add(state.advertiser_id)
    by_point: dict[str, list[Any]] = defaultdict(list)
    zones: set[str] = set()
    for row in rows:
        values = {
            # 某天任一事实行该列为 NULL 时整点该列为空，与行内严格合计口径一致。
            name: row[name] if row[f"n_{name}"] == row["n_rows"] else None
            for name in METRIC_COLUMNS
        }
        zone = row["zone_min"] if row["zone_min"] == row["zone_max"] else "MIXED"
        zones.add(zone)
        at = (
            row["at"].isoformat()
            if granularity == "day"
            else row["at"].isoformat(timespec="minutes")
        )
        by_point[at].append(metric_vector(values, currency=row["currency"], timezone=zone))
    if granularity == "day":
        keys = [(start + timedelta(days=i)).isoformat() for i in range(span)]
    else:
        keys = sorted(by_point)
    points = []
    for key in keys:
        vectors = by_point.get(key, [])
        buckets: dict[tuple[str], list] = defaultdict(list)
        for vector in vectors:
            buckets[bucket_key(vector)].append(vector)
        day = date.fromisoformat(key[:10])
        done = complete_days.get(day, set())
        coverage = (
            "COMPLETE" if done >= grants else "INCOMPLETE" if done or vectors else "MISSING"
        )
        points.append(
            TrendPoint(
                at=key,
                buckets=tuple(aggregate_metrics(v) for _, v in sorted(buckets.items())),
                coverage=coverage,
            )
        )
    return TrendPublic(
        level=level,
        granularity=granularity,
        timezone=next(iter(zones)) if len(zones) == 1 else ("MIXED" if zones else "UTC"),
        points=tuple(points),
    )
```

核对点（实现时逐项确认，不要猜）：
- `ReportSyncState.report_kind` 的真实取值：`grep -n "report_kind=" -r app/modules/reporting | head`。按实际值替换 `"ad_day"` / `"material_day"`。
- `MaterialReportDay` 的 `stat_date`、`main_material_id` 列名（`store_models.py:107-125`）。
- `authorized_grants` 对 `advertiser_ids=None` 的含义（应为全部授权账户），以及它在 BC 不属于租户时是否已抛 403/404；若已抛，保留其行为。

- [ ] **Step 5: 路由**

`api.py` 在 `ad_detail_route` 之前加入：

```python
@router.get("/ads/trend", response_model=TrendPublic, name="ad_trend")
def ad_trend_route(
    tenant_id: UUID,
    bc_id: Annotated[str, Query(min_length=1, max_length=128)],
    level: TrendLevel,
    start_date: date,
    end_date: date,
    session: SessionDep,
    user: CurrentUser,
    granularity: Literal["day", "hour"] = "day",
    advertiser_ids: Annotated[str | None, Query(max_length=20000)] = None,
    ids: Annotated[str | None, Query(max_length=20000)] = None,
) -> TrendPublic:
    context = _context(session, user, tenant_id)
    split = lambda value: tuple(v for v in (value or "").split(",") if v)  # noqa: E731
    return build_trend(
        session,
        context=context,
        bc_id=bc_id,
        level=level,
        advertiser_ids=split(advertiser_ids),
        ids=split(ids),
        start=start_date,
        end=end_date,
        granularity=granularity,
    )
```

- [ ] **Step 6: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting/test_trend.py tests/modules/reporting/test_api.py -v && bash scripts/lint.sh`
Expected: PASS（`test_reporting_routes_have_stable_operation_ids` 覆盖新路由的 operationId）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/reporting/trend.py backend/app/modules/reporting/schemas.py backend/app/modules/reporting/api.py backend/tests/modules/reporting/test_trend.py
git commit -m "reporting: 新增按层级与对象读取本地事实的趋势接口"
```

### Task 6: 详情操作记录改读管理任务

**Files:**
- Modify: `backend/app/modules/reporting/detail.py:143-160`
- Test: `backend/tests/modules/reporting/test_detail.py`

**Interfaces:**
- Produces: `AdDetailPublic.operation_history` 每项为 `{"task_id", "created_at", "field", "mode", "original_value", "final_value", "execution_result", "reason"}`，按任务创建时间倒序，只包含同 BC、同账户、同对象的任务项。

- [ ] **Step 1: 写失败测试**

`test_detail.py` 追加：

```python
def test_detail_history_reads_management_task_items(report_case):
    from uuid import uuid4

    from app.modules.ad_management.models import (
        ManagementPreview,
        ManagementTask,
        ManagementTaskItem,
    )
    from app.modules.reporting.detail import get_ad_detail

    session = report_case.session
    ref = report_case.seed_campaign("Provider-Drama", "report-account", 1, 2)
    preview = ManagementPreview(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        selection_id=uuid4(),
        digest="d" * 64,
        mutation={"field": "status", "mode": "set", "value": "DISABLE"},
    )
    session.add(preview)
    session.flush()
    task = ManagementTask(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        preview_id=preview.id,
        idempotency_key=uuid4(),
        digest="d" * 64,
    )
    session.add(task)
    session.flush()
    session.add(
        ManagementTaskItem(
            tenant_id=report_case.context.tenant_id,
            task_id=task.id,
            preview_item_id=uuid4(),
            preview_id=preview.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            },
            original_value="ENABLE",
            final_value="DISABLE",
            execution_result="SUCCEEDED",
        )
    )
    session.flush()
    detail = get_ad_detail(
        session, context=report_case.context, bc_id=report_case.bc_id, ref=ref
    )
    assert detail.operation_history[0]["field"] == "status"
    assert detail.operation_history[0]["final_value"] == "DISABLE"
```

（`ManagementPreview`/`ManagementTask` 的必填字段以 `models.py:26-180` 为准；缺默认值的字段按模型补齐，`execution_result` 取值以模型注释中的枚举为准。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/reporting/test_detail.py -v`
Expected: FAIL，`IndexError`（历史为空）。

- [ ] **Step 3: 实现**

把 `detail.py` 中 `history_rows = ...` 到 `history = tuple(...)` 整段替换为：

```python
    # 本工具的管理操作只记录在管理任务项里；按冻结的对象引用匹配，动作取自预览。
    history_rows = session.exec(
        select(ManagementTaskItem, ManagementTask, ManagementPreview)
        .join(ManagementTask, col(ManagementTask.id) == col(ManagementTaskItem.task_id))
        .join(ManagementPreview, col(ManagementPreview.id) == col(ManagementTask.preview_id))
        .where(
            ManagementTaskItem.tenant_id == context.tenant_id,
            ManagementTask.bc_id == bc_id,
            ManagementTaskItem.ref["advertiser_id"].astext == ref.advertiser_id,
            ManagementTaskItem.ref["remote_id"].astext == ref.remote_id,
        )
        .order_by(col(ManagementTask.created_at).desc())
        .limit(100)
    ).all()
    history = tuple(
        {
            "task_id": str(task.id),
            "created_at": task.created_at.isoformat(),
            "field": preview.mutation.get("field"),
            "mode": preview.mutation.get("mode"),
            "original_value": item.original_value,
            "final_value": item.final_value,
            "execution_result": item.execution_result,
            "reason": item.reason,
        }
        for item, task, preview in history_rows
    )
```

删除不再使用的 `AuditEvent` 导入，补充 `ManagementTaskItem, ManagementTask, ManagementPreview` 导入。若 `ref` 列的 SQLAlchemy 类型不支持 `["key"].astext`，改用 `ManagementTaskItem.ref.op("->>")("remote_id")`。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests/modules/reporting/test_detail.py -v && bash scripts/lint.sh`
Expected: PASS。

- [ ] **Step 5: 重新生成前端客户端并提交**

Run: `bash scripts/generate-client.sh`，然后 `cd frontend && bun run build`。
Expected: 构建通过；`frontend/src/client` 出现 `metricCatalog`、`adTrend` 方法和 `PARTIAL` 类型。

```bash
git add backend/app/modules/reporting/detail.py backend/tests/modules/reporting/test_detail.py frontend/openapi.json frontend/src/client
git commit -m "reporting: 详情操作记录改读管理任务，并更新前端客户端"
```

---

## 阶段二：表格与汇总

### Task 7: 前端指标字典、状态映射与回收窗口

**Files:**
- Create: `frontend/src/features/ads/metricCatalog.ts`
- Create: `frontend/src/features/ads/statusLabel.ts`
- Test: `frontend/tests/ads-metric-catalog.spec.ts`（新建，Playwright 中直接调用纯函数）

**Interfaces:**
- Consumes: `AdsReportingService.metricCatalog({ path: { tenant_id } })` → `MetricDefinitionPublic[]`。
- Produces:
  - `type ColumnDef = { key: string; label: string; fullName: string; unit: "money"|"count"|"ratio"|"multiple"; recoveryDay: number | null }`
  - `columnsFor(catalog: MetricDefinitionPublic[], dimension: AdsDimension): ColumnDef[]`：素材维度去掉 `material_field === null` 的列；表头用 `basic_label`/`material_label`，`fullName` 为完整官方名。
  - `recoveryDayOf(key: string): number | null`：`*_d0 → 0`、`*_d3 → 2`、`*_d7 → 6`、`*_d14 → 13`、`*_d30 → 29`，其他为 `null`。
  - `isRecoveryOpen(day: number, endDate: string, timezones: string[], now?: Date): boolean`：任一账户本地今天 − 结束日 < day + 1 即为 `true`。
  - `formatMetric(value: string | null | undefined, unit): string`：`ratio` 显示百分比两位，`multiple` 两位小数，`money` 两位，`count` 千分位整数，空为 `"—"`。
  - `useMetricCatalog(): UseQueryResult<MetricDefinitionPublic[]>`（`staleTime: Infinity`）。
  - `statusLabel(display: Record<string, string | null>): { label: string; tone: "default"|"secondary"|"destructive"|"outline"; raw: string }`。

- [ ] **Step 1: 写失败测试**

新建 `frontend/tests/ads-metric-catalog.spec.ts`：

```ts
import { expect, test } from "@playwright/test"
import {
  formatMetric,
  isRecoveryOpen,
  recoveryDayOf,
} from "../src/features/ads/metricCatalog"
import { statusLabel } from "../src/features/ads/statusLabel"

test("官方天数映射", () => {
  expect(recoveryDayOf("roas_d3")).toBe(2)
  expect(recoveryDayOf("revenue_d30")).toBe(29)
  expect(recoveryDayOf("spend")).toBeNull()
})

test("回收窗口按最晚的账户本地日判断", () => {
  const now = new Date("2026-10-12T01:00:00Z")
  // UTC 已是 10-12，洛杉矶仍是 10-11：结束日 10-10 时第 2 天窗口对洛杉矶账户未结束。
  expect(isRecoveryOpen(2, "2026-10-10", ["UTC"], now)).toBe(false)
  expect(isRecoveryOpen(2, "2026-10-10", ["UTC", "America/Los_Angeles"], now)).toBe(true)
})

test("指标格式化", () => {
  expect(formatMetric("0.1234", "ratio")).toBe("12.34%")
  expect(formatMetric("1234", "count")).toBe("1,234")
  expect(formatMetric(null, "money")).toBe("—")
})

test("状态中文映射", () => {
  expect(statusLabel({ operation_status: "DISABLE" }).label).toBe("已暂停")
  expect(
    statusLabel({ operation_status: "ENABLE", secondary_status: "CAMPAIGN_STATUS_ENABLE" }).label,
  ).toBe("投放中")
  expect(statusLabel({ operation_status: "SOMETHING_NEW" }).label).toBe("SOMETHING_NEW")
})
```

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ads-metric-catalog.spec.ts`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现 metricCatalog.ts**

```ts
import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type MetricDefinitionPublic } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"
import type { AdsDimension } from "./search"

export type MetricUnit = MetricDefinitionPublic["unit"]
export type ColumnDef = {
  key: string
  label: string
  fullName: string
  unit: MetricUnit
  recoveryDay: number | null
}

// 本地存储列 dN 与官方天数的对应：revenue_d3 是官方"第 2 天"，依此类推。
const RECOVERY_DAYS: Record<string, number> = {
  d0: 0,
  d3: 2,
  d7: 6,
  d14: 13,
  d30: 29,
}

export function recoveryDayOf(key: string): number | null {
  const match = /_(d\d+)$/.exec(key)
  return match ? (RECOVERY_DAYS[match[1]] ?? null) : null
}

function localToday(timezone: string, now: Date): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now)
}

/** 第 N 天指标在点击后 (N+1)×24 小时内仍会增长；任一账户未满窗口即视为未结束。 */
export function isRecoveryOpen(
  day: number,
  endDate: string,
  timezones: string[],
  now: Date = new Date(),
): boolean {
  const end = Date.parse(`${endDate}T00:00:00Z`)
  return timezones.some((zone) => {
    const today = Date.parse(`${localToday(zone, now)}T00:00:00Z`)
    return (today - end) / 86_400_000 < day + 1
  })
}

export function formatMetric(
  value: string | null | undefined,
  unit: MetricUnit,
): string {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  if (!Number.isFinite(number)) return "—"
  if (unit === "ratio") return `${(number * 100).toFixed(2)}%`
  if (unit === "count") return Math.round(number).toLocaleString("en-US")
  return number.toFixed(2)
}

export function columnsFor(
  catalog: MetricDefinitionPublic[],
  dimension: AdsDimension,
): ColumnDef[] {
  const material = dimension === "material"
  return catalog
    .filter((item) => !material || item.material_field !== null)
    .map((item) => ({
      key: item.key,
      label: (material ? item.material_label : null) ?? item.basic_label,
      fullName: (material ? item.material_name_zh : null) ?? item.basic_name_zh,
      unit: item.unit,
      recoveryDay: recoveryDayOf(item.key),
    }))
}

export function useMetricCatalog() {
  const { tenantId } = useTenantScope()
  return useQuery({
    queryKey: ["tenant", tenantId, "ads-metric-catalog"],
    enabled: Boolean(tenantId),
    staleTime: Number.POSITIVE_INFINITY,
    queryFn: async () =>
      (
        await AdsReportingService.metricCatalog({
          path: { tenant_id: tenantId! },
        })
      ).data,
  })
}
```

注意：素材维度后端字典里 `ratio`/派生列的 `material_field` 也有值（`ctr`、`cpm`、`cpc`），只有第 2 天、第 29 天两列为空，正好按"平台不提供则隐藏"处理。生成的客户端方法名以 `frontend/src/client/sdk.gen.ts` 为准（operationId `ads_reporting-metric_catalog` 通常生成 `metricCatalog`）。

- [ ] **Step 4: 实现 statusLabel.ts**

```ts
type Tone = "default" | "secondary" | "destructive" | "outline"

// 主状态优先看审核与投放（secondary_status），再看开关（operation_status）；
// 未登记的枚举原样显示，不隐藏，避免平台新增状态时投手看不到。
const SECONDARY: Array<[RegExp, string, Tone]> = [
  [/DELETE/, "已删除", "outline"],
  [/AUDIT_DENY|REJECT|NOT_APPROVE/, "未通过", "destructive"],
  [/AUDIT|REVIEW/, "审核中", "secondary"],
  [/BUDGET_EXCEED|OFFLINE_BUDGET|BALANCE/, "预算不足", "destructive"],
  [/NOT_START|NO_SCHEDULE|TIME_NO_REACH|DONE/, "不在排期", "secondary"],
  [/DISABLE/, "已暂停", "outline"],
  [/ENABLE|DELIVERY_OK|DELIVERING/, "投放中", "default"],
]
const OPERATION: Record<string, [string, Tone]> = {
  ENABLE: ["投放中", "default"],
  DISABLE: ["已暂停", "outline"],
  DELETE: ["已删除", "outline"],
}
const ACCOUNT: Record<string, [string, Tone]> = {
  STATUS_ENABLE: ["正常", "default"],
  STATUS_DISABLE: ["已停用", "outline"],
  STATUS_PENDING_CONFIRM: ["审核中", "secondary"],
  STATUS_CONFIRM_FAIL: ["未通过", "destructive"],
  STATUS_LIMIT: ["受限", "destructive"],
}

export function statusLabel(display: Record<string, string | null | undefined>) {
  const account = display.account_status
  if (account) {
    const [label, tone] = ACCOUNT[account] ?? [account, "outline" as Tone]
    return { label, tone, raw: account }
  }
  const secondary = display.secondary_status ?? ""
  const operation = display.operation_status ?? ""
  const raw = [operation, secondary, display.review_status]
    .filter(Boolean)
    .join(" / ")
  if (operation === "DISABLE") return { label: "已暂停", tone: "outline" as Tone, raw }
  for (const [pattern, label, tone] of SECONDARY)
    if (secondary && pattern.test(secondary)) return { label, tone, raw }
  if (operation) {
    const [label, tone] = OPERATION[operation] ?? [operation, "outline" as Tone]
    return { label, tone, raw }
  }
  return { label: "未知", tone: "outline" as Tone, raw: raw || "—" }
}
```

账户状态枚举以 `AdvertiserAccount.remote_status` 实际写入值为准（`grep -rn "remote_status=" backend/app | head`），按实际值补全 `ACCOUNT`。

- [ ] **Step 5: 运行确认通过**

Run: `cd frontend && bunx playwright test tests/ads-metric-catalog.spec.ts && bun run lint`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/features/ads/metricCatalog.ts frontend/src/features/ads/statusLabel.ts frontend/tests/ads-metric-catalog.spec.ts
git commit -m "frontend: 报表指标字典、中文状态与回收窗口判断"
```

### Task 8: 表格重构

**Files:**
- Create: `frontend/src/features/ads/AdsNameCell.tsx`
- Rewrite: `frontend/src/features/ads/AdsTable.tsx`
- Modify: `frontend/src/features/ads/search.ts`
- Modify: `frontend/src/features/ads/AdsWorkspace.tsx`
- Modify: `frontend/tests/utils/adsBoundary.ts`（模拟 `metric-catalog` 接口）
- Test: `frontend/tests/ads-workspace.spec.ts`

**Interfaces:**
- Consumes: `columnsFor`、`formatMetric`、`isRecoveryOpen`、`statusLabel`（Task 7）。
- Produces:
  - `AdsTable` props：`{ rows, dimension, columns: ColumnDef[], endDate: string, selected, allMatching, total, onToggle, onToggleAllOnPage, onOpen(row), onDrill(row), onFilterParent(kind, id, advertiserId), nextCursor, hasPrevious, onNext, onPrevious, sortBy, sortDirection, onSort }`。
  - `AdsNameCell` props：`{ row, dimension, onOpen, onDrill, onFilterParent }`。
  - `search.ts`：`defaultAdsSearch()` 返回 `dimension: "account"`、`sort_by: "spend"`、`sort_direction: "desc"`；导出 `nameKeyFor(dimension)`、`parentPath(row, dimension): Array<{ kind: "account"|"campaign"|"adgroup"; id: string; name: string }>`。

- [ ] **Step 1: 更新测试夹具与失败测试**

`adsBoundary.ts` 的路由处理中，在 `/ads` 之前增加：

```ts
    if (path.endsWith(`/tenants/${TENANT}/ads/metric-catalog`))
      return route.fulfill({ headers, json: METRIC_CATALOG })
```

`METRIC_CATALOG` 直接从后端字典复制：先运行
`cd backend && uv run python -c "import json;from app.modules.reporting.api import metric_catalog_route" ` 不可行（需要会话），因此改用
`uv run python -c "import json,dataclasses;from app.modules.reporting.metric_catalog import METRIC_CATALOG,table_label;print(json.dumps([dataclasses.asdict(i)|{'basic_label':table_label(i.basic_name_zh),'material_label':table_label(i.material_name_zh) if i.material_name_zh else None} for i in METRIC_CATALOG],ensure_ascii=False,indent=2))"`，
把输出写入 `frontend/tests/fixtures/metricCatalog.json`，在 `adsBoundary.ts` 中 `import METRIC_CATALOG from "../fixtures/metricCatalog.json"`。

把 `ads-workspace.spec.ts` 第一个用例里的旧列名列表替换为：

```ts
    for (const column of [
      "消耗",
      "广告展示总价值",
      "广告收益 ROAS",
      "第 0 天 广告收益金额",
      "第 0 天广告收益 ROAS",
      "第 2 天广告收益 ROAS",
      "第 6 天广告收益 ROAS",
      "第 13 天广告收益 ROAS",
      "第 29 天广告收入 ROAS",
      "展示量",
      "点击量（目标页面）",
      "点击率（目标页面）",
      "千次展示成本 (CPM)",
      "平均点击成本（目标页面）",
      "广告展示事件总数",
      "单次广告展示事件成本",
    ]) {
```

在该文件追加：

```ts
  test("默认账户维度，横向滚动后勾选框和名称列仍固定", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("tab", { name: "账户", selected: true })).toBeVisible()
    const container = page.locator('[data-slot="table-container"]')
    const nameCell = page.locator('[data-sticky="name"]').first()
    const before = await nameCell.boundingBox()
    await container.evaluate((el) => el.scrollTo({ left: 800 }))
    const after = await nameCell.boundingBox()
    expect(after?.x).toBe(before?.x)
    await expect(page.locator('[data-sticky="select"]').first()).toBeInViewport()
  })

  test("不再出现详情与 TikTok 后台列", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("columnheader", { name: "详情" })).toHaveCount(0)
    await expect(page.getByRole("link", { name: "TikTok 后台" })).toHaveCount(0)
    await expect(page.getByRole("columnheader", { name: "数据状态" })).toHaveCount(0)
  })

  test("素材维度隐藏平台不提供的天数列", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "素材" }).click()
    await expect(page.getByRole("columnheader", { name: /第 2 天/ })).toHaveCount(0)
    await expect(page.getByRole("columnheader", { name: /广告曝光总价值/ })).toBeVisible()
  })
```

原有用例里依赖"默认系列维度"、旧列名、"查看详情"按钮的地方，按新行为改写（先点"系列"页签再断言）。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts`
Expected: FAIL（列名、默认维度、固定列）。

- [ ] **Step 3: search.ts**

- `defaultAdsSearch()`：`dimension: "account"`、`sort_by: "spend"`、`sort_direction: "desc"`。
- 删除 `rowDrama`、`ID_COLUMN_BY_DISPLAY_COLUMN`、`officialPlatformUrl`、`rowMetric`、`availabilityLabel`、`availabilityReason`、`formatMetric`（改用 `metricCatalog.formatMetric`）、`targetRoasLabel`、`targetRoasReason`、`rowBudget`、`childDimensionForColumn`、`rowHierarchyId` 中不再被引用的部分。删除前用 `grep -rn "<函数名>" frontend/src` 确认没有其他调用方。
- 新增：

```ts
const NAME_KEY: Record<AdsDimension, string> = {
  account: "account_name",
  campaign: "campaign_name",
  adgroup: "adgroup_name",
  ad: "ad_name",
  material: "material_name",
  drama: "drama_name",
}
const CHILD: Partial<Record<AdsDimension, AdsDimension>> = {
  account: "campaign",
  campaign: "adgroup",
  adgroup: "ad",
  ad: "material",
}
export const nameKeyFor = (dimension: AdsDimension) => NAME_KEY[dimension]
export const childDimension = (dimension: AdsDimension) => CHILD[dimension] ?? null

export type ParentLink = {
  kind: "account" | "campaign" | "adgroup"
  id: string
  name: string
}
/** 名称下方的上级路径；只列出比当前维度更高的层级。 */
export function parentPath(row: ReportRow, dimension: AdsDimension): ParentLink[] {
  const display = row.display ?? {}
  const order = ["account", "campaign", "adgroup"] as const
  const depth = { account: 0, campaign: 1, adgroup: 2, ad: 3, material: 4, drama: 0 }[dimension]
  return order
    .slice(0, dimension === "drama" || dimension === "material" ? 0 : depth)
    .map((kind) => ({
      kind,
      id: display[`${kind}_id`] ?? "",
      name: display[`${kind}_name`] ?? display[`${kind}_id`] ?? "",
    }))
    .filter((link) => link.id)
}
/** 指标值与可用状态；多币种时由调用方逐桶渲染。 */
export function metricOf(row: ReportRow, key: string, bucket = 0) {
  const vector = row.metric_buckets?.[bucket]
  const value = vector?.values?.[key] ?? null
  return {
    value,
    availability: vector?.availability?.[key] ?? (value === null ? "MISSING" : "AVAILABLE"),
    currency: vector?.currency ?? "",
  }
}
```

- [ ] **Step 4: AdsNameCell.tsx**

```tsx
import { ChevronRight, CircleAlert } from "lucide-react"
import type { ReportRow } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import {
  type AdsDimension,
  childDimension,
  nameKeyFor,
  type ParentLink,
  parentPath,
} from "./search"

export function AdsNameCell({
  row,
  dimension,
  onOpen,
  onDrill,
  onFilterParent,
}: {
  row: ReportRow
  dimension: AdsDimension
  onOpen: (row: ReportRow) => void
  onDrill: (row: ReportRow) => void
  onFilterParent: (link: ParentLink, advertiserId: string) => void
}) {
  const display = row.display ?? {}
  const name = display[nameKeyFor(dimension)] ?? display.name ?? row.row_key
  const id = display.remote_id
  const path = parentPath(row, dimension)
  const advertiserId = display.account_id ?? display.advertiser_id ?? ""
  const incomplete =
    row.coverage?.status !== undefined && row.coverage.status !== "COMPLETE"
  const staleDates = (row.coverage?.stale_dates as string[] | undefined) ?? []
  return (
    <div className="flex min-w-0 items-start gap-1">
      <div className="min-w-0 flex-1">
        <button
          type="button"
          className="line-clamp-2 text-left text-sm font-medium hover:underline"
          title={name}
          onClick={() => onOpen(row)}
        >
          {name}
        </button>
        {id && <div className="text-[11px] text-muted-foreground">{id}</div>}
        {path.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-1 text-[11px] text-muted-foreground">
            {path.map((link, index) => (
              <span key={link.kind} className="inline-flex items-center gap-1">
                {index > 0 && <span aria-hidden="true">›</span>}
                <button
                  type="button"
                  className="max-w-40 truncate hover:underline"
                  title={link.name}
                  onClick={() => onFilterParent(link, advertiserId)}
                >
                  {link.name}
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
      {incomplete && (
        <Tooltip>
          <TooltipTrigger asChild>
            <CircleAlert
              className="mt-0.5 size-3.5 shrink-0 text-amber-500"
              aria-label="数据不完整"
            />
          </TooltipTrigger>
          <TooltipContent>
            {staleDates.length
              ? `数据不完整，缺失日期：${staleDates.join("、")}`
              : "该行数据尚未同步完整"}
          </TooltipContent>
        </Tooltip>
      )}
      {childDimension(dimension) && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-6 shrink-0"
          aria-label={`查看 ${name} 的下级`}
          onClick={() => onDrill(row)}
        >
          <ChevronRight className="size-4" aria-hidden="true" />
        </Button>
      )}
    </div>
  )
}
```

- [ ] **Step 5: 重写 AdsTable.tsx**

要点（完整实现，不保留旧列）：
- 列顺序：勾选框 → 名称（`AdsNameCell`）→ 状态 → 配置列（账户：余额；系列：预算；广告组：预算、目标 ROAS、优化目标；广告：目标 ROAS、优化目标）→ `columns`（指标）。
- 固定列：勾选框 `<TableHead/TableCell data-sticky="select" className="sticky left-0 z-20 w-10 bg-background group-hover:bg-muted/50 group-data-[state=selected]:bg-muted">`；名称 `data-sticky="name" className="sticky left-10 z-20 min-w-64 max-w-80 bg-background shadow-[inset_-1px_0_0_var(--border)] group-hover:bg-muted/50 group-data-[state=selected]:bg-muted"`；`TableRow` 加 `className="group"` 和 `data-state={selected ? "selected" : undefined}`；表头单元格 `sticky top-0 z-30`。
- 表头勾选框：勾选/取消当前页（`onToggleAllOnPage(checked)`）；"选择全部匹配结果"移到工具栏（Task 11）。
- 状态：`<Badge variant={tone} title={raw}>{label}</Badge>`。
- 指标单元格：多币种时每个桶一行 `USD 12.34`；单桶时 `formatMetric(value, unit)`；`availability` 非 `AVAILABLE`/`PARTIAL` 时显示 "—"，`title` 为原因（`UNSUPPORTED`：平台未提供；`FAILED`：读取失败，请刷新重试；其他：暂无数据，同步完成后显示）。
- 指标表头：`SortableHead` 保留现有组件；外层 `Tooltip` 内容为 `fullName`；若 `recoveryDay !== null && isRecoveryOpen(recoveryDay, endDate, timezones)`，表头文字加 `text-muted-foreground`，Tooltip 追加"回收窗口未结束，数值仍会增长"。`timezones` 取当前页各行 `metric_buckets[*].timezone` 去重（`MIXED` 忽略）。
- 配置列：预算取 `display.budget ?? display.campaign_budget ?? display.adgroup_budget`；目标 ROAS 取 `display.target_roas`；优化目标映射 `VALUE → 价值`、其余原样。
- 账户余额与对账提示：保留现有 `balance`、`reconciliation === "DIFF"` 的信息图标逻辑，放在余额/消耗单元格内。
- 删除 `officialPlatformUrl` 引用、"详情"列、"数据状态"列。

- [ ] **Step 6: AdsWorkspace.tsx 接线**

- 用 `useMetricCatalog()` 和 `columnsFor(catalog, applied.dimension)` 生成 `columns`（Task 10 再接入列偏好）。
- `onDrill(row)`：沿用 `navigateToChild` 的逻辑，父 ID 取 `row.display.remote_id`，账户取 `row.display.account_id ?? row.display.advertiser_id`，目标维度取 `childDimension(applied.dimension)`。
- `onFilterParent(link, advertiserId)`：`setApplied({ ...applied, dimension: link.kind === "account" ? "campaign" : link.kind === "campaign" ? "adgroup" : "ad", advertiser_ids: [advertiserId], ids: [link.id], page: 1, cursor: undefined, snapshot_id: undefined })`，并同步 `setSearch`、清空选择。
- `onOpen(row)`：暂时 `setSelectedRow(row)`（Task 12 接入新详情面板）；暂时保留旧 `AdsDetails` 直到 Task 12 删除。

- [ ] **Step 7: 运行确认通过**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts tests/ads-reporting.spec.ts && bun run lint && bun run build`
Expected: PASS。`ads-reporting.spec.ts` 中断言旧列名、"查看详情"的地方按新行为更新。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/features/ads/AdsNameCell.tsx frontend/src/features/ads/AdsTable.tsx frontend/src/features/ads/search.ts frontend/src/features/ads/AdsWorkspace.tsx frontend/tests/utils/adsBoundary.ts frontend/tests/fixtures/metricCatalog.json frontend/tests/ads-workspace.spec.ts frontend/tests/ads-reporting.spec.ts
git commit -m "frontend: 报表表格固定勾选与名称列，列名改为官方口径，默认账户维度"
```

### Task 9: 汇总卡片与页面趋势

**Files:**
- Rewrite: `frontend/src/features/ads/AdsSummary.tsx`
- Create: `frontend/src/features/ads/AdsTrendChart.tsx`
- Create: `frontend/src/features/ads/useAdsTrend.ts`
- Create: `frontend/src/components/ui/chart.tsx`（`bunx shadcn@latest add chart` 生成）
- Modify: `frontend/package.json`、`frontend/bun.lock`（recharts，由 shadcn 命令写入）
- Modify: `frontend/src/features/ads/AdsWorkspace.tsx`
- Modify: `frontend/tests/utils/adsBoundary.ts`（模拟 `/ads/trend`）
- Test: `frontend/tests/ads-workspace.spec.ts`

**Interfaces:**
- Consumes: `AdsReportingService.adTrend(...)`、`formatMetric`、`ColumnDef`。
- Produces:
  - `useAdsTrend(params: { level: TrendLevel; advertiserIds?: string[]; ids?: string[]; startDate: string; endDate: string; granularity: "day"|"hour"; enabled: boolean })`。
  - `AdsTrendChart({ trend: TrendPublic | undefined, metric: ColumnDef, loading: boolean, error: boolean, onRetry })`：每个币种一条线（金额类）或一条线（比率/次数类按币种分别画）。
  - `AdsSummary({ summary, catalog, activeMetric, onSelectMetric })`。
  - `SUMMARY_KEYS = ["spend","revenue_total","roas_total","roas_d0","impressions","ctr","cpm"]`。

- [ ] **Step 1: 安装图表组件**

Run: `cd frontend && bunx shadcn@latest add chart`
Expected: 生成 `src/components/ui/chart.tsx`，`package.json` 新增 `recharts`。确认版本被锁定在 `bun.lock`；`bun run build` 通过。

- [ ] **Step 2: 夹具与失败测试**

`adsBoundary.ts` 增加 `/ads/trend` 响应：两天、USD 单桶，第一天 `spend: "4.00"`、第二天 `spend: "6.00"`，`coverage: "COMPLETE"`；并记录请求次数到 `options.trendCalls`（新增可选 `trendCalls?: { count: number }`）。`summary` 夹具新增 `incomplete_advertiser_count: 2` 的分支（`options.incomplete` 为真时）。

`ads-workspace.spec.ts` 追加：

```ts
  test("汇总卡片使用官方名并提示不完整账户", async ({ page }) => {
    await adsBoundary(page, { incomplete: true })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    for (const name of ["消耗", "广告展示总价值", "广告收益 ROAS", "第 0 天广告收益 ROAS", "展示量", "点击率（目标页面）", "千次展示成本 (CPM)"])
      await expect(page.getByRole("button", { name: new RegExp(`^${name}`) })).toBeVisible()
    await expect(page.getByText("2 个账户数据不完整")).toBeVisible()
  })

  test("趋势默认折叠，展开才请求", async ({ page }) => {
    const trendCalls = { count: 0 }
    await adsBoundary(page, { trendCalls })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await expect(page.getByRole("heading", { name: "广告报表" })).toBeVisible()
    expect(trendCalls.count).toBe(0)
    await page.getByRole("button", { name: "展开趋势" }).click()
    await expect(page.getByTestId("ads-trend-chart")).toBeVisible()
    expect(trendCalls.count).toBe(1)
  })
```

- [ ] **Step 3: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts -g "汇总|趋势"`
Expected: FAIL。

- [ ] **Step 4: 实现**

`useAdsTrend.ts`：

```ts
import { useQuery } from "@tanstack/react-query"
import { AdsReportingService, type TrendLevel } from "@/client"
import { useTenantScope } from "@/features/tenants/TenantScope"

export function useAdsTrend(params: {
  level: TrendLevel
  advertiserIds?: string[]
  ids?: string[]
  startDate: string
  endDate: string
  granularity: "day" | "hour"
  enabled: boolean
}) {
  const { tenantId, bc } = useTenantScope()
  const bcId = bc?.bc_id
  return useQuery({
    // 键包含 BC：切换 BC 后旧请求的结果不会显示到新 BC。
    queryKey: ["tenant", tenantId, "ads-trend", bcId, params],
    enabled: params.enabled && Boolean(tenantId && bcId),
    staleTime: 60_000,
    queryFn: async ({ signal }) =>
      (
        await AdsReportingService.adTrend({
          path: { tenant_id: tenantId! },
          query: {
            bc_id: bcId!,
            level: params.level,
            advertiser_ids: params.advertiserIds?.join(",") || undefined,
            ids: params.ids?.join(",") || undefined,
            start_date: params.startDate,
            end_date: params.endDate,
            granularity: params.granularity,
          },
          signal,
        })
      ).data,
  })
}
```

`AdsTrendChart.tsx`：使用 `ChartContainer`、`LineChart`、`XAxis`、`YAxis`、`CartesianGrid`、`ChartTooltip`、`Line`。数据转换：

```ts
const currencies = [...new Set(trend.points.flatMap((p) => p.buckets.map((b) => b.currency)))]
const data = trend.points.map((point) => ({
  at: point.at,
  coverage: point.coverage,
  ...Object.fromEntries(
    point.buckets.map((b) => [b.currency, b.values?.[metric.key] == null ? null : Number(b.values[metric.key])]),
  ),
}))
```

每个币种一条 `<Line dataKey={currency} connectNulls={false} />`；X 轴日粒度显示 `MM-DD`，小时粒度显示 `HH:00`；`INCOMPLETE` 的点在 Tooltip 中注明"该日数据不完整"。根节点 `data-testid="ads-trend-chart"`。加载中显示 `Skeleton`，出错显示"趋势读取失败"与"重试"按钮。`trend.timezone === "MIXED"` 时图下方注明"各账户按本地日期汇总"。

`AdsSummary.tsx`：7 张卡片，每张是 `button`（`aria-pressed={active}`），标题为字典表头名（`basic_label`），`title` 为完整官方名；值逐币种显示，`PARTIAL` 照常显示数值；卡片区下方当 `summary.incomplete_advertiser_count > 0` 时显示 `"{n} 个账户数据不完整"`，悬停列出 `incomplete_dates`。

`AdsWorkspace.tsx`：
- 卡片下方加折叠区：按钮文案"展开趋势"/"收起趋势"，状态存 `localStorage`（键 `ads-trend-open`，读写包 `try/catch`），默认折叠。
- 展开时 `useAdsTrend({ level: "account", advertiserIds: applied.advertiser_ids, startDate, endDate, granularity: startDate === endDate ? "hour" : "day", enabled: open })`。
- `activeMetric` 状态默认 `spend`，点卡片切换。

- [ ] **Step 5: 运行确认通过**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts && bun run lint && bun run build`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/components/ui/chart.tsx frontend/package.json frontend/bun.lock frontend/src/features/ads/AdsSummary.tsx frontend/src/features/ads/AdsTrendChart.tsx frontend/src/features/ads/useAdsTrend.ts frontend/src/features/ads/AdsWorkspace.tsx frontend/tests/utils/adsBoundary.ts frontend/tests/ads-workspace.spec.ts
git commit -m "frontend: 报表汇总改为官方指标卡片并新增按需加载的趋势图"
```

### Task 10: 列设置与保存筛选

**Files:**
- Create: `frontend/src/features/ads/columnPrefs.ts`
- Create: `frontend/src/features/ads/AdsTableToolbar.tsx`（本任务只实现未选中状态的"列设置"）
- Modify: `frontend/src/features/ads/AdsWorkspace.tsx`
- Test: `frontend/tests/ads-workspace.spec.ts`

**Interfaces:**
- Produces:
  - `loadColumnPrefs(scopeKey: string, dimension: AdsDimension): { hidden: string[]; order: string[] } | null`、`saveColumnPrefs(scopeKey, dimension, prefs)`；`scopeKey = \`${tenantId}:${userId}\``；全部读写包在 `try/catch` 中，失败时返回 `null`/静默。
  - `applyColumnPrefs(columns: ColumnDef[], prefs): ColumnDef[]`：先按 `order` 排序（未出现在 `order` 的列保持字典顺序追加），再去掉 `hidden`。
  - `AdsTableToolbar` props（本任务部分）：`{ columns: ColumnDef[]; prefs; onPrefsChange(prefs) }`；Task 11 再加选中态 props。

- [ ] **Step 1: 失败测试**

```ts
  test("隐藏列在刷新后保持", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("button", { name: "列设置" }).click()
    await page.getByRole("menuitemcheckbox", { name: "千次展示成本 (CPM)" }).click()
    await page.keyboard.press("Escape")
    await expect(page.getByRole("columnheader", { name: /千次展示成本/ })).toHaveCount(0)
    await page.reload()
    await expect(page.getByRole("columnheader", { name: /千次展示成本/ })).toHaveCount(0)
  })

  test("保存筛选提交字典列键", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    const request = page.waitForRequest((r) => r.url().endsWith("/report-views") && r.method() === "POST")
    await page.getByRole("button", { name: "保存筛选" }).click()
    const body = (await request).postDataJSON()
    expect(body.columns).toContain("roas_d0")
    expect(body.columns).not.toContain("status_raw")
  })
```

（`adsBoundary` 需对 `POST /report-views` 返回 200 的视图对象；若尚无此分支，按 `SavedViewPublic` 字段补一个。）

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts -g "列|保存筛选"`
Expected: FAIL。

- [ ] **Step 3: 实现**

`columnPrefs.ts`：

```ts
import type { AdsDimension } from "./search"
import type { ColumnDef } from "./metricCatalog"

export type ColumnPrefs = { hidden: string[]; order: string[] }
const keyOf = (scope: string, dimension: AdsDimension) => `ads-columns:${scope}:${dimension}`

// 列偏好只是个人浏览习惯，放本地即可；存储不可用时退回字典默认列，不影响报表。
export function loadColumnPrefs(scope: string, dimension: AdsDimension): ColumnPrefs | null {
  try {
    const raw = localStorage.getItem(keyOf(scope, dimension))
    return raw ? (JSON.parse(raw) as ColumnPrefs) : null
  } catch {
    return null
  }
}

export function saveColumnPrefs(scope: string, dimension: AdsDimension, prefs: ColumnPrefs) {
  try {
    localStorage.setItem(keyOf(scope, dimension), JSON.stringify(prefs))
  } catch {
    // 隐私模式或存储已满时放弃保存。
  }
}

export function applyColumnPrefs(columns: ColumnDef[], prefs: ColumnPrefs | null): ColumnDef[] {
  if (!prefs) return columns
  const rank = new Map(prefs.order.map((key, index) => [key, index]))
  return [...columns]
    .sort((a, b) => (rank.get(a.key) ?? 1e6 + columns.indexOf(a)) - (rank.get(b.key) ?? 1e6 + columns.indexOf(b)))
    .filter((column) => !prefs.hidden.includes(column.key))
}
```

`AdsTableToolbar.tsx`（未选中态）：`DropdownMenu` 触发按钮"列设置"；每列一个 `DropdownMenuCheckboxItem`（勾选=显示），以及每项右侧"上移/下移"两个小图标按钮（`aria-label="上移 {label}"`）；底部"恢复默认"。

`AdsWorkspace.tsx`：
- `const { user } = useAuth()`；`scope = \`${tenantId}:${user?.id}\``。
- `prefs` 状态随维度加载；`visibleColumns = applyColumnPrefs(columnsFor(...), prefs)` 传给 `AdsTable`。
- "保存筛选"按钮移到筛选区（`AdsFilters` 内，与"应用筛选""重置"并列）；`viewMutation` 的 `columns` 改为 `["name", "status", ...visibleColumns.map((c) => c.key)]`。

- [ ] **Step 4: 运行确认通过并提交**

Run: `cd frontend && bunx playwright test tests/ads-workspace.spec.ts && bun run lint && bun run build`

```bash
git add frontend/src/features/ads/columnPrefs.ts frontend/src/features/ads/AdsTableToolbar.tsx frontend/src/features/ads/AdsWorkspace.tsx frontend/src/features/ads/AdsFilters.tsx frontend/tests/utils/adsBoundary.ts frontend/tests/ads-workspace.spec.ts
git commit -m "frontend: 报表支持列显示与排序设置，保存筛选使用字典列键"
```

---

## 阶段三：批量操作

### Task 11: 表头批量工具栏与预览面板

**Files:**
- Modify: `frontend/src/features/ads/AdsTableToolbar.tsx`
- Modify: `frontend/src/features/ad-management/ManagementPreviewSheet.tsx`
- Modify: `frontend/src/features/ads/AdsWorkspace.tsx`
- Delete: `frontend/src/features/ads/BulkActionBar.tsx`
- Test: `frontend/tests/ad-management.spec.ts`

**Interfaces:**
- Produces:
  - `type ManagementAction = { field: "status"; value: "ENABLE" | "DISABLE" } | { field: "roas" | "budget" }`。
  - `ManagementPreviewSheet` props 新增 `action: ManagementAction | null`；删除内部 `field` 下拉框状态。
  - `AdsTableToolbar` 选中态 props：`{ selectedCount; allMatching; total; dimension; readonly; busy; onAction(action: ManagementAction); onSelectAllMatching(); onClear() }`。
  - `actionsFor(dimension: AdsDimension): Array<"enable"|"disable"|"roas"|"budget">`：

| 维度 | 动作 |
| --- | --- |
| account | enable、disable、budget |
| campaign | enable、disable、roas、budget |
| adgroup | enable、disable、roas、budget |
| ad | enable、disable、roas |
| material | enable、disable |
| drama | enable、disable、budget |

- [ ] **Step 1: 失败测试**

在 `ad-management.spec.ts` 中把原先点"批量操作"按钮的三处改为点具体动作，并追加：

```ts
  test("勾选后表头直接出现动作，启用提交 ENABLE", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page.getByRole("checkbox", { name: "选择 嘉书-总裁归来-测试" }).check()
    for (const name of ["启用", "暂停", "修改目标 ROAS", "修改预算", "取消选择"])
      await expect(page.getByRole("button", { name })).toBeVisible()
    await expect(page.getByRole("button", { name: "批量操作" })).toHaveCount(0)
    const preview = page.waitForRequest((r) => r.url().includes("ad-management-previews") && r.method() === "POST")
    await page.getByRole("button", { name: "启用" }).click()
    expect((await preview).postDataJSON().mutation).toMatchObject({ field: "status", mode: "set", value: "ENABLE" })
    await expect(page.getByRole("combobox", { name: "操作维度" })).toHaveCount(0)
  })

  test("修改目标 ROAS 支持按百分比增加，数值为空不生成预览", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组" }).click()
    await page.getByRole("checkbox").nth(1).check()
    await page.getByRole("button", { name: "修改目标 ROAS" }).click()
    await expect(page.getByRole("status", { name: /正在生成/ })).toHaveCount(0)
    await page.getByRole("combobox", { name: "调整方式" }).click()
    await page.getByRole("option", { name: "增加百分比" }).click()
    const preview = page.waitForRequest((r) => r.url().includes("ad-management-previews"))
    await page.getByRole("textbox", { name: "管理新值" }).fill("10")
    expect((await preview).postDataJSON().mutation).toMatchObject({ field: "roas", mode: "increase_percent", value: "10" })
  })

  test("账户维度不提供修改目标 ROAS", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("checkbox").nth(1).check()
    await expect(page.getByRole("button", { name: "修改目标 ROAS" })).toHaveCount(0)
  })
```

原"只读成员看不到批量操作"用例改为断言"启用""暂停"按钮数量为 0。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ad-management.spec.ts`
Expected: FAIL。

- [ ] **Step 3: 实现预览面板**

`ManagementPreviewSheet.tsx`：
- 新 prop `action`；删除 `field` 状态与"操作维度" `<select>`；`SheetTitle` 改为动作名称："批量启用"/"批量暂停"/"修改目标 ROAS"/"修改预算"。
- 新状态 `mode: MutationSpec["mode"]`（默认 `"set"`）与 `value: string`（默认 `""`，删除 `DEFAULT_VALUE`）；`open` 或 `selection_id` 变化时重置为 `"set"`/`""`。
- 数值类动作时显示 shadcn `Select`（`aria-label="调整方式"`）：设为 `set`、增加 `add`、减少 `subtract`、增加百分比 `increase_percent`、减少百分比 `decrease_percent`；输入框 `aria-label="管理新值"`，百分比模式时后缀显示 `%`。
- `mutation`：

```ts
  const mutation = useMemo<MutationSpec | null>(() => {
    if (!action) return null
    if (action.field === "status")
      return { field: "status", mode: "set", value: action.value, include_parents: includeParents ? (selection?.refs ?? []) : [], excluded_refs: excludedRefs }
    // 数值为空或非法时不生成预览，避免用默认值误改真实广告。
    if (!value.trim() || !Number.isFinite(Number(value))) return null
    return { field: action.field, mode, value, include_parents: includeParents ? (selection?.refs ?? []) : [], excluded_refs: excludedRefs }
  }, [action, mode, value, includeParents, excludedRefs, selection?.refs])
```

  `preview` 查询 `enabled: open && !!selection && !!mutation`；数值输入加 400ms 防抖（`useDeferredValue` 或 `setTimeout`）后再写入 `value`。
- 预览列表"目标"列显示名称：从 `selection.rows` 或预览项可用的名称字段取；若预览项只有 `ref`，保留 `kind · remote_id`，并把 kind 翻成中文（系列/广告组/广告/素材）。实现前查看 `ManagementPreviewPublic.items[]` 的生成类型，确认是否有名称字段。
- `MutationSpec["mode"]` 的类型以生成客户端为准（应为 `"set"|"add"|"subtract"|"increase_percent"|"decrease_percent"`）。

- [ ] **Step 4: 实现工具栏选中态并接线**

`AdsTableToolbar.tsx` 选中态：左侧文本 `已选 {n} 条` 或 `已选全部 {total} 条匹配结果`；右侧按 `actionsFor(dimension)` 渲染按钮（"启用""暂停"为 `variant="outline"`，"修改目标 ROAS""修改预算"为 `variant="outline"`），然后"选择全部匹配结果"（`allMatching` 时禁用）与"取消选择"（`variant="ghost"`）；`readonly` 时只显示计数与"取消选择"；`busy` 时动作按钮禁用。

`AdsWorkspace.tsx`：
- `const [action, setAction] = useState<ManagementAction | null>(null)`。
- `onAction(next)`：`setAction(next)`，记录 `selectionRequestScopeRef`，调用 `selectionMutation.mutate()`（成功后打开面板，沿用现有逻辑）。
- 删除 `BulkActionBar` 引用与文件；工具栏渲染在表格卡片内、维度页签与表格之间，只有选中数 > 0 时切换为选中态。
- `clearManagementPreview` 同时 `setAction(null)`。

- [ ] **Step 5: 运行确认通过并提交**

Run: `cd frontend && bunx playwright test tests/ad-management.spec.ts tests/ads-workspace.spec.ts && bun run lint && bun run build`

```bash
git rm frontend/src/features/ads/BulkActionBar.tsx
git add frontend/src/features/ads/AdsTableToolbar.tsx frontend/src/features/ad-management/ManagementPreviewSheet.tsx frontend/src/features/ads/AdsWorkspace.tsx frontend/tests/ad-management.spec.ts
git commit -m "frontend: 勾选后表头直接提供启停与调价动作，修复无法批量启用"
```

---

## 阶段四：详情面板

### Task 12: 详情面板与单对象操作

**Files:**
- Create: `frontend/src/features/ads/details/AdsDetailSheet.tsx`
- Create: `frontend/src/features/ads/details/OverviewTab.tsx`、`TrendTab.tsx`、`SettingsTab.tsx`、`MaterialsTab.tsx`、`HistoryTab.tsx`
- Create: `frontend/src/features/ads/details/configFields.ts`
- Modify: `frontend/src/features/ads/AdsWorkspace.tsx`
- Delete: `frontend/src/features/ads/AdsDetails.tsx`
- Modify: `frontend/tests/utils/adsBoundary.ts`（详情响应带完整 `configuration`）
- Test: `frontend/tests/ads-detail.spec.ts`（新建）、`frontend/tests/ads-detail-config.spec.ts`（新建，纯函数）

**Interfaces:**
- Consumes: `AdsReportingService.adDetail`、`useAdsTrend`、`AdsTrendChart`、`statusLabel`、`formatMetric`、`columnsFor`、`ManagementAction`。
- Produces:
  - `AdsDetailSheet({ row: ReportRow | null; dimension: AdsDimension; startDate; endDate; readonly; onClose(); onAction(row, action: ManagementAction); onOpenParent(link: ParentLink, advertiserId) })`。
  - `configSections(kind: "campaign"|"adgroup"|"ad", config: Record<string, unknown>): Array<{ title: string; items: Array<{ label: string; value: string }> }>`，未映射字段归入标题"其他字段"的分组。

- [ ] **Step 1: 配置字段失败测试**

新建 `frontend/tests/ads-detail-config.spec.ts`：

```ts
import { expect, test } from "@playwright/test"
import { configSections } from "../src/features/ads/details/configFields"

test("广告组配置翻译关键字段并保留未知字段", () => {
  const sections = configSections("adgroup", {
    roas_bid: "1.1",
    bid_type: "BID_TYPE_NO_BID",
    optimization_goal: "VALUE",
    vbo_window: "ZERO_DAY",
    placements: ["PLACEMENT_TIKTOK"],
    targeting_spec: { gender: "GENDER_UNLIMITED", age_groups: ["AGE_18_24", "AGE_25_34"], location_ids: ["6252001"] },
    click_attribution_window: "SEVEN_DAYS",
    brand_new_field: "x",
  })
  const flat = Object.fromEntries(sections.flatMap((s) => s.items.map((i) => [i.label, i.value])))
  expect(flat["目标 ROAS"]).toBe("1.1")
  expect(flat["出价方式"]).toBe("最大化投放")
  expect(flat["优化目标"]).toBe("价值")
  expect(flat["价值优化窗口"]).toBe("第 0 天")
  expect(flat["版位"]).toBe("TikTok")
  expect(flat["性别"]).toBe("不限")
  expect(flat["年龄"]).toBe("18-24、25-34")
  expect(flat["点击归因窗口"]).toBe("7 天")
  expect(sections.at(-1)?.title).toBe("其他字段")
  expect(flat["brand_new_field"]).toBe("x")
})
```

- [ ] **Step 2: 详情面板失败测试**

`adsBoundary.ts` 的详情响应（`/ads/{kind}/{id}`）改为返回线上实测形态的配置（从本计划核对时取到的系列/广告组字段：`budget`、`budget_mode`、`objective_type`、`app_promotion_type`、`budget_optimize_on`、`roas_bid`、`bid_type`、`optimization_event`、`vbo_window`、`schedule_type`、`schedule_start_time`、`placements`、`targeting_spec`、`click_attribution_window`、`minis_id`），`operation_history` 返回一条 `{ task_id, created_at, field: "status", mode: "set", original_value: "ENABLE", final_value: "DISABLE", execution_result: "SUCCEEDED" }`。

新建 `frontend/tests/ads-detail.spec.ts`：

```ts
import { expect, test } from "@playwright/test"
import { adsBoundary, BC_A, TENANT } from "./utils/adsBoundary"

test.describe("ads detail", () => {
  test("点名称打开详情，各页签有内容", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "广告组" }).click()
    await page.getByRole("button", { name: /嘉书-总裁归来/ }).first().click()
    const sheet = page.getByRole("dialog")
    await expect(sheet.getByRole("tab", { name: "概览" })).toBeVisible()
    await expect(sheet.getByText("版权方")).toBeVisible()
    await sheet.getByRole("tab", { name: "设置" }).click()
    await expect(sheet.getByText("目标 ROAS")).toBeVisible()
    await sheet.getByRole("tab", { name: "趋势" }).click()
    await expect(sheet.getByTestId("ads-trend-chart")).toBeVisible()
    await sheet.getByRole("tab", { name: "操作记录" }).click()
    await expect(sheet.getByText("ENABLE → DISABLE")).toBeVisible()
  })

  test("详情内暂停走同一预览流程", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page.getByRole("button", { name: /嘉书-总裁归来/ }).first().click()
    const freeze = page.waitForRequest((r) => r.url().includes("/ad-selections"))
    await page.getByRole("dialog").getByRole("button", { name: "暂停" }).click()
    expect((await freeze).postDataJSON()).toMatchObject({ mode: "EXPLICIT" })
    await expect(page.getByRole("heading", { name: "批量暂停" })).toBeVisible()
  })

  test("切 BC 关闭详情", async ({ page }) => {
    await adsBoundary(page)
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page.getByRole("button", { name: /嘉书-总裁归来/ }).first().click()
    await expect(page.getByRole("dialog")).toBeVisible()
    await page.keyboard.press("Escape")
    await page.getByRole("combobox", { name: "当前 BC" }).click()
    await page.getByText("乙 BC", { exact: true }).click()
    await expect(page.getByRole("dialog")).toHaveCount(0)
  })

  test("只读成员详情无操作按钮", async ({ page }) => {
    await adsBoundary(page, { role: "viewer" })
    await page.goto(`/tenants/${TENANT}/ads?bc_id=${BC_A}`)
    await page.getByRole("tab", { name: "系列" }).click()
    await page.getByRole("button", { name: /嘉书-总裁归来/ }).first().click()
    await expect(page.getByRole("dialog").getByRole("button", { name: "暂停" })).toHaveCount(0)
  })
})
```

（`ad-management.spec.ts` 的 `managementBoundary(page, { viewer: true })` 在 `/api/me/tenants` 返回 `role: "viewer"`。`adsBoundary` 按同样方式新增 `role?: "viewer" | "operator"` 选项，只改该响应里的 `role`。）

- [ ] **Step 3: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/ads-detail.spec.ts tests/ads-detail-config.spec.ts`
Expected: FAIL。

- [ ] **Step 4: configFields.ts**

```ts
type Section = { title: string; items: Array<{ label: string; value: string }> }
type Field = { key: string; label: string; format?: (value: unknown) => string }

const ENUMS: Record<string, string> = {
  APP_PROMOTION: "应用推广", MINIS: "Minis", REGULAR_CAMPAIGN: "常规系列",
  BUDGET_MODE_DYNAMIC_DAILY_BUDGET: "动态日预算", BUDGET_MODE_DAY: "日预算",
  BUDGET_MODE_TOTAL: "总预算", BUDGET_MODE_INFINITE: "不限",
  BID_TYPE_NO_BID: "最大化投放", BID_TYPE_CUSTOM: "自定义出价",
  VALUE: "价值", VO_MIN_ROAS: "最低 ROAS", ZERO_DAY: "第 0 天", SEVEN_DAYS: "7 天",
  ONE_DAY: "1 天", OFF: "关闭", EVERY: "每次", ONCE: "一次",
  OCPM: "oCPM", MINI_APP: "小程序", IMPRESSION_LEVEL_AD_REVENUE: "展示级广告收益",
  SCHEDULE_FROM_NOW: "从现在起长期投放", SCHEDULE_START_END: "指定起止时间",
  PLACEMENT_TYPE_NORMAL: "手动版位", PLACEMENT_TYPE_AUTOMATIC: "自动版位",
  PLACEMENT_TIKTOK: "TikTok", PLACEMENT_PANGLE: "Pangle",
  GENDER_UNLIMITED: "不限", GENDER_MALE: "男", GENDER_FEMALE: "女",
  OVER_EIGHTEEN: "18 岁以上", MULTIPLE: "多广告组",
}
// 未登记的枚举原样显示，平台新增值时不会被吞掉。
const text = (value: unknown): string => {
  if (value === null || value === undefined || value === "") return "—"
  if (Array.isArray(value)) return value.map(text).join("、")
  if (typeof value === "boolean") return value ? "开启" : "关闭"
  if (typeof value === "object") return JSON.stringify(value)
  const raw = String(value)
  return ENUMS[raw] ?? raw
}
const age = (value: unknown) =>
  Array.isArray(value)
    ? value.map((v) => String(v).replace(/^AGE_/, "").replace("_100", "+").replace("_", "-")).join("、")
    : text(value)

const SECTIONS: Record<"campaign" | "adgroup" | "ad", Array<{ title: string; fields: Field[] }>> = {
  campaign: [
    { title: "基本信息", fields: [
      { key: "objective_type", label: "推广目标" },
      { key: "app_promotion_type", label: "推广类型" },
      { key: "campaign_type", label: "系列类型" },
      { key: "smart_plus_adgroup_mode", label: "Smart+ 广告组模式" },
      { key: "create_time", label: "创建时间" },
      { key: "modify_time", label: "修改时间" },
    ] },
    { title: "预算", fields: [
      { key: "budget", label: "预算" },
      { key: "budget_mode", label: "预算模式" },
      { key: "budget_optimize_on", label: "系列预算优化" },
    ] },
  ],
  adgroup: [
    { title: "优化与出价", fields: [
      { key: "promotion_type", label: "推广类型" },
      { key: "minis_id", label: "Minis ID" },
      { key: "optimization_goal", label: "优化目标" },
      { key: "optimization_event", label: "优化事件" },
      { key: "bid_type", label: "出价方式" },
      { key: "deep_bid_type", label: "深度出价方式" },
      { key: "roas_bid", label: "目标 ROAS" },
      { key: "conversion_bid_price", label: "转化出价" },
      { key: "vbo_window", label: "价值优化窗口" },
      { key: "billing_event", label: "计费方式" },
    ] },
    { title: "预算与排期", fields: [
      { key: "budget", label: "预算" },
      { key: "budget_mode", label: "预算模式" },
      { key: "min_budget", label: "最低预算" },
      { key: "schedule_type", label: "排期类型" },
      { key: "schedule_start_time", label: "开始时间" },
      { key: "schedule_end_time", label: "结束时间" },
      { key: "dayparting", label: "分时投放" },
    ] },
    { title: "版位与定向", fields: [
      { key: "placement_type", label: "版位类型" },
      { key: "placements", label: "版位" },
      { key: "targeting_spec.location_ids", label: "地区 ID" },
      { key: "targeting_spec.age_groups", label: "年龄", format: age },
      { key: "targeting_spec.gender", label: "性别" },
      { key: "targeting_spec.languages", label: "语言" },
      { key: "targeting_spec.spc_audience_age", label: "Smart+ 受众年龄" },
    ] },
    { title: "归因", fields: [
      { key: "click_attribution_window", label: "点击归因窗口" },
      { key: "view_attribution_window", label: "浏览归因窗口" },
      { key: "engaged_view_attribution_window", label: "专注观看归因窗口" },
      { key: "attribution_event_count", label: "事件计数方式" },
    ] },
    { title: "互动设置", fields: [
      { key: "comment_disabled", label: "关闭评论" },
      { key: "share_disabled", label: "关闭分享" },
      { key: "video_download_disabled", label: "关闭下载" },
      { key: "create_time", label: "创建时间" },
      { key: "modify_time", label: "修改时间" },
    ] },
  ],
  ad: [
    { title: "内容", fields: [
      { key: "ad_text_list", label: "文案", format: (v) => Array.isArray(v) ? v.map((i) => (i as { ad_text?: string }).ad_text ?? text(i)).join(" / ") : text(v) },
      { key: "landing_page_url_list", label: "落地页", format: (v) => Array.isArray(v) ? v.map((i) => (i as { landing_page_url?: string }).landing_page_url ?? text(i)).join("\n") : text(v) },
      { key: "creative_list", label: "创意数", format: (v) => (Array.isArray(v) ? `${v.length} 个` : text(v)) },
      { key: "create_time", label: "创建时间" },
      { key: "modify_time", label: "修改时间" },
    ] },
  ],
}
// 已在表头/概览展示或属于身份信息的字段，不放进"其他字段"。
const IDENTITY = new Set(["advertiser_id", "campaign_id", "adgroup_id", "ad_id", "smart_plus_ad_id", "campaign_name", "adgroup_name", "ad_name", "operation_status", "secondary_status", "targeting_spec", "ad_configuration"])

const read = (config: Record<string, unknown>, path: string) =>
  path.split(".").reduce<unknown>((value, key) => (value && typeof value === "object" ? (value as Record<string, unknown>)[key] : undefined), config)

export function configSections(kind: "campaign" | "adgroup" | "ad", config: Record<string, unknown>) {
  const used = new Set<string>()
  const sections: Section[] = SECTIONS[kind].map(({ title, fields }) => ({
    title,
    items: fields
      .filter((field) => read(config, field.key) !== undefined)
      .map((field) => {
        used.add(field.key.split(".")[0])
        return { label: field.label, value: (field.format ?? text)(read(config, field.key)) }
      }),
  })).filter((section) => section.items.length > 0)
  const rest = Object.keys(config).filter((key) => !used.has(key) && !IDENTITY.has(key))
  if (rest.length)
    sections.push({ title: "其他字段", items: rest.map((key) => ({ label: key, value: text(config[key]) })) })
  return sections
}
```

注意：`targeting_spec` 中已映射子项以外的键（如 `brand_safety_type`），在"版位与定向"组末尾逐项以原字段名追加，不丢弃——在 `configSections` 中对 `targeting_spec` 单独补这一步。

- [ ] **Step 5: 详情面板组件**

`AdsDetailSheet.tsx`：
- `Sheet` 右侧，`SheetContent className="w-full overflow-y-auto sm:max-w-[min(60vw,1100px)] sm:min-w-[720px]"`。
- 页头：名称、类型（`kind` 中文：账户/系列/广告组/广告/素材/剧）、`statusLabel` 徽标；非只读且 `actionsFor(dimension)` 非空时显示动作按钮，点击 `onAction(row, action)`。
- `Tabs`：概览、趋势、设置（仅系列/广告组/广告）、素材（仅广告组/广告/素材）、操作记录（仅系列/广告组/广告）。
- 对象详情查询：`kind ∈ campaign|adgroup|ad` 时用 `AdsReportingService.adDetail`，`queryKey` 包含 `bc.bc_id`；账户、剧、素材不请求详情接口。

`OverviewTab.tsx`：层级路径（`parentPath`，点击调用 `onOpenParent`）；命名解析：`display.provider`、`display.drama_name ?? display.name` 第二段、`naming_status`（`INVALID` 时提示"命名不符合 版权方-剧名-备注 规则"）；状态与父级限制原因（`detail.parent.reasons`，把 `operation_status:DISABLE` 之类前缀翻为中文）；核心指标网格：`columnsFor` 结果中前 8 个，用 `metricOf` + `formatMetric`；账户额外显示余额、币种、时区、账户状态；剧额外显示关联系列列表（`row.refs`，每项点击切换到该系列详情）。

`TrendTab.tsx`：区间选择（近 7 天、近 14 天、近 30 天、跟随页面），默认近 14 天（结束日为页面结束日）；页面区间为单日且对象为账户/系列时提供"按小时"开关。`useAdsTrend` 参数映射：

| 对象 | level | advertiserIds | ids |
| --- | --- | --- | --- |
| 账户 | account | [account_id] | [] |
| 系列 | campaign | [account_id] | [remote_id] |
| 广告组 | adgroup | [account_id] | [remote_id] |
| 广告 | ad | [account_id] | [remote_id] |
| 素材 | material | [account_id] | [remote_id] |
| 剧 | campaign | 去重的 refs 账户 | refs 的 remote_id |

指标切换按钮组（消耗、广告展示总价值、广告收益 ROAS、第 0 天广告收益 ROAS、展示量、点击率）；下方逐日明细 `Table`，列为全部 `columnsFor` 指标。

`SettingsTab.tsx`：`configSections(kind, detail.configuration)` 渲染为分组的两列描述列表；顶部注明"配置来自最近一次目录同步：{observed_at}"。

`MaterialsTab.tsx`：广告：`detail.materials` 列表（名称、类型、状态、本地/外部）；广告组/系列：显示"该范围的素材请在素材维度查看"并提供按钮切换到素材维度（`ids` 过滤）。

`HistoryTab.tsx`：表格列为时间、动作（`status`→启停、`roas`→目标 ROAS、`budget`→预算，加上 mode 中文）、原值 → 新值、结果；每行链接到 `/tenants/$tenantId/ad-management-tasks/$taskId`；为空时显示"暂无本工具的操作记录"。

- [ ] **Step 6: 接线与删除旧详情**

`AdsWorkspace.tsx`：
- 用 `AdsDetailSheet` 替换 `AdsDetails`；`onAction(row, action)`：冻结选择改为 `mode: "EXPLICIT"`、`row_keys: [row.row_key]`，随后与 Task 11 相同打开预览面板。给 `selectionMutation` 增加可选参数 `rowKeys?: string[]`，有值时用它而不是表格勾选集合。
- BC 变化的 `useEffect` 里已 `setSelectedRow(null)`，保留；确认详情面板 `open` 由 `selectedRow` 驱动。
- `git rm frontend/src/features/ads/AdsDetails.tsx`。

- [ ] **Step 7: 运行确认通过并提交**

Run: `cd frontend && bunx playwright test tests/ads-detail.spec.ts tests/ads-detail-config.spec.ts tests/ads-workspace.spec.ts tests/ad-management.spec.ts && bun run lint && bun run build`

```bash
git rm frontend/src/features/ads/AdsDetails.tsx
git add frontend/src/features/ads/details frontend/src/features/ads/AdsWorkspace.tsx frontend/tests/utils/adsBoundary.ts frontend/tests/ads-detail.spec.ts frontend/tests/ads-detail-config.spec.ts
git commit -m "frontend: 报表详情面板展示趋势、完整配置、素材与操作记录"
```

### Task 13: 全量验证与进度记录

**Files:**
- Modify: `docs/implementation-progress.md`

- [ ] **Step 1: 全量回归**

Run（在仓库根目录）：

```bash
cd backend && bash scripts/lint.sh && bash scripts/test.sh
cd ../frontend && bun run lint && bun run build && bunx playwright test
```

Expected: 全部通过。任何失败先定位根因再修复；与本轮无关、修复前就存在的失败，记录用例名和报错，不顺手改。

- [ ] **Step 2: 清理检查**

Run: `grep -rn "officialPlatformUrl\|BulkActionBar\|AdsDetails\|查看详情\|TikTok 后台\|D3 ROAS\|总收入" frontend/src backend/app`
Expected: 无输出。

- [ ] **Step 3: 记录进度并提交**

在 `docs/implementation-progress.md` 末尾追加 `## 2026-10-12：广告报表工作台重设计`，写明：各任务提交号、测试命令与结果（含通过数）、未执行的项目（例如缺少 `_test` 数据库时的后端回归）、尚未做的事（测试环境发布、真实 BC 启停与改 ROAS 联调需用户指定对象）。

```bash
git add docs/implementation-progress.md
git commit -m "docs: 记录广告报表工作台重设计实施结果"
```

发布到测试环境不在本计划内，按 `docs/runbooks/deployment.md` 另行执行（需先备份），并在用户确认后进行。
