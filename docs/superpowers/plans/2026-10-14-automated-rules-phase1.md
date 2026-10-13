# 自动规则阶段 1(规则与预警)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 租户可以创建跨 BC 的自动规则(系列 / 广告组 / 广告 / 素材,三种统计口径,最多 5 个条件),试运行查看命中,按排期自动判断并向租户飞书群发送预警卡片;本阶段不改动任何广告。

**Architecture:** 新增后端模块 `backend/app/modules/automation_rules`。规则定义是一个严格校验的 Pydantic 文档,存入 `automation_rule.definition`。判断分三层:输入加载(范围展开、对象目录、报表事实、新鲜度,读 PostgreSQL)→ 纯函数判断引擎 → 运行器(持久化运行与命中、经 outbox 投递飞书发送)。Beat 每 60 秒扫描到期规则;判断在 `ads-reporting` 队列,飞书发送在 `control` 队列。前端新增「自动规则」页面(规则管理、执行记录)与「通知设置」页面。

**Tech Stack:** FastAPI + SQLModel + PostgreSQL + Alembic + Celery/Redis(uv、pytest);httpx(飞书 Webhook);React + TypeScript + TanStack Query/Router + shadcn/ui(Bun、Biome、Playwright)。

**Spec:** `docs/superpowers/specs/2026-10-14-automated-rules-design.md`;调研依据 `docs/integrations/2026-10-13-tiktok-automated-rules-research.md`。

## Global Constraints

- 规则作用域是整个租户;账户可跨 BC;所有表带 `tenant_id`,所有查询按租户过滤;越权租户读不到任何规则、运行或命中。
- 时间周期按各账户自身时区取本地日期。「最近 N 天」= 含今天在内的 N 个本地日 `[today-N+1, today]`;「投放以来」= 报表库中该对象的全部日数据。
- 合计口径下比率指标用分子、分母分别求和再相除;分母为 0 或为空时该条件不满足。
- 不同币种的数据不相加:「每部剧合计」「所有对象合计」按币种分组判断。
- 阶段 1 只接受执行模式 `NOTIFY`;`AUTO`、`CONFIRM` 由模型识别,保存时返回 `automation_mode_unavailable`(409),阶段 2 开放。
- 阶段 1 通知只支持「每次运行都发」;「固定时间汇总」阶段 3 开放。
- 素材层级的规则只支持「每个对象」口径(素材报表无法拆分到剧);「每部剧合计」「所有对象合计」要求应用层级为系列 / 广告组 / 广告。执行层级仍可选素材。对设计第 3.2 节的补充决定。
- 素材层级可用收益指标以报表指标字典为准:总收益/ROAS、第 0、6、13 天(素材报表无第 2、29 天)。对设计第 3.4 节表格的更正。
- 部署开关 `AUTOMATED_RULES_ENABLED`(默认 false)在本阶段引入,控制 Beat 是否调度规则运行与飞书发送;试运行不受限。按 AGENTS.md 同一提交补齐 Settings、`.env.example`、compose、k8s 示例、发布工具 `FLAGS`、部署手册。
- 不新增 TikTok 调用;不做真实广告写入;飞书只在传输边界用 `httpx.MockTransport` 替身。
- 飞书 Webhook 只接受 `https://open.feishu.cn/open-apis/bot/v2/hook/<token>` 或 `https://open.larksuite.com/open-apis/bot/v2/hook/<token>`;Webhook 与签名密钥用 `core/credentials.py` 加密存储;API 永不返回明文。
- 中文注释说明业务规则;Python 四空格、TS 两空格;前端只用 shadcn/ui,遵循现行视觉规范。
- Git:只 `git add` 明确路径;每个任务一次提交,提交信息结尾 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;不推送,不建分支。
- 后端测试使用专用测试库:`source /tmp/arres/testenv.sh` 后在 `backend/` 执行 `uv run --frozen pytest ...`。

## Review Focus

1. **同一账户同时属于两个 BC**(`sync_seed` 中 acc-2 在 bc-a、bc-b):展开范围后只出现一次,剧合计与全部合计不重复计数。Task 5 测试 `test_account_in_two_bcs_counted_once`。
2. **命名不规范的系列**:不参与「每部剧合计」,计入运行记录 `skipped_count`,不会被归到名为空的剧。Task 6 测试 `test_drama_skips_invalid_naming`。
3. **合计分母为 0**(合计消耗为 0 时的 ROAS):条件不满足,不抛异常、不产生无穷大。Task 6 测试 `test_zero_denominator_is_not_a_hit`。
4. **混合币种**:USD 与其他币种账户在「所有对象合计」下分成两个判断对象。Task 6 测试 `test_all_objects_split_by_currency`。
5. **Webhook 指向非飞书域名或带重定向**:保存与测试都拒绝;读取接口不出现 Webhook 明文和密钥。Task 3 测试 `test_rejects_non_feishu_hosts`,Task 4 测试 `test_settings_never_echo_secrets`。

---

## 文件结构

**后端 `backend/app/modules/automation_rules/`**

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 空 |
| `catalog.py` | 规则指标目录(基于报表指标字典 + 对象属性)、层级与口径合法性、执行层级合法性 |
| `schemas.py` | `RuleDefinition` 及子模型、API 请求/响应模型 |
| `models.py` | 5 张表的 SQLModel |
| `feishu.py` | Webhook 校验、签名、卡片构建、发送 |
| `settings_service.py` | 租户自动规则设置与飞书配置读写 |
| `inputs.py` | 范围展开、对象目录、报表事实、新鲜度加载 |
| `evaluate.py` | 纯函数判断引擎 |
| `schedule.py` | 纯函数排期计算 |
| `service.py` | 规则增删改查、试运行 |
| `runner.py` | 一次运行的执行与持久化 |
| `tasks.py` | Celery 任务:扫描到期、执行运行、发送飞书、清理 |
| `api.py` | 路由 |

**后端其他修改**:`app/modules/reporting/metric_catalog.py`(新增 `DERIVED_RATIOS`)、`app/modules/reporting/dimension_query.py`(改用 `DERIVED_RATIOS`)、`app/alembic/versions/0026_automation_rules.py`、`app/alembic/env.py`、`app/api/main.py`、`app/core/errors.py`、`app/core/config.py`、`app/jobs/celery_app.py`。

**测试 `backend/tests/modules/automation_rules/`**:`__init__.py`、`conftest.py`、`test_definition.py`、`test_models.py`、`test_feishu.py`、`test_settings.py`、`test_inputs.py`、`test_evaluate.py`、`test_schedule.py`、`test_api.py`、`test_runner.py`、`test_tasks.py`。

**前端 `frontend/src/features/automation-rules/`**:`queries.ts`、`ruleForm.ts`、`AutomationRulesPage.tsx`、`RuleList.tsx`、`RuleEditorSheet.tsx`、`ConditionRows.tsx`、`DryRunPanel.tsx`、`RunHistory.tsx`、`NotificationSettingsPage.tsx`;路由 `src/routes/_layout/automation-rules.tsx`、`tenants.$tenantId.automation-rules.tsx`、`tenants.$tenantId.notifications.tsx`;侧边栏 `src/routes/_layout.tsx`、`src/components/Sidebar/AppSidebar.tsx`;测试 `tests/automation-rule-form.spec.ts`、`tests/automation-rules.spec.ts`、`tests/utils/automationBoundary.ts`。

**部署与文档**:`.env.example`、`compose.yml`、`compose.production.yml`、`deploy/k8s/runtime-secret.example.env`、`deploy/k8s/README.md`、`deploy/release/common.py`、`deploy/k8s/operations.py`、`scripts/check-local-deployment.py`、`scripts/test-k8s-deployment.py`、`scripts/test-release-integration.py`(只在它们枚举 `ADS_SYNC_ENABLED` 的位置并列加入新开关)、`docs/runbooks/deployment.md`、`AGENTS.md`(开关清单)、`docs/implementation-progress.md`。

---

### Task 1: 规则指标目录与规则定义模型

**Files:**
- Modify: `backend/app/modules/reporting/metric_catalog.py`(文件末尾新增 `DERIVED_RATIOS`)
- Modify: `backend/app/modules/reporting/dimension_query.py:62-72`(改用 `DERIVED_RATIOS`)
- Create: `backend/app/modules/automation_rules/__init__.py`、`catalog.py`、`schemas.py`
- Test: `backend/tests/modules/automation_rules/__init__.py`、`test_definition.py`

**Interfaces:**
- Produces: `metric_catalog.DERIVED_RATIOS: tuple[tuple[str, str, str, int], ...]`(派生键、分子、分母、倍数)。
- Produces: `catalog.RuleMetric`(`key, group, unit, kind: "fact"|"attribute", levels: frozenset[str], aggregations: frozenset[str], operators: tuple[str, ...], ranged: bool`)、`catalog.RULE_METRICS: dict[str, RuleMetric]`、`catalog.allowed_action_levels(aggregation, apply_level, action_type) -> tuple[str, ...]`、`catalog.min_frequency_minutes(apply_level) -> int`。
- Produces: `schemas.RuleDefinition` 与子模型 `AccountScope, ApplyTo, Condition, Adjustment, Frequency, Action, Period, Schedule, NotificationSetting`;校验失败抛 `pydantic.ValidationError`。

- [ ] **Step 1: 写失败测试** `backend/tests/modules/automation_rules/test_definition.py`

```python
import pytest
from pydantic import ValidationError

from app.modules.automation_rules.catalog import (
    RULE_METRICS,
    allowed_action_levels,
)
from app.modules.automation_rules.schemas import RuleDefinition


def base(**overrides):
    value = {
        "accounts": {"mode": "ALL"},
        "apply_to": {"level": "adgroup", "status": "ACTIVE"},
        "aggregation": "EACH",
        "conditions": [
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["10"]}
        ],
        "action": {"type": "PAUSE", "level": "adgroup"},
        "mode": "NOTIFY",
        "schedule": {"frequency": "30M", "window": "CONTINUOUS"},
    }
    value.update(overrides)
    return value


def test_valid_definition_defaults():
    rule = RuleDefinition.model_validate(base())
    assert rule.max_targets == 200
    assert rule.notification.enabled is True


def test_material_level_lacks_day2_revenue():
    assert "material" not in RULE_METRICS["roas_d3"].levels
    assert "material" in RULE_METRICS["roas_d7"].levels


@pytest.mark.parametrize(
    "patch",
    [
        {"conditions": []},
        {"conditions": [base()["conditions"][0]] * 6},
        {"conditions": [{"metric": "nope", "range": "TODAY", "operator": "GT", "values": ["1"]}]},
        {"conditions": [{"metric": "spend", "range": "TODAY", "operator": "BETWEEN", "values": ["1"]}]},
        {"conditions": [{"metric": "spend", "range": "TODAY", "operator": "BETWEEN", "values": ["5", "1"]}]},
        {"conditions": [{"metric": "spend", "operator": "GT", "values": ["1"]}]},
        {"conditions": [{"metric": "name", "operator": "GT", "values": ["1"]}]},
        {"aggregation": "DRAMA", "conditions": [{"metric": "budget", "operator": "GT", "values": ["1"]}]},
        {"apply_to": {"level": "material", "status": "ACTIVE"}, "aggregation": "DRAMA"},
        {"action": {"type": "BUDGET", "level": "ad", "adjustment": {"mode": "SET", "value_type": "AMOUNT", "value": "50"}}},
        {"action": {"type": "PAUSE", "level": "campaign"}},
        {"action": {"type": "BUDGET", "level": "adgroup", "adjustment": {"mode": "INCREASE", "value_type": "PERCENT", "value": "20"}}},
        {"apply_to": {"level": "material", "status": "ACTIVE"}, "action": {"type": "PAUSE", "level": "material"}},
        {"apply_to": {"level": "adgroup", "status": "SELECTED"}},
        {"accounts": {"mode": "BC"}},
        {"schedule": {"frequency": "DAILY", "window": "CONTINUOUS"}},
        {"schedule": {"frequency": "30M", "window": "ONCE"}},
        {"schedule": {"frequency": "30M", "window": "CUSTOM", "periods": [{"date_type": "WEEK", "days": [], "start": "09:00", "end": "18:00"}]}},
    ],
)
def test_invalid_definitions(patch):
    with pytest.raises(ValidationError):
        RuleDefinition.model_validate(base(**patch))


def test_action_levels_follow_judge_scope():
    assert allowed_action_levels("EACH", "adgroup", "PAUSE") == ("adgroup", "ad", "material")
    assert allowed_action_levels("DRAMA", "adgroup", "PAUSE") == ("campaign", "adgroup", "ad", "material")
    assert allowed_action_levels("EACH", "ad", "BUDGET") == ()
    assert allowed_action_levels("EACH", "campaign", "ROAS") == ("campaign",)
```

说明:`{"apply_to": material, action PAUSE material}` 失败是因为素材层级排期下限 60 分钟,而 `base()` 用 30M;`{"action": PAUSE campaign}` 失败是因为 EACH 广告组不能执行到系列。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && source /tmp/arres/testenv.sh && uv run --frozen pytest tests/modules/automation_rules/test_definition.py -q`
Expected: FAIL,`ModuleNotFoundError: app.modules.automation_rules`

- [ ] **Step 3: 抽出派生比率**

在 `metric_catalog.py` 末尾加入:

```python
# 派生比率:由同口径合计的分子、分母重算,报表与自动规则共用,不平均各行比率。
DERIVED_RATIOS: tuple[tuple[str, str, str, int], ...] = (
    *(
        (f"roas_{d}", f"revenue_{d}", "spend", 1)
        for d in ("d0", "d3", "d7", "d14", "d30", "total")
    ),
    ("ctr", "clicks", "impressions", 1),
    ("cpm", "spend", "impressions", 1000),
    ("cpc", "spend", "clicks", 1),
    ("cost_per_ad_impression_event", "spend", "ad_impression_events", 1),
)
```

`dimension_query.py` 中 `for name, numerator, denominator, scale in [ ... ]:` 改为 `for name, numerator, denominator, scale in DERIVED_RATIOS:`,并从 `metric_catalog` 导入;删除原内联列表及其注释(注释移到常量上方已完成)。

- [ ] **Step 4: 实现 `catalog.py`**

```python
"""自动规则可用的指标与层级规则。

指标只来自 TK-ADA 已同步的报表事实(报表指标字典)和对象目录属性;
不提供平台有、但我们没有同步的指标。
"""

from dataclasses import dataclass
from typing import Literal

from app.modules.reporting.metric_catalog import METRIC_CATALOG

Level = Literal["campaign", "adgroup", "ad", "material"]
Aggregation = Literal["EACH", "DRAMA", "ALL"]
ActionType = Literal["PAUSE", "ENABLE", "BUDGET", "ROAS"]
LEVELS: tuple[Level, ...] = ("campaign", "adgroup", "ad", "material")
NUMERIC_OPERATORS = ("GT", "LT", "BETWEEN", "EQ")
TEXT_OPERATORS = ("CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH", "EQ")
RANGES = ("TODAY", "YESTERDAY", "PAST_3_DAYS", "PAST_5_DAYS", "PAST_7_DAYS", "LIFETIME")


@dataclass(frozen=True)
class RuleMetric:
    key: str
    group: Literal["delivery", "revenue", "attribute"]
    unit: str
    kind: Literal["fact", "attribute"]
    levels: frozenset[str]
    aggregations: frozenset[str]
    operators: tuple[str, ...]
    ranged: bool


_DELIVERY = {"spend", "impressions", "clicks", "ctr", "cpm", "cpc"}


def _fact_metrics() -> dict[str, RuleMetric]:
    result = {}
    for item in METRIC_CATALOG:
        levels = {"campaign", "adgroup", "ad"}
        # 素材报表缺少的天数(第 2、29 天)在素材层级不可选。
        if item.material_field is not None:
            levels.add("material")
        result[item.key] = RuleMetric(
            key=item.key,
            group="delivery" if item.key in _DELIVERY else "revenue",
            unit=item.unit,
            kind="fact",
            levels=frozenset(levels),
            aggregations=frozenset({"EACH", "DRAMA", "ALL"}),
            operators=NUMERIC_OPERATORS,
            ranged=True,
        )
    return result


def _attributes() -> dict[str, RuleMetric]:
    def attr(key, unit, levels, operators):
        # 属性描述单个对象,合计口径下没有意义,只允许「每个对象」。
        return RuleMetric(key, "attribute", unit, "attribute", frozenset(levels),
                          frozenset({"EACH"}), operators, False)

    return {
        "name": attr("name", "text", LEVELS, TEXT_OPERATORS),
        "days_since_creation": attr("days_since_creation", "day", ("campaign", "adgroup", "ad"), NUMERIC_OPERATORS),
        "budget": attr("budget", "money", ("campaign", "adgroup"), NUMERIC_OPERATORS),
        "roas_bid": attr("roas_bid", "multiple", ("campaign", "adgroup"), NUMERIC_OPERATORS),
    }


RULE_METRICS: dict[str, RuleMetric] = {**_fact_metrics(), **_attributes()}

_DEPTH = {level: index for index, level in enumerate(LEVELS)}
# 各操作支持的执行层级;ROAS 以广告管理现有 roas 变更为准(系列)。
ACTION_LEVELS: dict[str, tuple[str, ...]] = {
    "PAUSE": LEVELS,
    "ENABLE": LEVELS,
    "BUDGET": ("campaign", "adgroup"),
    "ROAS": ("campaign",),
}


def allowed_action_levels(aggregation: str, apply_level: str, action_type: str) -> tuple[str, ...]:
    """执行层级必须在判断对象范围内:合计口径不限,单对象口径只能是自身或下级。"""
    supported = ACTION_LEVELS[action_type]
    if aggregation != "EACH":
        return supported
    return tuple(level for level in supported if _DEPTH[level] >= _DEPTH[apply_level])


def min_frequency_minutes(apply_level: str, action_level: str) -> int:
    # 素材报表 60 分钟同步一次,更频繁的检查只会看到同一份数据。
    return 60 if "material" in (apply_level, action_level) else 30
```

- [ ] **Step 5: 实现 `schemas.py`(定义部分)**

```python
"""自动规则定义与接口模型。定义整体存 JSONB,每次运行保存版本快照。"""

from datetime import datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.automation_rules.catalog import (
    RANGES,
    RULE_METRICS,
    allowed_action_levels,
    min_frequency_minutes,
)

Level = Literal["campaign", "adgroup", "ad", "material"]
FREQUENCY_MINUTES = {"30M": 30, "1H": 60, "2H": 120, "4H": 240, "DAILY": 1440}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountScope(_Strict):
    mode: Literal["ALL", "BC", "ACCOUNTS"]
    bc_ids: tuple[str, ...] = ()
    advertiser_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check(self):
        if self.mode == "BC" and not self.bc_ids:
            raise ValueError("bc_ids required")
        if self.mode == "ACCOUNTS" and not self.advertiser_ids:
            raise ValueError("advertiser_ids required")
        return self


class ApplyTo(_Strict):
    level: Level
    status: Literal["ACTIVE", "PAUSED", "SELECTED"]
    # 指定对象:"账户ID:远端ID";素材为"账户ID:主素材ID"。
    object_ids: tuple[str, ...] = ()
    drama_names: tuple[str, ...] = ()
    name_contains: str | None = Field(default=None, max_length=200)
    name_excludes: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def check(self):
        if self.status == "SELECTED" and not self.object_ids:
            raise ValueError("object_ids required")
        return self


class Condition(_Strict):
    metric: str
    range: Literal["TODAY", "YESTERDAY", "PAST_3_DAYS", "PAST_5_DAYS", "PAST_7_DAYS", "LIFETIME"] | None = None
    operator: Literal["GT", "LT", "BETWEEN", "EQ", "CONTAINS", "NOT_CONTAINS", "STARTS_WITH", "ENDS_WITH"]
    values: tuple[str, ...] = Field(min_length=1, max_length=2)


class Adjustment(_Strict):
    mode: Literal["INCREASE", "DECREASE", "SET"]
    value_type: Literal["AMOUNT", "PERCENT"]
    value: Decimal = Field(gt=0)
    limit: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check(self):
        # 提高必须设上限、降低必须设下限,防止预算或出价失控。
        if self.mode != "SET" and self.limit is None:
            raise ValueError("limit required")
        if self.mode == "SET" and (self.value_type != "AMOUNT" or self.limit is not None):
            raise ValueError("SET takes an absolute value without limit")
        return self


class Frequency(_Strict):
    type: Literal["ONCE", "EVERY_24H", "EVERY_48H", "EVERY_WEEK", "CUSTOM"]
    window: int | None = Field(default=None, ge=1, le=10000)
    unit: Literal["MINUTE", "HOUR", "DAY"] | None = None
    count: int | None = Field(default=None, ge=1, le=1000)

    @model_validator(mode="after")
    def check(self):
        custom = (self.window, self.unit, self.count)
        if (self.type == "CUSTOM") != all(v is not None for v in custom):
            raise ValueError("custom frequency needs window, unit and count")
        return self


class Action(_Strict):
    type: Literal["PAUSE", "ENABLE", "BUDGET", "ROAS"]
    level: Level
    adjustment: Adjustment | None = None
    frequency: Frequency | None = None

    @model_validator(mode="after")
    def check(self):
        adjusts = self.type in ("BUDGET", "ROAS")
        if adjusts != (self.adjustment is not None) or adjusts != (self.frequency is not None):
            raise ValueError("budget/roas need adjustment and frequency; pause/enable take none")
        return self


class Period(_Strict):
    date_type: Literal["DAY", "WEEK", "MONTH"]
    days: tuple[int, ...] = ()
    start: time
    end: time

    @model_validator(mode="after")
    def check(self):
        limit = {"DAY": 0, "WEEK": 7, "MONTH": 28}[self.date_type]
        if self.date_type != "DAY" and not self.days:
            raise ValueError("days required")
        if any(not 1 <= day <= limit for day in self.days) or self.start > self.end:
            raise ValueError("invalid period")
        return self


class Schedule(_Strict):
    frequency: Literal["30M", "1H", "2H", "4H", "DAILY"]
    daily_at: time | None = None
    window: Literal["CONTINUOUS", "CUSTOM", "ONCE"]
    periods: tuple[Period, ...] = Field(default=(), max_length=7)
    once_at: datetime | None = None

    @model_validator(mode="after")
    def check(self):
        if (self.frequency == "DAILY") != (self.daily_at is not None):
            raise ValueError("daily_at only and always with DAILY")
        if (self.window == "CUSTOM") != bool(self.periods):
            raise ValueError("periods only and always with CUSTOM")
        if (self.window == "ONCE") != (self.once_at is not None):
            raise ValueError("once_at only and always with ONCE")
        if self.once_at is not None and self.once_at.tzinfo is None:
            raise ValueError("once_at must carry a timezone")
        return self


class NotificationSetting(_Strict):
    enabled: bool = True
    timing: Literal["EVERY_RUN"] = "EVERY_RUN"
    notify_without_hits: bool = False


def _check_condition(condition: Condition, *, level: str, aggregation: str) -> None:
    metric = RULE_METRICS.get(condition.metric)
    if metric is None:
        raise ValueError(f"unknown metric {condition.metric}")
    if level not in metric.levels or aggregation not in metric.aggregations:
        raise ValueError(f"{condition.metric} unavailable here")
    if condition.operator not in metric.operators:
        raise ValueError(f"{condition.operator} unavailable for {condition.metric}")
    if metric.ranged != (condition.range is not None):
        raise ValueError("range required for facts and forbidden for attributes")
    if (condition.operator == "BETWEEN") != (len(condition.values) == 2):
        raise ValueError("BETWEEN takes exactly two values")
    if metric.kind == "fact" or metric.key != "name":
        try:
            numbers = [Decimal(v) for v in condition.values]
        except ArithmeticError as exc:
            raise ValueError("numeric values required") from exc
        if any(not n.is_finite() or n < 0 for n in numbers):
            raise ValueError("values must be finite and non-negative")
        if len(numbers) == 2 and numbers[0] >= numbers[1]:
            raise ValueError("BETWEEN lower bound must be below upper bound")
    elif not all(0 < len(v) <= 512 for v in condition.values):
        raise ValueError("name values must be 1-512 characters")


class RuleDefinition(_Strict):
    accounts: AccountScope
    apply_to: ApplyTo
    aggregation: Literal["EACH", "DRAMA", "ALL"]
    conditions: tuple[Condition, ...] = Field(min_length=1, max_length=5)
    action: Action
    mode: Literal["AUTO", "CONFIRM", "NOTIFY"]
    max_targets: int = Field(default=200, ge=1, le=5000)
    schedule: Schedule
    notification: NotificationSetting = NotificationSetting()

    @model_validator(mode="after")
    def check(self):
        level = self.apply_to.level
        if level == "material" and self.aggregation != "EACH":
            raise ValueError("material rules judge each material")
        for condition in self.conditions:
            _check_condition(condition, level=level, aggregation=self.aggregation)
        if self.action.level not in allowed_action_levels(self.aggregation, level, self.action.type):
            raise ValueError("action level outside the judged scope")
        minimum = min_frequency_minutes(level, self.action.level)
        if FREQUENCY_MINUTES[self.schedule.frequency] < minimum:
            raise ValueError(f"run at most every {minimum} minutes")
        return self
```

确认 `RANGES` 导入后如未使用则删除该导入(Ruff 会提示)。

- [ ] **Step 6: 运行测试**

Run: `uv run --frozen pytest tests/modules/automation_rules/test_definition.py tests/modules/reporting/test_dimension_query.py tests/modules/reporting/test_metric_catalog.py -q`
Expected: 全部 PASS

- [ ] **Step 7: 静态检查并提交**

```bash
uv run --frozen ruff check app/modules/automation_rules app/modules/reporting tests/modules/automation_rules
uv run --frozen ruff format app/modules/automation_rules tests/modules/automation_rules
git add backend/app/modules/automation_rules/__init__.py backend/app/modules/automation_rules/catalog.py backend/app/modules/automation_rules/schemas.py backend/app/modules/reporting/metric_catalog.py backend/app/modules/reporting/dimension_query.py backend/tests/modules/automation_rules/__init__.py backend/tests/modules/automation_rules/test_definition.py
git commit -m "automation-rules: 规则定义模型与指标目录"
```

---

### Task 2: 数据表与迁移

**Files:**
- Create: `backend/app/modules/automation_rules/models.py`、`backend/app/alembic/versions/0026_automation_rules.py`
- Modify: `backend/app/alembic/env.py`(导入 models)、`backend/app/core/errors.py`(错误码)
- Test: `backend/tests/modules/automation_rules/test_models.py`

**Interfaces:**
- Produces 表模型:`AutomationRule`、`AutomationRuleRun`、`AutomationRuleHit`、`AutomationTenantSetting`、`TenantFeishuSetting`(字段见下)。
- Produces 错误码(`ERROR_HTTP_STATUS`):`automation_rule_invalid` 422、`automation_rule_not_found` 404、`automation_rule_name_conflict` 409、`automation_rule_version_conflict` 409、`automation_mode_unavailable` 409、`automation_run_not_found` 404、`feishu_webhook_invalid` 422、`feishu_not_configured` 409、`feishu_send_failed` 502。

- [ ] **Step 1: 写失败测试** `test_models.py`

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
from tests.modules.conftest import create_context


def rule(context, name="规则"):
    return AutomationRule(
        tenant_id=context.tenant_id, name=name, definition={"x": 1},
        created_by=context.actor_id, updated_by=context.actor_id,
    )


def test_rule_name_unique_per_tenant(session):
    a, b = create_context(session), create_context(session)
    session.add_all([rule(a), rule(b)])
    session.flush()
    session.add(rule(a))
    with pytest.raises(IntegrityError):
        session.flush()


def test_one_run_per_slot(session):
    context = create_context(session)
    item = rule(context)
    session.add(item)
    session.flush()
    slot = datetime(2026, 10, 14, 1, 0, tzinfo=UTC)
    for _ in range(2):
        session.add(AutomationRuleRun(
            tenant_id=context.tenant_id, rule_id=item.id, rule_version=1,
            definition={}, slot_at=slot,
        ))
    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 2: 运行确认失败**(`ModuleNotFoundError`)

- [ ] **Step 3: 实现 `models.py`**

```python
"""自动规则持久化:规则、每次运行、命中对象、租户设置、飞书配置。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime


def _now() -> datetime:
    return datetime.now(UTC)


class AutomationRule(SQLModel, table=True):
    __tablename__ = "automation_rule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_automation_rule_name"),
        Index("ix_automation_rule_due", "enabled", "next_run_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=100)
    enabled: bool = True
    version: int = 1
    definition: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    created_by: UUID = Field(foreign_key="user.id")
    updated_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    next_run_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    last_run_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class AutomationRuleRun(SQLModel, table=True):
    __tablename__ = "automation_rule_run"
    __table_args__ = (
        UniqueConstraint("rule_id", "slot_at", name="uq_automation_run_slot"),
        Index("ix_automation_run_tenant_time", "tenant_id", "started_at"),
        CheckConstraint("status IN ('RUNNING','COMPLETE','FAILED')", name="ck_automation_run_status"),
        CheckConstraint(
            "notify_status IN ('NOT_REQUIRED','PENDING','SENT','FAILED')",
            name="ck_automation_run_notify",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    rule_id: UUID = Field(foreign_key="automation_rule.id", ondelete="CASCADE")
    rule_version: int
    # 运行所用的规则定义快照;规则后续修改不影响历史记录解释。
    definition: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    slot_at: datetime = Field(sa_type=AwareDateTime)
    status: str = Field(default="RUNNING", max_length=16)
    started_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    checked_count: int = 0
    hit_count: int = 0
    skipped_count: int = 0
    skipped_accounts: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = Field(default=None, max_length=64)
    notify_status: str = Field(default="NOT_REQUIRED", max_length=16)
    notify_error: str | None = Field(default=None, max_length=500)


class AutomationRuleHit(SQLModel, table=True):
    __tablename__ = "automation_rule_hit"
    __table_args__ = (Index("ix_automation_hit_run", "run_id", "sequence"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    run_id: UUID = Field(foreign_key="automation_rule_run.id", ondelete="CASCADE")
    sequence: int
    subject_key: str = Field(max_length=300)
    subject_label: str = Field(max_length=500)
    drama_name: str | None = Field(default=None, max_length=300)
    advertiser_id: str | None = Field(default=None, max_length=128)
    bc_id: str | None = Field(default=None, max_length=128)
    currency: str | None = Field(default=None, max_length=16)
    metrics: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    targets: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    outcome: str = Field(default="NOTIFIED", max_length=32)


class AutomationTenantSetting(SQLModel, table=True):
    __tablename__ = "automation_tenant_setting"
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    # 自定义运行时段按租户时区解释;各账户指标仍按各自账户时区取日期。
    schedule_timezone: str = Field(default="Asia/Shanghai", max_length=64)
    paused: bool = False
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)


class TenantFeishuSetting(SQLModel, table=True):
    __tablename__ = "tenant_feishu_setting"
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    # 加密信封内为 {"url": Webhook, "secret": 签名密钥(可空串)}。
    ciphertext: str
    url_hint: str = Field(max_length=120)
    has_secret: bool = False
    enabled: bool = True
    updated_by: UUID = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
```

- [ ] **Step 4: 写迁移 `0026_automation_rules.py`**

`revision = "0026_automation_rules"`,`down_revision = "0025_drop_campaign_day_utc"`。`upgrade()` 用 `op.create_table` 按上面字段逐列建表(`sa.Uuid`、`sa.String(n)`、`sa.Boolean`、`sa.Integer`、`postgresql.JSONB`、`sa.DateTime(timezone=True)`、`sa.Text`),外键与约束名与模型一致,`automation_rule_run.rule_id`、`automation_rule_hit.run_id` 外键 `ondelete="CASCADE"`;布尔、整数列给 `server_default`(`sa.false()`/`sa.true()`/`"0"`/`"1"`),`status` 默认 `'RUNNING'`、`notify_status` 默认 `'NOT_REQUIRED'`、`outcome` 默认 `'NOTIFIED'`、`schedule_timezone` 默认 `'Asia/Shanghai'`、`skipped_accounts` 默认 `'[]'::jsonb`;建两个索引。`downgrade()` 逆序 `op.drop_table`。在 `env.py` 的模型导入区加入:

```python
from app.modules.automation_rules import models as automation_rules_models  # noqa: E402, F401
```

- [ ] **Step 5: 注册错误码**:在 `ERROR_HTTP_STATUS` 中按上面列表加入 9 个键。

- [ ] **Step 6: 运行测试**

Run: `uv run --frozen pytest tests/modules/automation_rules/test_models.py tests/test_migration_graph.py -q`
Expected: PASS。再执行模型与迁移一致性检查:

Run: `uv run --frozen alembic check`
Expected: `No new upgrade operations detected.`(若报差异,按差异修正迁移列定义后重跑)

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/automation_rules/models.py backend/app/alembic/versions/0026_automation_rules.py backend/app/alembic/env.py backend/app/core/errors.py backend/tests/modules/automation_rules/test_models.py
git commit -m "automation-rules: 规则、运行、命中与租户设置数据表"
```

---

### Task 3: 飞书 Webhook 客户端与卡片

**Files:**
- Create: `backend/app/modules/automation_rules/feishu.py`
- Test: `backend/tests/modules/automation_rules/test_feishu.py`

**Interfaces:**
- Produces: `validate_webhook(url: str) -> str`(合法则返回去空白的 URL,否则 `DomainError("feishu_webhook_invalid")`);`url_hint(url) -> str`;`sign(secret: str, timestamp: int) -> str`;`CardHit`(`label: str, drama: str | None, metrics: list[tuple[str, str]], target_count: int`);`build_card(*, title, rule_name, summary_lines: list[str], hits: list[CardHit], total_hits: int, link: str, template: str = "orange") -> dict`;`send(url, secret, card, *, transport=None, now=None) -> None`(失败抛 `DomainError("feishu_send_failed")`)。

- [ ] **Step 1: 写失败测试**

```python
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from app.core.errors import DomainError
from app.modules.automation_rules.feishu import (
    CardHit, build_card, send, sign, url_hint, validate_webhook,
)

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc-123"


@pytest.mark.parametrize("url", [
    "http://open.feishu.cn/open-apis/bot/v2/hook/abc",
    "https://open.feishu.cn.evil.com/open-apis/bot/v2/hook/abc",
    "https://evil.com/?u=https://open.feishu.cn/open-apis/bot/v2/hook/abc",
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://user@open.feishu.cn/open-apis/bot/v2/hook/abc",
    "https://open.feishu.cn:8443/open-apis/bot/v2/hook/abc",
])
def test_rejects_non_feishu_hosts(url):
    with pytest.raises(DomainError):
        validate_webhook(url)


def test_accepts_feishu_and_lark():
    assert validate_webhook(f" {HOOK} ") == HOOK
    validate_webhook("https://open.larksuite.com/open-apis/bot/v2/hook/x1")


def test_hint_hides_token():
    assert "abc-123" not in url_hint(HOOK)


def test_sign_matches_feishu_algorithm():
    expected = base64.b64encode(
        hmac.new(b"1700000000\nsec", b"", hashlib.sha256).digest()
    ).decode()
    assert sign("sec", 1700000000) == expected


def test_card_truncates_to_ten_hits_and_links():
    hits = [CardHit(f"对象{i}", "剧", [("消耗", "1.00")], 2) for i in range(12)]
    card = build_card(title="预警", rule_name="r", summary_lines=["命中 12"],
                      hits=hits, total_hits=12, link="https://x/y")
    text = json.dumps(card, ensure_ascii=False)
    assert "对象9" in text and "对象10" not in text
    assert "https://x/y" in text and "其余 2 个" in text
    assert len(text.encode()) < 20 * 1024


def test_send_posts_signed_body_without_redirects():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0})

    send(HOOK, "sec", {"msg_type": "interactive", "card": {}},
         transport=httpx.MockTransport(handler), now=1700000000)
    assert seen["body"]["timestamp"] == "1700000000"
    assert seen["body"]["sign"] == sign("sec", 1700000000)


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 19021, "msg": "sign match fail"}),
    httpx.Response(302, headers={"location": "https://evil.com"}),
    httpx.Response(500),
])
def test_send_failures_raise(response):
    with pytest.raises(DomainError):
        send(HOOK, "", {"msg_type": "interactive", "card": {}},
             transport=httpx.MockTransport(lambda r: response))
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `feishu.py`**

```python
"""租户飞书群自定义机器人:只允许官方 Webhook 域名,不跟随重定向。"""

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

import httpx

from app.core.errors import DomainError

_HOOK = re.compile(
    r"^https://open\.(feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9-]+$"
)
MAX_CARD_HITS = 10
MAX_CARD_BYTES = 20 * 1024


def validate_webhook(url: str) -> str:
    value = (url or "").strip()
    if not _HOOK.fullmatch(value):
        raise DomainError("feishu_webhook_invalid", "请填写飞书群机器人的 Webhook 地址")
    return value


def url_hint(url: str) -> str:
    token = url.rsplit("/", 1)[-1]
    return f"{url[: -len(token)]}…{token[-4:]}"


def sign(secret: str, timestamp: int) -> str:
    # 飞书签名:以 "timestamp\nsecret" 为密钥对空消息做 HmacSHA256 后 Base64。
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


@dataclass(frozen=True)
class CardHit:
    label: str
    drama: str | None
    metrics: list[tuple[str, str]]
    target_count: int


def _hit_line(hit: CardHit) -> str:
    metrics = ",".join(f"{name} {value}" for name, value in hit.metrics)
    drama = f"【{hit.drama}】" if hit.drama else ""
    return f"- {drama}{hit.label}:{metrics}(涉及 {hit.target_count} 个对象)"


def build_card(*, title, rule_name, summary_lines, hits, total_hits, link, template="orange"):
    lines = [_hit_line(hit) for hit in hits[:MAX_CARD_HITS]]
    if total_hits > len(lines):
        lines.append(f"其余 {total_hits - len(lines)} 个请在 TK-ADA 查看")
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**规则**:{rule_name}\n" + "\n".join(summary_lines)}},
    ]
    if lines:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    elements.append({"tag": "action", "actions": [{
        "tag": "button", "type": "primary", "url": link,
        "text": {"tag": "plain_text", "content": "查看明细"},
    }]})
    card = {"msg_type": "interactive", "card": {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }}
    # 极端长名称下逐条减少明细,保证不超过飞书卡片体积上限。
    while len(json.dumps(card, ensure_ascii=False).encode()) >= MAX_CARD_BYTES and lines:
        lines.pop(-2 if len(lines) > 1 else -1)
        elements[1]["text"]["content"] = "\n".join(lines)
    return card


def send(url, secret, card, *, transport=None, now=None):
    body = dict(card)
    if secret:
        timestamp = int(now if now is not None else time.time())
        body.update(timestamp=str(timestamp), sign=sign(secret, timestamp))
    try:
        with httpx.Client(transport=transport, timeout=10, follow_redirects=False) as client:
            response = client.post(validate_webhook(url), json=body)
    except httpx.HTTPError:
        raise DomainError("feishu_send_failed", "飞书发送失败:网络错误") from None
    if response.status_code != 200:
        raise DomainError("feishu_send_failed", f"飞书发送失败:HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise DomainError("feishu_send_failed", "飞书发送失败:响应无效") from None
    code = data.get("code", data.get("StatusCode"))
    if code != 0:
        raise DomainError("feishu_send_failed", f"飞书发送失败:{data.get('msg') or code}")
```

- [ ] **Step 4: 运行测试** `pytest tests/modules/automation_rules/test_feishu.py -q` → PASS

- [ ] **Step 5: 提交** `git add backend/app/modules/automation_rules/feishu.py backend/tests/modules/automation_rules/test_feishu.py` 并提交 `automation-rules: 飞书 Webhook 签名发送与预警卡片`。

---

### Task 4: 租户设置与飞书配置接口

**Files:**
- Create: `backend/app/modules/automation_rules/settings_service.py`、`backend/app/modules/automation_rules/api.py`(先放设置路由)
- Modify: `backend/app/modules/automation_rules/schemas.py`(设置模型)、`backend/app/api/main.py`(挂载路由)
- Test: `backend/tests/modules/automation_rules/conftest.py`、`test_settings.py`

**Interfaces:**
- Produces: `schemas.AutomationSettingsPublic`(`schedule_timezone, paused, feishu_configured, feishu_enabled, feishu_url_hint: str | None, feishu_has_secret`)、`schemas.AutomationSettingsUpdate`(`schedule_timezone, paused, feishu_enabled, webhook_url: str | None = None, secret: str | None = None, clear_feishu: bool = False`;`webhook_url=None` 表示保留原值)、`schemas.FeishuTestRequest`(`webhook_url: str | None = None, secret: str | None = None`)。
- Produces: `settings_service.get_settings(session, *, context) -> AutomationSettingsPublic`、`update_settings(session, *, context, body) -> AutomationSettingsPublic`、`load_tenant_setting(session, tenant_id) -> AutomationTenantSetting`(无行时返回默认实例,不写库)、`load_feishu(session, tenant_id) -> tuple[str, str] | None`(启用时返回 url、secret)、`send_test(session, *, context, body, transport=None) -> None`。
- 路由(`router = APIRouter(prefix="/tenants/{tenant_id}", tags=["automation_rules"])`):`GET /automation-settings`(`read`)、`PUT /automation-settings`(`manage`)、`POST /automation-settings/test-feishu`(`manage`)。
- Produces 测试夹具(`conftest.py`):`headers(context) -> dict`;`admin_context(session)`、`viewer_context(session)` 用 `create_context(session, role=...)`。

- [ ] **Step 1: 写失败测试**

```python
# conftest.py
from datetime import timedelta

import pytest

from app.core.security import create_access_token
from tests.modules.conftest import create_context


def headers(context):
    token = create_access_token(context.actor_id, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_context(session):
    return create_context(session, role="tenant_admin")


@pytest.fixture
def viewer_context(session):
    return create_context(session, role="viewer")
```

```python
# test_settings.py
import httpx

from tests.modules.automation_rules.conftest import headers

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/token-9999"


def url(context):
    return f"/api/tenants/{context.tenant_id}/automation-settings"


def test_defaults(client, admin_context):
    body = client.get(url(admin_context), headers=headers(admin_context)).json()
    assert body == {
        "schedule_timezone": "Asia/Shanghai", "paused": False,
        "feishu_configured": False, "feishu_enabled": False,
        "feishu_url_hint": None, "feishu_has_secret": False,
    }


def test_settings_never_echo_secrets(client, admin_context):
    response = client.put(url(admin_context), headers=headers(admin_context), json={
        "schedule_timezone": "Asia/Shanghai", "paused": False, "feishu_enabled": True,
        "webhook_url": HOOK, "secret": "top-secret",
    })
    assert response.status_code == 200, response.text
    for text in (response.text, client.get(url(admin_context), headers=headers(admin_context)).text):
        assert "token-9999" not in text and "top-secret" not in text
    assert response.json()["feishu_has_secret"] is True


def test_keep_webhook_when_omitted(client, admin_context, session):
    from app.modules.automation_rules.settings_service import load_feishu
    client.put(url(admin_context), headers=headers(admin_context), json={
        "schedule_timezone": "Asia/Shanghai", "paused": False, "feishu_enabled": True,
        "webhook_url": HOOK})
    client.put(url(admin_context), headers=headers(admin_context), json={
        "schedule_timezone": "Asia/Hong_Kong", "paused": True, "feishu_enabled": True})
    assert load_feishu(session, admin_context.tenant_id) == (HOOK, "")


def test_invalid_timezone_and_webhook(client, admin_context):
    for patch in ({"schedule_timezone": "Mars/Base"}, {"webhook_url": "https://evil.com/x"}):
        body = {"schedule_timezone": "Asia/Shanghai", "paused": False, "feishu_enabled": True, **patch}
        assert client.put(url(admin_context), headers=headers(admin_context), json=body).status_code == 422


def test_viewer_cannot_write(client, viewer_context):
    body = {"schedule_timezone": "Asia/Shanghai", "paused": False, "feishu_enabled": False}
    assert client.put(url(viewer_context), headers=headers(viewer_context), json=body).status_code == 403


def test_other_tenant_isolated(client, admin_context, session):
    from tests.modules.conftest import create_context
    other = create_context(session, role="tenant_admin")
    response = client.get(url(admin_context), headers=headers(other))
    assert response.status_code == 403


def test_send_test_uses_given_webhook(session, admin_context):
    from app.modules.automation_rules.schemas import FeishuTestRequest
    from app.modules.automation_rules.settings_service import send_test
    calls = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={"code": 0}))
    send_test(session, context=admin_context, body=FeishuTestRequest(webhook_url=HOOK), transport=transport)
    assert len(calls) == 1
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现设置模型**(追加到 `schemas.py`)

```python
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import field_validator


class AutomationSettingsPublic(BaseModel):
    schedule_timezone: str
    paused: bool
    feishu_configured: bool
    feishu_enabled: bool
    feishu_url_hint: str | None
    feishu_has_secret: bool


class AutomationSettingsUpdate(_Strict):
    schedule_timezone: str = Field(max_length=64)
    paused: bool
    feishu_enabled: bool
    webhook_url: str | None = Field(default=None, max_length=300)
    secret: str | None = Field(default=None, max_length=200)
    clear_feishu: bool = False

    @field_validator("schedule_timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return value


class FeishuTestRequest(_Strict):
    webhook_url: str | None = Field(default=None, max_length=300)
    secret: str | None = Field(default=None, max_length=200)
```

- [ ] **Step 4: 实现 `settings_service.py`**

```python
"""租户自动规则设置与飞书配置;Webhook 与密钥只以加密信封落库。"""

from datetime import UTC, datetime

from sqlmodel import Session

from app.core.context import TenantContext
from app.core.credentials import decrypt_credentials, encrypt_credentials
from app.core.errors import DomainError
from app.modules.automation_rules import feishu
from app.modules.automation_rules.models import AutomationTenantSetting, TenantFeishuSetting
from app.modules.automation_rules.schemas import (
    AutomationSettingsPublic, AutomationSettingsUpdate, FeishuTestRequest,
)
from app.modules.tenants.permissions import require_tenant


def load_tenant_setting(session: Session, tenant_id) -> AutomationTenantSetting:
    return session.get(AutomationTenantSetting, tenant_id) or AutomationTenantSetting(tenant_id=tenant_id)


def load_feishu(session: Session, tenant_id) -> tuple[str, str] | None:
    row = session.get(TenantFeishuSetting, tenant_id)
    if row is None or not row.enabled:
        return None
    value = decrypt_credentials(tenant_id=tenant_id, ciphertext=row.ciphertext)
    return value["url"], value.get("secret", "")


def _public(session, tenant_id) -> AutomationSettingsPublic:
    setting = load_tenant_setting(session, tenant_id)
    row = session.get(TenantFeishuSetting, tenant_id)
    return AutomationSettingsPublic(
        schedule_timezone=setting.schedule_timezone, paused=setting.paused,
        feishu_configured=row is not None, feishu_enabled=bool(row and row.enabled),
        feishu_url_hint=row.url_hint if row else None,
        feishu_has_secret=bool(row and row.has_secret),
    )


def get_settings(session, *, context: TenantContext) -> AutomationSettingsPublic:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read")
    return _public(session, context.tenant_id)


def update_settings(session, *, context, body: AutomationSettingsUpdate) -> AutomationSettingsPublic:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage")
    now = datetime.now(UTC)
    setting = session.get(AutomationTenantSetting, context.tenant_id)
    if setting is None:
        setting = AutomationTenantSetting(tenant_id=context.tenant_id)
        session.add(setting)
    setting.schedule_timezone, setting.paused, setting.updated_at = body.schedule_timezone, body.paused, now
    row = session.get(TenantFeishuSetting, context.tenant_id)
    if body.clear_feishu:
        if row is not None:
            session.delete(row)
    elif body.webhook_url is not None:
        url = feishu.validate_webhook(body.webhook_url)
        secret = (body.secret or "").strip()
        ciphertext = encrypt_credentials(tenant_id=context.tenant_id, value={"url": url, "secret": secret})
        if row is None:
            row = TenantFeishuSetting(tenant_id=context.tenant_id, ciphertext=ciphertext,
                                      url_hint=feishu.url_hint(url), updated_by=context.actor_id)
            session.add(row)
        row.ciphertext, row.url_hint, row.has_secret = ciphertext, feishu.url_hint(url), bool(secret)
    if row is not None and not body.clear_feishu:
        row.enabled, row.updated_by, row.updated_at = body.feishu_enabled, context.actor_id, now
    session.flush()
    return _public(session, context.tenant_id)


def send_test(session, *, context, body: FeishuTestRequest, transport=None) -> None:
    require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="manage")
    if body.webhook_url:
        url, secret = feishu.validate_webhook(body.webhook_url), (body.secret or "").strip()
    else:
        row = session.get(TenantFeishuSetting, context.tenant_id)
        if row is None:
            raise DomainError("feishu_not_configured", "尚未配置飞书 Webhook")
        value = decrypt_credentials(tenant_id=context.tenant_id, ciphertext=row.ciphertext)
        url, secret = value["url"], value.get("secret", "")
    card = feishu.build_card(title="TK-ADA 自动规则测试消息", rule_name="测试",
                             summary_lines=["飞书通知配置可用。"], hits=[], total_hits=0,
                             link=_site_link(context.tenant_id), template="green")
    feishu.send(url, secret, card, transport=transport)


def _site_link(tenant_id) -> str:
    from app.core.config import settings
    return f"{settings.FRONTEND_HOST.rstrip('/')}/tenants/{tenant_id}/automation-rules"
```

- [ ] **Step 5: 实现路由并挂载**

`api.py`:

```python
"""自动规则 HTTP 接口。"""

from uuid import UUID

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.core.context import TenantContext
from app.modules.automation_rules import settings_service
from app.modules.automation_rules.schemas import (
    AutomationSettingsPublic, AutomationSettingsUpdate, FeishuTestRequest,
)

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["automation_rules"])


def _context(user: CurrentUser, tenant_id: UUID) -> TenantContext:
    # 权限在各服务函数内按动作重建;这里只组装请求范围。
    return TenantContext(tenant_id=tenant_id, actor_id=user.id, role="")


@router.get("/automation-settings", response_model=AutomationSettingsPublic)
def get_settings(tenant_id: UUID, session: SessionDep, user: CurrentUser):
    return settings_service.get_settings(session, context=_context(user, tenant_id))


@router.put("/automation-settings", response_model=AutomationSettingsPublic)
def put_settings(tenant_id: UUID, body: AutomationSettingsUpdate, session: SessionDep, user: CurrentUser):
    result = settings_service.update_settings(session, context=_context(user, tenant_id), body=body)
    session.commit()
    return result


@router.post("/automation-settings/test-feishu", status_code=204)
def test_feishu(tenant_id: UUID, body: FeishuTestRequest, session: SessionDep, user: CurrentUser):
    settings_service.send_test(session, context=_context(user, tenant_id), body=body)
```

`app/api/main.py`:`from app.modules.automation_rules.api import router as automation_rules_router`,并 `api_router.include_router(automation_rules_router)`。

- [ ] **Step 6: 运行测试** `pytest tests/modules/automation_rules/test_settings.py -q` → PASS

- [ ] **Step 7: 提交** 本任务全部文件,`automation-rules: 租户设置与飞书配置接口`。

---

### Task 5: 判断输入加载(范围、对象、事实、新鲜度)

**Files:**
- Create: `backend/app/modules/automation_rules/inputs.py`
- Test: `backend/tests/modules/automation_rules/test_inputs.py`

**Interfaces:**
- Produces 数据结构(冻结 dataclass):
  - `AccountInfo(advertiser_id, bc_id, timezone, currency, name)`
  - `Entity(key, kind, advertiser_id, bc_id, remote_id, name, drama: str | None, naming_valid: bool, status: str | None, currency, timezone, parent_key: str | None, attributes: dict[str, Decimal | str | None], material_uses: tuple[dict, ...])`。`key` 规则:`"{kind}:{advertiser_id}:{remote_id}"`;素材 `remote_id` 为主素材 ID;`drama` 为 `"{provider_label}-{drama_name}"`;`material_uses` 仅素材实体有值,每项 `{"advertiser_id","ad_remote_id","platform_material_id","ad_material_id","material_type"}`。
  - `Inputs(accounts: dict[str, AccountInfo], entities: dict[str, Entity], facts: dict[str, dict[str, dict[str, Decimal | None]]], skipped_accounts: list[dict[str, str]])`;`facts[range][entity_key][stored_metric]`。
- Produces: `expand_accounts(session, *, tenant_id, scope: AccountScope) -> dict[str, AccountInfo]`;`load_inputs(session, *, tenant_id, definition: RuleDefinition, now: datetime) -> Inputs`;`local_window(range_type, timezone, now) -> tuple[date, date]`。
- 新鲜度阈值:`FRESHNESS = {"ad_day": timedelta(minutes=90), "material_day": timedelta(minutes=150)}`。

- [ ] **Step 1: 写失败测试**(使用 `sync_seed`:acc-1 香港时区在 bc-a;acc-2 UTC 同时在 bc-a、bc-b)

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import AdObject, CampaignNameProjection
from app.modules.automation_rules.inputs import expand_accounts, load_inputs, local_window
from app.modules.automation_rules.schemas import AccountScope, RuleDefinition
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.automation_rules.test_definition import base
from tests.modules.reporting.sync_conftest import ad_row, sync_seed  # noqa: F401

NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)  # 香港 11:00,UTC 03:00
TODAY = date(2026, 10, 14)


def _seed(session, seed, *, fresh=("acc-1", "acc-2")):
    publish_ad_day(session, tenant_id=seed.tenant_id, job_id=uuid4(),
                   accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong"), "acc-2": AccountMeta("USD", "UTC")},
                   start=TODAY - timedelta(days=2), end=TODAY, rows=[
                       ad_row("a1", TODAY, campaign="c1", adgroup="g1", spend="10.00"),
                       ad_row("a1", TODAY - timedelta(days=2), campaign="c1", adgroup="g1", spend="4.00"),
                       ad_row("b1", TODAY, campaign="c9", adgroup="g9", spend="2.00", adv="acc-2"),
                   ])
    for adv in ("acc-1", "acc-2"):
        state = session.get(ReportSyncState, (seed.tenant_id, adv, "ad_day", TODAY))
        state.last_success_at = NOW - (timedelta(minutes=10) if adv in fresh else timedelta(hours=5))
    for adv, kind, rid, parent, name, cfg, status in (
        ("acc-1", "campaign", "c1", None, "嘉书-总裁-备注", {"create_time": "2026-10-01 00:00:00", "budget": "50"}, "ENABLE"),
        ("acc-1", "adgroup", "g1", "c1", "组一", {"budget": "20", "roas_bid": "1.2"}, "ENABLE"),
        ("acc-1", "ad", "a1", "g1", "广告一", {}, "DISABLE"),
        ("acc-2", "campaign", "c9", None, "无效命名", {}, "ENABLE"),
        ("acc-2", "adgroup", "g9", "c9", "组九", {}, "ENABLE"),
        ("acc-2", "ad", "b1", "g9", "广告九", {}, "ENABLE"),
    ):
        session.add(AdObject(tenant_id=seed.tenant_id, advertiser_id=adv, kind=kind, remote_id=rid,
                             parent_kind={"adgroup": "campaign", "ad": "adgroup"}.get(kind),
                             parent_remote_id=parent, ad_type="SMART_PLUS", name=name,
                             configuration=cfg, operation_status=status, observed_at=NOW, published_version=1))
    session.add(CampaignNameProjection(tenant_id=seed.tenant_id, advertiser_id="acc-1", campaign_remote_id="c1",
                                       name_revision=1, raw_name="嘉书-总裁-备注", provider_label="嘉书",
                                       drama_name="总裁", status="VALID", parser_revision=1, grouping_revision=1))
    session.add(CampaignNameProjection(tenant_id=seed.tenant_id, advertiser_id="acc-2", campaign_remote_id="c9",
                                       name_revision=1, raw_name="无效命名", status="INVALID",
                                       parser_revision=1, grouping_revision=1))
    session.flush()


def definition(**patch):
    return RuleDefinition.model_validate(base(**patch))


def test_account_in_two_bcs_counted_once(session, sync_seed):
    accounts = expand_accounts(session, tenant_id=sync_seed.tenant_id, scope=AccountScope(mode="ALL"))
    assert sorted(accounts) == ["acc-1", "acc-2"]
    assert accounts["acc-2"].bc_id == "bc-a"  # 多 BC 时取最小 BC ID,结果稳定
    only_b = expand_accounts(session, tenant_id=sync_seed.tenant_id, scope=AccountScope(mode="BC", bc_ids=("bc-b",)))
    assert list(only_b) == ["acc-2"] and only_b["acc-2"].bc_id == "bc-b"


def test_local_windows_follow_account_timezone():
    late = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 香港已是 10-15
    assert local_window("TODAY", "Asia/Hong_Kong", late) == (date(2026, 10, 15),) * 2
    assert local_window("TODAY", "UTC", late) == (date(2026, 10, 14),) * 2
    assert local_window("PAST_3_DAYS", "UTC", late) == (date(2026, 10, 12), date(2026, 10, 14))
    assert local_window("YESTERDAY", "UTC", late) == (date(2026, 10, 13),) * 2


def test_entities_facts_and_attributes(session, sync_seed):
    _seed(session, sync_seed)
    rule = definition(conditions=[
        {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]},
        {"metric": "spend", "range": "PAST_3_DAYS", "operator": "GT", "values": ["1"]},
    ])
    inputs = load_inputs(session, tenant_id=sync_seed.tenant_id, definition=rule, now=NOW)
    g1 = inputs.entities["adgroup:acc-1:g1"]
    assert g1.drama == "嘉书-总裁" and g1.naming_valid
    assert g1.attributes["budget"] == Decimal("20") and g1.attributes["roas_bid"] == Decimal("1.2")
    assert inputs.entities["adgroup:acc-2:g9"].naming_valid is False
    assert inputs.facts["TODAY"]["adgroup:acc-1:g1"]["spend"] == Decimal("10.00")
    assert inputs.facts["PAST_3_DAYS"]["adgroup:acc-1:g1"]["spend"] == Decimal("14.00")
    # 后代对象(广告)也加载,供执行层级展开使用。
    assert inputs.entities["ad:acc-1:a1"].parent_key == "adgroup:acc-1:g1"
    assert inputs.entities["campaign:acc-1:c1"].attributes["days_since_creation"] == Decimal("13")


def test_stale_accounts_are_skipped(session, sync_seed):
    _seed(session, sync_seed, fresh=("acc-1",))
    inputs = load_inputs(session, tenant_id=sync_seed.tenant_id, definition=definition(), now=NOW)
    assert "acc-2" not in inputs.accounts
    assert not any(e.advertiser_id == "acc-2" for e in inputs.entities.values())
    assert inputs.skipped_accounts == [{"advertiser_id": "acc-2", "reason": "STALE_REPORT"}]
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `inputs.py`**

要点(全部写成代码,下列为结构与关键 SQL):

```python
"""判断输入:展开租户账户、加载对象目录、报表事实与新鲜度。只读本地已同步数据。"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.accounts.access import usable_grants
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.schemas import AccountScope, RuleDefinition
from app.modules.reporting.metric_catalog import STORED_KEYS

FRESHNESS = {"ad_day": timedelta(minutes=90), "material_day": timedelta(minutes=150)}
_PAST = {"PAST_3_DAYS": 3, "PAST_5_DAYS": 5, "PAST_7_DAYS": 7}
_LIFETIME_START = date(2000, 1, 1)
_BUDGET_KEYS = ("budget", "daily_budget", "campaign_daily_budget")
_ROAS_KEYS = ("roas_bid", "target_roas")
```

- `local_window(range_type, timezone, now)`:`today = now.astimezone(ZoneInfo(timezone)).date()`;TODAY→(today, today);YESTERDAY→(today-1, today-1);PAST_N→(today-N+1, today);LIFETIME→(`_LIFETIME_START`, today)。
- `expand_accounts`:`usable_grants(tenant_id=tenant_id, bc_id=col(BCAccountAccess.bc_id), action="read")`,按 `scope.mode` 追加 `bc_id IN` / `advertiser_id IN` 过滤,`order_by(advertiser_id, bc_id)`,对每个账户取第一行(最小 BC ID);再一次查询 `AdvertiserAccount` 取 timezone、currency、name。
- `load_inputs`:
  1. `accounts = expand_accounts(...)`。
  2. 新鲜度:按应用层级选 `kind = "material_day" if level == "material" else "ad_day"`,SQL:
     ```sql
     SELECT advertiser_id, max(last_success_at) FROM report_sync_state
     WHERE tenant_id = :t AND report_kind = :k AND advertiser_id = ANY(:advs)
     GROUP BY advertiser_id
     ```
     没有记录或早于 `now - FRESHNESS[kind]` 的账户移出 `accounts`,记 `{"advertiser_id": adv, "reason": "STALE_REPORT"}`(按账户 ID 排序)。
  3. 对象:一次查询 `ad_object`(kind in campaign/adgroup/ad,账户在范围内)与最新 `campaign_name_projection`(同 `row_set.py` 中 `DISTINCT ON ... ORDER BY grouping_revision DESC, name_revision DESC, observed_at DESC`)。建实体:`drama`/`naming_valid` 从系列投影取,广告组、广告沿 `parent_remote_id` 继承;`attributes`:`name`(str)、`budget`/`roas_bid`(配置中第一个非空可解析值,Decimal)、`days_since_creation`(`create_time` 解析:数字视为 epoch 秒,字符串 `YYYY-MM-DD HH:MM:SS` 视为 UTC;按账户时区取本地日,与 `now` 的本地日相减,Decimal;无法解析为 None)。
  4. 素材(仅应用层级为 material,或执行层级为 material 时加载):查询 `ad_material_reference` 中 `main_material_id IS NOT NULL` 的行;按 `(advertiser_id, main_material_id)` 建实体 `key = f"material:{adv}:{main_id}"`,`name` 取第一条非空 name,`status = "ENABLE"` 若任一使用 `operation_status == "ENABLE"` 否则 `"DISABLE"`,`drama` 取所用广告的剧(多部剧时为 None),`material_uses` 列出全部使用,`parent_key=None`。
  5. 事实:按条件中出现的 range 去重;对每个 range 为每个账户算窗口,SQL(广告日报;素材层级改用 `material_report_day` 并按 `main_material_id` 分组):
     ```sql
     SELECT f.advertiser_id, f.campaign_id, f.adgroup_id, f.ad_id,
            sum(f.spend) AS spend, ...(STORED_KEYS 逐列 sum)
     FROM ad_report_day f
     JOIN unnest(CAST(:advs AS text[]), CAST(:starts AS date[]), CAST(:ends AS date[])) AS w(adv, s, e)
       ON w.adv = f.advertiser_id AND f.stat_date BETWEEN w.s AND w.e
     WHERE f.tenant_id = :t
     GROUP BY f.advertiser_id, f.campaign_id, f.adgroup_id, f.ad_id
     ```
     在 Python 中把广告行累加到 `ad:`、`adgroup:`、`campaign:` 三个实体键(`None` 与数值相加时保留数值;全为 None 时为 None)。只保留范围内实体的键。
  6. 返回 `Inputs`。没有事实行的实体不写入 `facts`(判断引擎视为 0)。

- [ ] **Step 4: 运行测试** `pytest tests/modules/automation_rules/test_inputs.py -q` → PASS

- [ ] **Step 5: 提交** `automation-rules: 规则判断输入加载`。

---

### Task 6: 判断引擎

**Files:**
- Create: `backend/app/modules/automation_rules/evaluate.py`
- Test: `backend/tests/modules/automation_rules/test_evaluate.py`

**Interfaces:**
- Consumes: `Inputs`、`Entity`(Task 5)、`RuleDefinition`(Task 1)、`DERIVED_RATIOS`。
- Produces: `Target(key, kind, advertiser_id, bc_id, remote_id, name, material_uses: tuple[dict, ...])`;`Hit(subject_key, subject_label, drama, advertiser_id, bc_id, currency, metrics: dict[str, str], targets: tuple[Target, ...])`;`Evaluation(checked: int, hits: tuple[Hit, ...], skipped_count: int)`;`evaluate(definition, inputs) -> Evaluation`。`metrics` 键为 `"{metric}:{range}"` 或属性名,值为十进制字符串(None 为空串)。

- [ ] **Step 1: 写失败测试**(纯函数,手工构造 `Inputs`)

```python
from decimal import Decimal

from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import AccountInfo, Entity, Inputs
from app.modules.automation_rules.schemas import RuleDefinition
from tests.modules.automation_rules.test_definition import base


def ent(kind, adv, rid, *, parent=None, drama="嘉书-总裁", valid=True, status="ENABLE",
        currency="USD", name=None, uses=(), **attrs):
    return Entity(key=f"{kind}:{adv}:{rid}", kind=kind, advertiser_id=adv, bc_id="bc-a",
                  remote_id=rid, name=name or rid, drama=drama if valid else None,
                  naming_valid=valid, status=status, currency=currency, timezone="UTC",
                  parent_key=parent, attributes={"name": name or rid, **attrs},
                  material_uses=tuple(uses))


def inputs(entities, today):
    return Inputs(accounts={"a": AccountInfo("a", "bc-a", "UTC", "USD", "A")},
                  entities={e.key: e for e in entities}, facts={"TODAY": today}, skipped_accounts=[])


def rule(**patch):
    return RuleDefinition.model_validate(base(**patch))


GROUPS = [
    ent("campaign", "a", "c1"), ent("campaign", "a", "c2"),
    ent("adgroup", "a", "g1", parent="campaign:a:c1"),
    ent("adgroup", "a", "g2", parent="campaign:a:c2"),
    ent("ad", "a", "x1", parent="adgroup:a:g1"),
]


def test_each_object_and_sum_over_sum_roas():
    facts = {"adgroup:a:g1": {"spend": Decimal("20"), "revenue_total": Decimal("10")},
             "adgroup:a:g2": {"spend": Decimal("5"), "revenue_total": Decimal("10")}}
    result = evaluate(rule(conditions=[
        {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["10"]},
        {"metric": "roas_total", "range": "TODAY", "operator": "LT", "values": ["1"]},
    ]), inputs(GROUPS, facts))
    assert result.checked == 2
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g1"]
    assert result.hits[0].metrics["roas_total:TODAY"] == "0.5"


def test_action_level_expands_to_descendants():
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}}
    result = evaluate(rule(action={"type": "PAUSE", "level": "ad"}), inputs(GROUPS, facts))
    assert [t.key for t in result.hits[0].targets] == ["ad:a:x1"]


def test_drama_total_hits_all_members():
    facts = {"adgroup:a:g1": {"spend": Decimal("6")}, "adgroup:a:g2": {"spend": Decimal("6")}}
    result = evaluate(rule(aggregation="DRAMA"), inputs(GROUPS, facts))
    assert len(result.hits) == 1 and result.hits[0].subject_label == "嘉书-总裁"
    assert {t.key for t in result.hits[0].targets} == {"adgroup:a:g1", "adgroup:a:g2"}


def test_drama_skips_invalid_naming():
    items = GROUPS + [ent("adgroup", "a", "g3", valid=False)]
    facts = {"adgroup:a:g3": {"spend": Decimal("50")}}
    result = evaluate(rule(aggregation="DRAMA"), inputs(items, facts))
    assert result.hits == () and result.skipped_count == 1


def test_zero_denominator_is_not_a_hit():
    facts = {"adgroup:a:g1": {"spend": Decimal("0"), "revenue_total": Decimal("0")}}
    result = evaluate(rule(aggregation="ALL", conditions=[
        {"metric": "roas_total", "range": "TODAY", "operator": "LT", "values": ["1"]}]), inputs(GROUPS, facts))
    assert result.hits == ()


def test_all_objects_split_by_currency():
    items = [ent("adgroup", "a", "g1"), ent("adgroup", "a", "g2", currency="EUR")]
    facts = {"adgroup:a:g1": {"spend": Decimal("8")}, "adgroup:a:g2": {"spend": Decimal("8")}}
    result = evaluate(rule(aggregation="ALL"), inputs(items, facts))
    assert result.hits == ()  # 各自 8,均未超过 10;不会合并成 16
    assert result.checked == 2


def test_status_filters_and_missing_facts_are_zero():
    items = [ent("adgroup", "a", "g1", status="DISABLE"), ent("adgroup", "a", "g2")]
    result = evaluate(rule(conditions=[
        {"metric": "spend", "range": "TODAY", "operator": "LT", "values": ["1"]}]), inputs(items, {}))
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g2"]


def test_name_and_attribute_conditions():
    items = [ent("adgroup", "a", "g1", name="嘉书-测试", budget=Decimal("30")),
             ent("adgroup", "a", "g2", name="其他", budget=Decimal("30"))]
    result = evaluate(rule(conditions=[
        {"metric": "name", "operator": "STARTS_WITH", "values": ["嘉书"]},
        {"metric": "budget", "operator": "BETWEEN", "values": ["20", "40"]}]), inputs(items, {}))
    assert [h.subject_key for h in result.hits] == ["adgroup:a:g1"]


def test_selected_objects_and_name_filters():
    patch = {"apply_to": {"level": "adgroup", "status": "SELECTED", "object_ids": ["a:g2"]}}
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}, "adgroup:a:g2": {"spend": Decimal("20")}}
    assert [h.subject_key for h in evaluate(rule(**patch), inputs(GROUPS, facts)).hits] == ["adgroup:a:g2"]
    patch = {"apply_to": {"level": "adgroup", "status": "ACTIVE", "name_excludes": "g1"}}
    assert [h.subject_key for h in evaluate(rule(**patch), inputs(GROUPS, facts)).hits] == ["adgroup:a:g2"]


def test_material_targets_from_ads():
    use = {"advertiser_id": "a", "ad_remote_id": "x1", "platform_material_id": "v1",
           "ad_material_id": "m1", "material_type": "VIDEO"}
    items = GROUPS + [ent("material", "a", "v1", uses=[use])]
    facts = {"adgroup:a:g1": {"spend": Decimal("20")}}
    result = evaluate(rule(action={"type": "PAUSE", "level": "material"},
                           schedule={"frequency": "1H", "window": "CONTINUOUS"}), inputs(items, facts))
    assert [t.key for t in result.hits[0].targets] == ["material:a:v1"]
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `evaluate.py`**

```python
"""纯函数判断引擎:按统计口径分组、计算指标、判断条件、展开执行对象。"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from app.modules.automation_rules.catalog import RULE_METRICS
from app.modules.automation_rules.inputs import Entity, Inputs
from app.modules.automation_rules.schemas import Condition, RuleDefinition
from app.modules.reporting.metric_catalog import DERIVED_RATIOS, STORED_KEYS

_RATIOS = {name: (num, den, scale) for name, num, den, scale in DERIVED_RATIOS}
_STATUS = {"ACTIVE": "ENABLE", "PAUSED": "DISABLE"}


@dataclass(frozen=True)
class Target:
    key: str
    kind: str
    advertiser_id: str
    bc_id: str
    remote_id: str
    name: str
    material_uses: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Hit:
    subject_key: str
    subject_label: str
    drama: str | None
    advertiser_id: str | None
    bc_id: str | None
    currency: str
    metrics: dict[str, str]
    targets: tuple[Target, ...]


@dataclass(frozen=True)
class Evaluation:
    checked: int
    hits: tuple[Hit, ...]
    skipped_count: int


def _in_scope(entity: Entity, definition: RuleDefinition) -> bool:
    apply = definition.apply_to
    if entity.kind != apply.level:
        return False
    if apply.status == "SELECTED":
        if f"{entity.advertiser_id}:{entity.remote_id}" not in apply.object_ids:
            return False
    elif entity.status != _STATUS[apply.status]:
        return False
    if apply.drama_names and entity.drama not in apply.drama_names:
        return False
    if apply.name_contains and apply.name_contains not in entity.name:
        return False
    if apply.name_excludes and apply.name_excludes in entity.name:
        return False
    return True


def _sum(members, facts, range_type):
    totals: dict[str, Decimal | None] = {key: Decimal(0) for key in STORED_KEYS}
    for member in members:
        row = facts.get(range_type, {}).get(member.key)
        if row is None:
            continue  # 已通过新鲜度检查的账户,没有事实行即没有消耗,按 0 计。
        for key in STORED_KEYS:
            value = row.get(key)
            if value is not None:
                totals[key] = (totals[key] or Decimal(0)) + value
    return totals


def _metric(condition: Condition, members, inputs) -> Decimal | str | None:
    meta = RULE_METRICS[condition.metric]
    if meta.kind == "attribute":
        return members[0].attributes.get(condition.metric)
    totals = _sum(members, inputs.facts, condition.range)
    if condition.metric in _RATIOS:
        num, den, scale = _RATIOS[condition.metric]
        a, b = totals.get(num), totals.get(den)
        # 分母为 0 或缺失时没有比率,条件视为不满足。
        return a / b * scale if a is not None and b else None
    return totals.get(condition.metric)


def _matches(condition: Condition, value) -> bool:
    if value is None:
        return False
    op, values = condition.operator, condition.values
    if isinstance(value, str):
        target = values[0]
        return {
            "CONTAINS": target in value, "NOT_CONTAINS": target not in value,
            "STARTS_WITH": value.startswith(target), "ENDS_WITH": value.endswith(target),
            "EQ": value == target,
        }[op]
    numbers = [Decimal(v) for v in values]
    if op == "GT":
        return value > numbers[0]
    if op == "LT":
        return value < numbers[0]
    if op == "EQ":
        return value == numbers[0]
    return numbers[0] < value < numbers[1]


def _metric_key(condition: Condition) -> str:
    return f"{condition.metric}:{condition.range}" if condition.range else condition.metric


def _format(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize() if value == value.to_integral() else value.quantize(Decimal("0.0001")).normalize(), "f")
    return str(value)


def _children(inputs: Inputs) -> dict[str, list[Entity]]:
    result = defaultdict(list)
    for entity in inputs.entities.values():
        if entity.parent_key:
            result[entity.parent_key].append(entity)
    return result


def _targets(members, level, inputs, children) -> tuple[Target, ...]:
    """把判断对象展开到执行层级;素材按使用它的广告归属。"""
    found: dict[str, Entity] = {}
    ad_keys: set[str] = set()
    stack = list(members)
    while stack:
        item = stack.pop()
        if item.kind == level:
            found[item.key] = item
            continue
        if item.kind == "ad":
            ad_keys.add(item.key)
        stack.extend(children.get(item.key, ()))
    if level == "material":
        for entity in inputs.entities.values():
            if entity.kind == "material" and (
                entity.key in found
                or any(f"ad:{u['advertiser_id']}:{u['ad_remote_id']}" in ad_keys for u in entity.material_uses)
            ):
                found[entity.key] = entity
    return tuple(
        Target(e.key, e.kind, e.advertiser_id, e.bc_id, e.remote_id, e.name, e.material_uses)
        for e in sorted(found.values(), key=lambda e: e.key)
    )


def evaluate(definition: RuleDefinition, inputs: Inputs) -> Evaluation:
    scoped = sorted((e for e in inputs.entities.values() if _in_scope(e, definition)), key=lambda e: e.key)
    skipped = 0
    groups: dict[tuple, list[Entity]] = defaultdict(list)
    for entity in scoped:
        if definition.aggregation == "EACH":
            groups[(entity.key,)].append(entity)
        elif definition.aggregation == "DRAMA":
            if not entity.naming_valid or entity.drama is None:
                skipped += 1  # 命名不规范,解析不出剧名,不参与剧合计。
                continue
            groups[(entity.drama, entity.currency)].append(entity)
        else:
            groups[(entity.currency,)].append(entity)
    children = _children(inputs)
    hits = []
    for key, members in groups.items():
        values = [(c, _metric(c, members, inputs)) for c in definition.conditions]
        if not all(_matches(c, v) for c, v in values):
            continue
        first = members[0]
        if definition.aggregation == "EACH":
            subject, label = first.key, first.name
        elif definition.aggregation == "DRAMA":
            subject, label = f"drama:{key[0]}:{key[1]}", key[0]
        else:
            subject, label = f"all:{key[0]}", f"全部对象({key[0]})"
        hits.append(Hit(
            subject_key=subject, subject_label=label, drama=first.drama,
            advertiser_id=first.advertiser_id if definition.aggregation == "EACH" else None,
            bc_id=first.bc_id if definition.aggregation == "EACH" else None,
            currency=first.currency,
            metrics={_metric_key(c): _format(v) for c, v in values},
            targets=_targets(members, definition.action.level, inputs, children),
        ))
    return Evaluation(checked=len(groups), hits=tuple(hits), skipped_count=skipped)
```

- [ ] **Step 4: 运行测试** → PASS

- [ ] **Step 5: 提交** `automation-rules: 判断引擎(三种统计口径)`。

---

### Task 7: 排期计算

**Files:**
- Create: `backend/app/modules/automation_rules/schedule.py`
- Test: `backend/tests/modules/automation_rules/test_schedule.py`

**Interfaces:**
- Produces: `next_run_at(schedule: Schedule, *, after: datetime, timezone: str) -> datetime | None`(严格晚于 `after` 的下一个运行时刻,UTC;没有时返回 None)。

- [ ] **Step 1: 写失败测试**

```python
from datetime import UTC, datetime

from app.modules.automation_rules.schedule import next_run_at
from app.modules.automation_rules.schemas import Schedule

SH = "Asia/Shanghai"


def at(*args):
    return datetime(*args, tzinfo=UTC)


def test_every_30_minutes_aligns_to_slot():
    s = Schedule(frequency="30M", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 1, 7), timezone=SH) == at(2026, 10, 14, 1, 30)
    assert next_run_at(s, after=at(2026, 10, 14, 1, 30), timezone=SH) == at(2026, 10, 14, 2, 0)


def test_four_hours_aligns_in_tenant_timezone():
    s = Schedule(frequency="4H", window="CONTINUOUS")
    # 上海 09:10 → 下一个 4 小时整点为上海 12:00 = UTC 04:00
    assert next_run_at(s, after=at(2026, 10, 14, 1, 10), timezone=SH) == at(2026, 10, 14, 4, 0)


def test_daily_at():
    s = Schedule(frequency="DAILY", daily_at="10:00", window="CONTINUOUS")
    assert next_run_at(s, after=at(2026, 10, 14, 3, 0), timezone=SH) == at(2026, 10, 15, 2, 0)


def test_custom_week_periods():
    s = Schedule.model_validate({"frequency": "1H", "window": "CUSTOM", "periods": [
        {"date_type": "WEEK", "days": [1], "start": "09:00", "end": "10:00"}]})
    # 2026-10-14 是周三;下一次在周一上海 09:00 = UTC 01:00
    assert next_run_at(s, after=at(2026, 10, 14, 1, 0), timezone=SH) == at(2026, 10, 19, 1, 0)
    assert next_run_at(s, after=at(2026, 10, 19, 1, 0), timezone=SH) == at(2026, 10, 19, 2, 0)
    assert next_run_at(s, after=at(2026, 10, 19, 2, 0), timezone=SH) == at(2026, 10, 26, 1, 0)


def test_once():
    s = Schedule(frequency="30M", window="ONCE", once_at=at(2026, 10, 20, 0, 0))
    assert next_run_at(s, after=at(2026, 10, 14, 0, 0), timezone=SH) == at(2026, 10, 20, 0, 0)
    assert next_run_at(s, after=at(2026, 10, 20, 0, 0), timezone=SH) is None
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `schedule.py`**

```python
"""规则运行时刻:按租户时区对齐频率槽位,再筛选生效时段。"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.modules.automation_rules.schemas import FREQUENCY_MINUTES, Period, Schedule

_HORIZON_DAYS = 62


def _in_period(local: datetime, period: Period) -> bool:
    if period.date_type == "WEEK" and local.isoweekday() not in period.days:
        return False
    if period.date_type == "MONTH" and local.day not in period.days:
        return False
    return period.start <= local.time().replace(second=0, microsecond=0) <= period.end


def next_run_at(schedule: Schedule, *, after: datetime, timezone: str) -> datetime | None:
    if schedule.window == "ONCE":
        return schedule.once_at.astimezone(UTC) if schedule.once_at > after else None
    zone = ZoneInfo(timezone)
    local = after.astimezone(zone)
    if schedule.frequency == "DAILY":
        candidate = local.replace(hour=schedule.daily_at.hour, minute=schedule.daily_at.minute,
                                  second=0, microsecond=0)
        step = timedelta(days=1)
        if candidate <= local:
            candidate += step
    else:
        minutes = FREQUENCY_MINUTES[schedule.frequency]
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = int((local - midnight).total_seconds() // 60)
        candidate = midnight + timedelta(minutes=(elapsed // minutes + 1) * minutes)
        step = timedelta(minutes=minutes)
    limit = local + timedelta(days=_HORIZON_DAYS)
    while candidate <= limit:
        # 槽位按当地墙钟计算;跨日时从当地零点重新对齐。
        normalized = candidate.astimezone(zone)
        if schedule.window == "CONTINUOUS" or any(_in_period(normalized, p) for p in schedule.periods):
            return normalized.astimezone(UTC)
        candidate += step
        if schedule.frequency != "DAILY" and candidate.date() != (candidate - step).date():
            candidate = candidate.replace(hour=0, minute=0)
    return None
```

- [ ] **Step 4: 运行测试** → PASS

- [ ] **Step 5: 提交** `automation-rules: 规则排期计算`。

---

### Task 8: 规则管理与试运行接口

**Files:**
- Create: `backend/app/modules/automation_rules/service.py`
- Modify: `backend/app/modules/automation_rules/schemas.py`(接口模型)、`api.py`(路由)
- Test: `backend/tests/modules/automation_rules/test_api.py`

**Interfaces:**
- Produces 接口模型:`RuleCreate(name: str 1-100, definition: RuleDefinition, enabled: bool = True)`、`RuleUpdate(name, definition, enabled, expected_version: int)`、`RulePublic(id, name, enabled, version, definition, created_by, updated_by, created_at, updated_at, next_run_at, last_run_at, last_run: RunSummary | None)`、`RunSummary(id, slot_at, status, started_at, finished_at, checked_count, hit_count, skipped_count, skipped_accounts, error_code, notify_status, notify_error)`、`RulePage(items: list[RulePublic], total: int)`、`HitPublic(subject_key, subject_label, drama_name, advertiser_id, bc_id, currency, metrics, target_count, targets)`、`DryRunRequest(definition: RuleDefinition)`、`DryRunPublic(checked_count, hit_count, skipped_count, skipped_accounts, hits: list[HitPublic])`、`CatalogPublic(metrics: list[CatalogMetric], action_levels: dict[str, list[str]])`、`CatalogMetric(key, group, name_zh, unit, kind, levels, aggregations, operators, ranged)`。
- Produces 服务:`list_rules(session, *, context) -> RulePage`、`get_rule`、`create_rule`、`update_rule`、`set_enabled(session, *, context, rule_id, enabled) -> RulePublic`、`delete_rule`、`dry_run(session, *, context, definition, now=None) -> DryRunPublic`、`catalog() -> CatalogPublic`。保存时:`definition.mode != "NOTIFY"` → `automation_mode_unavailable`;写入需 `ads_manage` 权限;计算 `next_run_at`(启用时)用 `schedule.next_run_at(after=now, timezone=租户设置时区)`。
- 路由(`automation_rules` 标签):`GET /automation-rules/catalog`、`GET/POST /automation-rules`、`GET/PUT/DELETE /automation-rules/{rule_id}`、`POST /automation-rules/{rule_id}/enabled`(body `{"enabled": bool}`)、`POST /automation-rules/dry-run`。

- [ ] **Step 1: 写失败测试**

```python
from uuid import uuid4

from tests.modules.automation_rules.conftest import headers
from tests.modules.automation_rules.test_definition import base
from tests.modules.reporting.sync_conftest import sync_seed  # noqa: F401


def url(context, suffix=""):
    return f"/api/tenants/{context.tenant_id}/automation-rules{suffix}"


def create(client, context, name="低效止损", **patch):
    return client.post(url(context), headers=headers(context),
                       json={"name": name, "definition": base(**patch)})


def test_crud_and_versioning(client, context):
    created = create(client, context)
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["version"] == 1 and rule["next_run_at"]
    listed = client.get(url(context), headers=headers(context)).json()
    assert listed["total"] == 1
    update = {"name": "改名", "definition": base(), "enabled": True, "expected_version": 1}
    assert client.put(url(context, f"/{rule['id']}"), headers=headers(context), json=update).json()["version"] == 2
    stale = client.put(url(context, f"/{rule['id']}"), headers=headers(context), json=update)
    assert stale.status_code == 409
    off = client.post(url(context, f"/{rule['id']}/enabled"), headers=headers(context), json={"enabled": False})
    assert off.json()["enabled"] is False and off.json()["next_run_at"] is None
    assert client.delete(url(context, f"/{rule['id']}"), headers=headers(context)).status_code == 204


def test_rejects_non_notify_mode_and_duplicates(client, context):
    assert create(client, context, mode="AUTO").status_code == 409
    assert create(client, context).status_code == 201
    assert create(client, context).status_code == 409


def test_invalid_definition_422(client, context):
    assert create(client, context, conditions=[]).status_code == 422


def test_viewer_reads_but_cannot_write(client, viewer_context):
    assert create(client, viewer_context).status_code == 403
    assert client.get(url(viewer_context), headers=headers(viewer_context)).status_code == 200


def test_tenant_isolation(client, context, other_context):
    rule = create(client, context).json()
    assert client.get(url(other_context, f"/{rule['id']}"), headers=headers(other_context)).status_code == 404
    assert client.get(url(context, f"/{uuid4()}"), headers=headers(context)).status_code == 404


def test_catalog_lists_metrics_with_official_names(client, context):
    body = client.get(url(context, "/catalog"), headers=headers(context)).json()
    spend = next(m for m in body["metrics"] if m["key"] == "spend")
    assert spend["name_zh"] == "消耗" and "material" in spend["levels"]
    assert body["action_levels"]["BUDGET"] == ["campaign", "adgroup"]


def test_dry_run_service_hits_without_persisting(session, sync_seed):
    from sqlmodel import select
    from app.modules.automation_rules.models import AutomationRuleRun
    from app.modules.automation_rules.schemas import RuleDefinition
    from app.modules.automation_rules.service import dry_run
    from tests.modules.automation_rules.test_inputs import NOW, _seed
    _seed(session, sync_seed)
    result = dry_run(session, context=sync_seed.context, now=NOW, definition=RuleDefinition.model_validate(base(
        conditions=[{"metric": "spend", "range": "PAST_3_DAYS", "operator": "GT", "values": ["1"]}])))
    assert [h.subject_key for h in result.hits] == ["adgroup:acc-1:g1"]
    assert session.exec(select(AutomationRuleRun)).all() == []


def test_dry_run_http_reports_stale_accounts(client, session, sync_seed):
    from tests.modules.automation_rules.test_inputs import _seed
    _seed(session, sync_seed)  # 种子时间早于当前时间,两个账户都判为数据过期
    ctx = sync_seed.context
    response = client.post(url(ctx, "/dry-run"), headers=headers(ctx), json={"definition": base()})
    assert response.status_code == 200, response.text
    assert {a["advertiser_id"] for a in response.json()["skipped_accounts"]} == {"acc-1", "acc-2"}
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现接口模型、`service.py`、路由**

`service.py` 关键逻辑:

```python
def _require(session, context, action):
    return require_tenant(session, actor_id=context.actor_id, tenant_id=context.tenant_id, action=action)


def _load(session, context, rule_id) -> AutomationRule:
    rule = session.get(AutomationRule, rule_id)
    if rule is None or rule.tenant_id != context.tenant_id:
        raise DomainError("automation_rule_not_found", "规则不存在")
    return rule


def _check_mode(definition: RuleDefinition) -> None:
    if definition.mode != "NOTIFY":
        raise DomainError("automation_mode_unavailable", "当前版本只支持「仅通知」模式")


def _schedule_next(session, rule, definition, now):
    setting = load_tenant_setting(session, rule.tenant_id)
    rule.next_run_at = (
        next_run_at(definition.schedule, after=now, timezone=setting.schedule_timezone)
        if rule.enabled else None
    )
```

- 名称冲突:`session.flush()` 捕获 `IntegrityError`(约束 `uq_automation_rule_name`),回滚到保存点(`session.begin_nested()` 包裹写入)后抛 `automation_rule_name_conflict`。
- `update_rule`:`expected_version != rule.version` → `automation_rule_version_conflict`;成功后 `version += 1`、`updated_by`、`updated_at`、重算 `next_run_at`。
- `set_enabled`:启用时重算,关闭时 `next_run_at=None`;不改版本。
- `list_rules`:按 `updated_at DESC`;`last_run` 用一次查询取每条规则最新运行(`DISTINCT ON (rule_id) ... ORDER BY rule_id, started_at DESC`)。
- `dry_run`:`_require(read)`;`inputs = load_inputs(...)`;`result = evaluate(definition, inputs)`;返回前 500 条命中,`targets` 每条最多 50 个。
- `catalog()`:遍历 `RULE_METRICS`;`name_zh` 对事实指标用 `table_label(metric_name_zh(key, "basic"))`,属性用固定中文(`name` 名称、`days_since_creation` 创建天数、`budget` 当前预算、`roas_bid` 当前 ROAS 出价);`action_levels` 取 `ACTION_LEVELS`。
- 路由:写操作后 `session.commit()`;创建返回 201、删除返回 204;Pydantic 校验失败由 FastAPI 返回 422。

- [ ] **Step 4: 运行测试** `pytest tests/modules/automation_rules -q` → PASS

- [ ] **Step 5: 提交** `automation-rules: 规则管理、指标目录与试运行接口`。

---

### Task 9: 运行器、后台任务与部署开关

**Files:**
- Create: `backend/app/modules/automation_rules/runner.py`、`tasks.py`
- Modify: `backend/app/core/config.py`(`AUTOMATED_RULES_ENABLED: bool = False`)、`backend/app/jobs/celery_app.py`(imports、task_routes、beat)、`backend/app/modules/automation_rules/api.py`(运行记录路由)、`schemas.py`(`RunPage(items: list[RunSummary & rule_id, rule_name], total)`、`RunDetail(run: RunSummary, rule_id, rule_name, hits: list[HitPublic])`)
- Modify(开关):`.env.example`、`compose.yml`、`compose.production.yml`、`deploy/k8s/runtime-secret.example.env`、`deploy/k8s/README.md`、`deploy/release/common.py`、`deploy/k8s/operations.py`、`scripts/check-local-deployment.py`、`scripts/test-k8s-deployment.py`、`scripts/test-release-integration.py`、`docs/runbooks/deployment.md`、`AGENTS.md`
- Test: `backend/tests/modules/automation_rules/test_runner.py`、`test_tasks.py`

**Interfaces:**
- Produces: `runner.execute_run(session, *, rule: AutomationRule, slot_at: datetime, now: datetime) -> AutomationRuleRun`(同一槽位重复调用返回已有运行,不重复写命中);`runner.deliver_notification(session, *, run_id: UUID, transport=None) -> str`(返回 `SENT`/`NOT_REQUIRED`/`FAILED`)。
- Produces Celery 任务:`automation.scan_due`(control,Beat 60 秒)、`automation.run_rule`(ads-reporting,outbox 投递,payload `{"rule_id", "slot_at"}`)、`automation.send_feishu`(control,outbox 投递,payload `{"run_id"}`)、`automation.cleanup`(control,Beat 3600 秒,删除 30 天前的运行及命中)。函数形式 `scan_due_rules(session, *, now) -> int`、`cleanup_runs(session, *, now) -> int` 供测试直接调用。
- 路由:`GET /automation-runs?rule_id=&limit=&offset=`(`read`)、`GET /automation-runs/{run_id}`(`read`)。

- [ ] **Step 1: 写失败测试**

```python
# test_runner.py
import httpx
from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import AutomationRule, AutomationRuleHit, AutomationRuleRun
from app.modules.automation_rules.runner import deliver_notification, execute_run
from app.modules.automation_rules.schemas import AutomationSettingsUpdate
from app.modules.automation_rules.settings_service import update_settings
from tests.modules.automation_rules.test_definition import base
from tests.modules.automation_rules.test_inputs import NOW, _seed
from tests.modules.reporting.sync_conftest import sync_seed  # noqa: F401

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/run-hook"


def make_rule(session, seed, **patch):
    rule = AutomationRule(tenant_id=seed.tenant_id, name="r", created_by=seed.actor_id,
                          updated_by=seed.actor_id, definition=base(**patch))
    session.add(rule)
    session.flush()
    return rule


def test_run_persists_hits_once_per_slot_and_queues_feishu(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    again = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert again.id == run.id and run.status == "COMPLETE"
    hits = session.exec(select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)).all()
    assert [h.subject_key for h in hits] == ["adgroup:acc-1:g1"]
    assert run.notify_status == "PENDING"
    dispatch = session.exec(select(PendingDispatch).where(PendingDispatch.task_name == "automation.send_feishu")).all()
    assert len(dispatch) == 1 and dispatch[0].payload == {"run_id": str(run.id)}
    assert rule.last_run_at == NOW


def test_no_hits_no_notification_by_default(session, sync_seed):
    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed, conditions=[
        {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["99999"]}])
    assert execute_run(session, rule=rule, slot_at=NOW, now=NOW).notify_status == "NOT_REQUIRED"


def test_owner_without_access_fails_closed(session, sync_seed):
    from app.modules.tenants.models import TenantMembership
    _seed(session, sync_seed)
    rule = make_rule(session, sync_seed)
    session.get(TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)).active = False
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    assert run.status == "FAILED" and run.error_code == "rule_owner_forbidden"


def test_deliver_marks_sent_and_failed(session, sync_seed):
    from app.modules.tenants.models import TenantMembership
    _seed(session, sync_seed)
    session.get(TenantMembership, (sync_seed.tenant_id, sync_seed.actor_id)).role = "tenant_admin"
    update_settings(session, context=sync_seed.context, body=AutomationSettingsUpdate(
        schedule_timezone="Asia/Shanghai", paused=False, feishu_enabled=True, webhook_url=HOOK))
    rule = make_rule(session, sync_seed)
    run = execute_run(session, rule=rule, slot_at=NOW, now=NOW)
    sent = []
    ok = httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={"code": 0}))
    assert deliver_notification(session, run_id=run.id, transport=ok) == "SENT"
    assert "组一" in sent[0].content.decode()
    run.notify_status = "PENDING"
    bad = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 9499, "msg": "bad"}))
    assert deliver_notification(session, run_id=run.id, transport=bad) == "FAILED"
    assert "bad" in session.get(AutomationRuleRun, run.id).notify_error
```

```python
# test_tasks.py
from datetime import timedelta

from sqlmodel import select

from app.core.config import settings
from app.jobs.models import PendingDispatch
from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun, AutomationTenantSetting
from app.modules.automation_rules.tasks import cleanup_runs, scan_due_rules
from tests.modules.automation_rules.test_runner import make_rule
from tests.modules.automation_rules.test_inputs import NOW
from tests.modules.reporting.sync_conftest import sync_seed  # noqa: F401


def test_scan_respects_flag_pause_and_advances(session, sync_seed, monkeypatch):
    rule = make_rule(session, sync_seed)
    rule.next_run_at = NOW - timedelta(minutes=1)
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", False)
    assert scan_due_rules(session, now=NOW) == 0
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    session.add(AutomationTenantSetting(tenant_id=sync_seed.tenant_id, paused=True))
    session.flush()
    assert scan_due_rules(session, now=NOW) == 0
    session.get(AutomationTenantSetting, sync_seed.tenant_id).paused = False
    assert scan_due_rules(session, now=NOW) == 1
    assert rule.next_run_at > NOW
    queued = session.exec(select(PendingDispatch).where(PendingDispatch.task_name == "automation.run_rule")).all()
    assert len(queued) == 1
    assert scan_due_rules(session, now=NOW) == 0  # 不重复投递


def test_once_rule_disables_after_scheduling(session, sync_seed, monkeypatch):
    monkeypatch.setattr(settings, "AUTOMATED_RULES_ENABLED", True)
    rule = make_rule(session, sync_seed, schedule={"frequency": "30M", "window": "ONCE",
                                                    "once_at": (NOW - timedelta(minutes=1)).isoformat()})
    rule.next_run_at = NOW - timedelta(minutes=1)
    assert scan_due_rules(session, now=NOW) == 1
    assert rule.enabled is False and rule.next_run_at is None


def test_cleanup_keeps_30_days(session, sync_seed):
    rule = make_rule(session, sync_seed)
    for days in (1, 31):
        session.add(AutomationRuleRun(tenant_id=rule.tenant_id, rule_id=rule.id, rule_version=1,
                                      definition={}, slot_at=NOW - timedelta(days=days),
                                      started_at=NOW - timedelta(days=days)))
    session.flush()
    assert cleanup_runs(session, now=NOW) == 1
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 `runner.py`**

- `execute_run`:
  1. 已存在 `(rule_id, slot_at)` 运行则直接返回。
  2. 用 `session.begin_nested()` 插入运行行(`rule_version`、`definition` 快照);捕获唯一约束冲突后读取已有运行返回。
  3. `require_tenant(actor=rule.updated_by, action="read")`;失败(`DomainError`)→ `status="FAILED"`、`error_code="rule_owner_forbidden"`、`finished_at=now`,返回。
  4. `definition = RuleDefinition.model_validate(rule.definition)`;`inputs = load_inputs(...)`;`result = evaluate(...)`。其他异常记录 `error_code="evaluation_failed"` 并 `FAILED`(记录日志,不吞掉 `SoftTimeLimitExceeded`)。
  5. 写入命中(`sequence` 从 0 递增,`targets` 存全部 Target 的 dict,`outcome="NOTIFIED"`),计数,`status="COMPLETE"`,`rule.last_run_at = now`。
  6. 通知:`definition.notification.enabled` 且(有命中或 `notify_without_hits`)或有跳过账户时 → `notify_status="PENDING"` 并 `enqueue_after_commit(session, context=TenantContext(rule.tenant_id, rule.updated_by, "operator"), task_name="automation.send_feishu", task_key=f"automation-feishu:{run.id}", payload={"run_id": str(run.id)})`;否则 `NOT_REQUIRED`。
- `deliver_notification`:读取运行与规则;`notify_status != "PENDING"` 直接返回当前值;`load_feishu` 为 None → `FAILED`、`notify_error="未配置或未启用飞书通知"`;否则构建卡片:标题 `【预警】{规则名}`(无命中时 `【运行报告】`,有跳过账户时追加 `,{n} 个账户数据未更新已跳过`),摘要行 `判断对象 {checked} 个,命中 {hit} 个`、跳过行;明细取前 10 条命中,指标显示名用 `catalog()` 的 `name_zh` 加时间周期中文(`TODAY` 今天、`YESTERDAY` 昨天、`PAST_3_DAYS` 近 3 天、`PAST_5_DAYS` 近 5 天、`PAST_7_DAYS` 近 7 天、`LIFETIME` 投放以来);链接 `{FRONTEND_HOST}/tenants/{tenant}/automation-rules?tab=runs&run={run_id}`。成功 → `SENT`;`DomainError` → `FAILED` 并记录 message(截断 500 字)。

- [ ] **Step 4: 实现 `tasks.py`**

```python
register_dispatch_task("automation.run_rule", "ads-reporting")
register_dispatch_task("automation.send_feishu", "control")


def scan_due_rules(session, *, now) -> int:
    if not settings.AUTOMATED_RULES_ENABLED:
        return 0
    rules = session.exec(
        select(AutomationRule)
        .where(col(AutomationRule.enabled).is_(True), col(AutomationRule.next_run_at) <= now)
        .order_by(col(AutomationRule.next_run_at))
        .limit(200)
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for rule in rules:
        setting = load_tenant_setting(session, rule.tenant_id)
        if setting.paused:
            continue  # 租户暂停全部规则:保持到期状态,恢复后立即运行一次。
        slot = rule.next_run_at
        enqueue_after_commit(session, context=TenantContext(rule.tenant_id, rule.updated_by, "operator"),
                             task_name="automation.run_rule",
                             task_key=f"automation-run:{rule.id}:{slot.isoformat()}",
                             payload={"rule_id": str(rule.id), "slot_at": slot.isoformat()})
        definition = RuleDefinition.model_validate(rule.definition)
        if definition.schedule.window == "ONCE":
            rule.enabled, rule.next_run_at = False, None
        else:
            rule.next_run_at = next_run_at(definition.schedule, after=now, timezone=setting.schedule_timezone)
        count += 1
    return count


def cleanup_runs(session, *, now) -> int:
    return session.connection().execute(
        delete(AutomationRuleRun).where(col(AutomationRuleRun.started_at) < now - timedelta(days=30))
    ).rowcount
```

Celery 入口(参照 `reporting.job_tasks` 写法):`@celery_app.task(name="automation.scan_due", time_limit=60, soft_time_limit=55)` 在 `Session(engine)` + `session.begin()` 中调用 `scan_due_rules`;`automation.run_rule(*, tenant_id, actor_id, payload)` 校验 `validate_dispatch_payload` 与键集合 `{"rule_id", "slot_at"}`,读取规则并核对 `tenant_id` 一致、规则仍存在,调用 `execute_run` 后提交(规则已删除时直接返回);`automation.send_feishu` 同样校验后调用 `deliver_notification` 并提交,发送失败不重试(状态已记录);`automation.cleanup` 调用 `cleanup_runs`。`celery_app.py`:`imports` 加 `"app.modules.automation_rules.tasks"`;`task_routes` 加四个任务的队列;`beat_schedule` 加 `"scan-automation-rules": {"task": "automation.scan_due", "schedule": 60.0, "options": {"queue": "control"}}` 与 `"cleanup-automation-runs": {"task": "automation.cleanup", "schedule": 3600.0, "options": {"queue": "control"}}`。

- [ ] **Step 5: 运行记录路由**:`GET /automation-runs`(按 `started_at DESC`,可按 `rule_id` 过滤,`limit` 1-100 默认 50,`offset`)、`GET /automation-runs/{run_id}`(租户不符或不存在 → `automation_run_not_found`;命中按 `sequence` 返回,`target_count=len(targets)`)。在 `test_api.py` 加一个用例:执行 `execute_run` 后两个接口分别返回该运行与命中,其他租户 404。

- [ ] **Step 6: 部署开关**:`config.py` 在 `ADS_SYNC_ENABLED` 下方加:

```python
    # 自动规则的周期判断与飞书发送默认关闭;试运行不受影响。
    AUTOMATED_RULES_ENABLED: bool = False
```

在 `.env.example`、`compose.yml`、`compose.production.yml`、`deploy/k8s/runtime-secret.example.env` 中 `ADS_SYNC_ENABLED` 的相邻位置以相同写法加入 `AUTOMATED_RULES_ENABLED`;`deploy/release/common.py`、`deploy/k8s/operations.py` 的 `FLAGS` 元组追加 `"AUTOMATED_RULES_ENABLED"`;`scripts/check-local-deployment.py`、`scripts/test-k8s-deployment.py`、`scripts/test-release-integration.py` 中凡枚举三个开关的断言或列表同步加入;`deploy/k8s/README.md` 第 5 条改为四个开关并说明新开关含义;`docs/runbooks/deployment.md` 开关表新增一行:

| `AUTOMATED_RULES_ENABLED` | Beat 不再调度自动规则运行,也不发送新的飞书通知;规则配置与试运行仍可用,已投递的运行仍会完成 | API、control/报表 Worker、Beat 加载同一值;租户已配置并测试飞书 Webhook;开启后所有已启用规则按排期运行。阶段 1 只发预警,不改广告 |

`AGENTS.md` 开关表同步新增该行(并把「部署级布尔开关只有以下三项」改为四项)。

- [ ] **Step 7: 运行测试**

Run: `uv run --frozen pytest tests/modules/automation_rules tests/jobs -q`
Expected: PASS。再运行发布工具自测:`python3 deploy/ci/test_backend_tests.py`(若存在开关枚举用例)与 `uv run --frozen python ../scripts/test-release-tools.py`;按实际脚本入口运行,失败按开关枚举补齐。

- [ ] **Step 8: 提交** 本任务全部文件,`automation-rules: 规则按排期运行并推送飞书预警,新增 AUTOMATED_RULES_ENABLED 开关`。

---

### Task 10: 前端客户端与规则表单逻辑

**Files:**
- Modify: `frontend/src/client/*`(由 `scripts/generate-client.sh` 生成)
- Create: `frontend/src/features/automation-rules/queries.ts`、`ruleForm.ts`
- Test: `frontend/tests/automation-rule-form.spec.ts`

**Interfaces:**
- Consumes: 生成的 `AutomationRulesService`(operationId 前缀 `automation_rules-`)及类型 `RuleDefinition`、`RulePublic`、`CatalogPublic`、`DryRunPublic`、`RunDetail`、`AutomationSettingsPublic`。
- Produces(`ruleForm.ts`):`emptyDefinition(): RuleDefinition`;`metricOptions(catalog, level, aggregation): CatalogMetric[]`;`actionLevels(aggregation, applyLevel, actionType): Level[]`(与后端 `allowed_action_levels` 一致);`minFrequency(applyLevel, actionLevel): "30M" | "1H"`;`summarizeConditions(def, catalog): string`;`summarizeAction(def): string`;`summarizeScope(def): string`;`RANGE_LABELS`、`OPERATOR_LABELS`、`LEVEL_LABELS`、`AGGREGATION_LABELS`、`ACTION_LABELS`、`MODE_LABELS`。
- Produces(`queries.ts`):`ruleKeys(tenantId)`、`catalogQuery(tenantId)`、`rulesQuery(tenantId)`、`runsQuery(tenantId, ruleId?)`、`runQuery(tenantId, runId)`、`settingsQuery(tenantId)`。

- [ ] **Step 1: 生成客户端**

Run: `cd .. && source /tmp/arres/testenv.sh && bash scripts/generate-client.sh`
Expected: `frontend/src/client/sdk.gen.ts` 出现 `AutomationRulesService`。

- [ ] **Step 2: 写失败测试** `tests/automation-rule-form.spec.ts`

```ts
import { expect, test } from "@playwright/test"
import {
  actionLevels,
  emptyDefinition,
  minFrequency,
  summarizeAction,
  summarizeConditions,
} from "../src/features/automation-rules/ruleForm"

const catalog = {
  metrics: [
    { key: "spend", group: "delivery", name_zh: "消耗", unit: "money", kind: "fact",
      levels: ["campaign", "adgroup", "ad", "material"], aggregations: ["EACH", "DRAMA", "ALL"],
      operators: ["GT", "LT", "BETWEEN", "EQ"], ranged: true },
    { key: "roas_total", group: "revenue", name_zh: "广告收益 ROAS", unit: "multiple", kind: "fact",
      levels: ["campaign", "adgroup", "ad", "material"], aggregations: ["EACH", "DRAMA", "ALL"],
      operators: ["GT", "LT", "BETWEEN", "EQ"], ranged: true },
  ],
  action_levels: { PAUSE: ["campaign", "adgroup", "ad", "material"], ENABLE: ["campaign", "adgroup", "ad", "material"],
    BUDGET: ["campaign", "adgroup"], ROAS: ["campaign"] },
}

test("执行层级与后端一致", () => {
  expect(actionLevels("EACH", "adgroup", "PAUSE")).toEqual(["adgroup", "ad", "material"])
  expect(actionLevels("DRAMA", "adgroup", "PAUSE")).toEqual(["campaign", "adgroup", "ad", "material"])
  expect(actionLevels("EACH", "ad", "BUDGET")).toEqual([])
})

test("素材层级最短 1 小时", () => {
  expect(minFrequency("adgroup", "material")).toBe("1H")
  expect(minFrequency("adgroup", "adgroup")).toBe("30M")
})

test("条件与操作摘要", () => {
  const def = {
    ...emptyDefinition(),
    aggregation: "DRAMA" as const,
    conditions: [
      { metric: "spend", range: "TODAY" as const, operator: "GT" as const, values: ["100"] },
      { metric: "roas_total", range: "PAST_3_DAYS" as const, operator: "BETWEEN" as const, values: ["0.5", "0.8"] },
    ],
  }
  expect(summarizeConditions(def, catalog)).toBe(
    "每部剧合计 · 今天消耗 > 100 且 近 3 天广告收益 ROAS 介于 0.5 ~ 0.8",
  )
  expect(summarizeAction(def)).toBe("暂停 → 广告组")
})
```

- [ ] **Step 3: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/automation-rule-form.spec.ts`
Expected: FAIL(模块不存在)

- [ ] **Step 4: 实现 `ruleForm.ts` 与 `queries.ts`**

`ruleForm.ts`:导出上述常量与函数;`emptyDefinition()` 返回 `accounts {mode:"ALL"}`、`apply_to {level:"adgroup", status:"ACTIVE"}`、`aggregation "EACH"`、一个条件 `spend TODAY GT ""`、`action {type:"PAUSE", level:"adgroup"}`、`mode "NOTIFY"`、`max_targets 200`、`schedule {frequency:"30M", window:"CONTINUOUS"}`、`notification {enabled:true, timing:"EVERY_RUN", notify_without_hits:false}`。`actionLevels` 用层级顺序 `campaign < adgroup < ad < material` 与 `ACTION_LEVELS`(同后端)。摘要中运算符:`GT ">"`、`LT "<"`、`EQ "="`、`BETWEEN "介于 a ~ b"`、文本 `包含 / 不包含 / 开头是 / 结尾是`;属性条件无时间周期前缀。`queries.ts` 参照 `features/strategies/queries.ts` 写法,`queryFn` 调用 `AutomationRulesService` 对应方法并返回 `.data!`。

- [ ] **Step 5: 运行测试** → PASS;`bun run lint` 无报错。

- [ ] **Step 6: 提交** 生成的客户端、`queries.ts`、`ruleForm.ts`、测试,`frontend: 自动规则客户端与表单逻辑`。

---

### Task 11: 自动规则页面(规则管理、编辑器、试运行)

**Files:**
- Create: `frontend/src/features/automation-rules/AutomationRulesPage.tsx`、`RuleList.tsx`、`RuleEditorSheet.tsx`、`ConditionRows.tsx`、`DryRunPanel.tsx`
- Create: `frontend/src/routes/_layout/automation-rules.tsx`、`frontend/src/routes/_layout/tenants.$tenantId.automation-rules.tsx`
- Modify: `frontend/src/routes/_layout.tsx`(工作菜单在「投放策略」后加 `{ icon: Workflow, title: "自动规则", path: \`${prefix}/automation-rules\` }`)、`frontend/src/components/Sidebar/AppSidebar.tsx`(默认工作菜单同步加入 `/automation-rules`)
- Test: `frontend/tests/utils/automationBoundary.ts`、`frontend/tests/automation-rules.spec.ts`

**Interfaces:**
- Consumes: Task 10 的查询与表单函数;`useTenantScope`、`WorkspacePageTitle`、`ManagementSheet`(`wide`)、`canManage`、`handleApiError`。
- 页面:`AutomationRulesPage` 本任务只有「规则管理」标签;Task 12 加入「执行记录」标签和搜索参数 `tab`、`run`。

- [ ] **Step 1: 写测试边界** `tests/utils/automationBoundary.ts`

参照 `tests/utils/adsBoundary.ts`:注入 `access_token`,拦截 `**/api/**`:`/api/users/me`、`/api/me/tenants`(角色可选,默认 `tenant_admin`)、`/tenants/{T}/bcs`(甲 BC、乙 BC)、`/automation-rules/catalog`(返回 spend、roas_total、name、budget 四个指标与 action_levels)、`GET /automation-rules`(返回 `options.rules`,默认一条「剧级止损」)、`POST /automation-rules`(记录请求体到 `options.created`,返回 201 规则)、`POST /automation-rules/dry-run`(返回 `checked_count 3, hit_count 1, skipped_count 0, skipped_accounts [{advertiser_id:"acc-9", reason:"STALE_REPORT"}], hits [{subject_label:"总裁归来", drama_name:"嘉书-总裁归来", metrics:{"spend:TODAY":"120"}, target_count:4, targets:[...]}]`)、`POST /automation-rules/{id}/enabled`、`/automation-runs`、`/automation-runs/{id}`、`/automation-settings`。导出 `TENANT`、`automationBoundary(page, options)`。

- [ ] **Step 2: 写失败测试** `tests/automation-rules.spec.ts`

```ts
import { expect, test } from "@playwright/test"
import { automationBoundary, TENANT } from "./utils/automationBoundary"

test("规则列表显示摘要与模式", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("heading", { name: "自动规则" })).toBeVisible()
  const row = page.getByRole("row", { name: /剧级止损/ })
  await expect(row).toContainText("每部剧合计 · 今天消耗 > 100")
  await expect(row).toContainText("仅通知")
})

test("新建规则:联动选项并提交正确的定义", async ({ page }) => {
  const created: unknown[] = []
  await automationBoundary(page, { created })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("textbox", { name: "规则名称" }).fill("广告组止损")
  await page.getByRole("radio", { name: "每部剧合计" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("100")
  await page.getByRole("button", { name: "添加条件" }).click()
  await page.getByRole("combobox", { name: "条件 2 指标" }).click()
  await page.getByRole("option", { name: "广告收益 ROAS" }).click()
  await page.getByRole("combobox", { name: "条件 2 运算符" }).click()
  await page.getByRole("option", { name: "小于" }).click()
  await page.getByRole("spinbutton", { name: "条件 2 数值" }).fill("0.8")
  await expect(page.getByRole("radio", { name: "自动执行" })).toBeDisabled()
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => created.length).toBe(1)
  expect(created[0]).toMatchObject({
    name: "广告组止损",
    definition: {
      aggregation: "DRAMA",
      mode: "NOTIFY",
      conditions: [
        { metric: "spend", range: "TODAY", operator: "GT", values: ["100"] },
        { metric: "roas_total", range: "TODAY", operator: "LT", values: ["0.8"] },
      ],
    },
  })
})

test("试运行展示命中与跳过账户", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await page.getByRole("button", { name: "新建规则" }).click()
  await page.getByRole("spinbutton", { name: "条件 1 数值" }).fill("10")
  await page.getByRole("button", { name: "试运行" }).click()
  await expect(page.getByText("判断 3 个对象,命中 1 个")).toBeVisible()
  await expect(page.getByRole("cell", { name: "总裁归来" })).toBeVisible()
  await expect(page.getByText("acc-9")).toBeVisible()
})

test("只读成员看不到写操作", async ({ page }) => {
  await automationBoundary(page, { role: "viewer" })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("row", { name: /剧级止损/ })).toBeVisible()
  await expect(page.getByRole("button", { name: "新建规则" })).toHaveCount(0)
})
```

- [ ] **Step 3: 运行确认失败**

- [ ] **Step 4: 实现页面与组件**

- `AutomationRulesPage`:`WorkspacePageTitle` 标题「自动规则」、描述「按条件自动检查跨账户、跨 BC 的投放数据并发送飞书预警。」;`Tabs`(本任务只有「规则管理」);内容 `RuleList`。
- `RuleList`:`useQuery(rulesQuery)`;工具栏「新建规则」(`write = scope.role !== "viewer"` 时显示)、按名称搜索(前端过滤);`Table` 列:开关(`Switch` 不在组件库,用 `Checkbox` 带 `aria-label="启用 {name}"`,调用 `/enabled`)、规则名、条件摘要 `summarizeConditions`、操作 `summarizeAction`、执行模式 `Badge`、适用范围 `summarizeScope`、运行频率、最近一次运行(`last_run` 的时间与「命中 n」,失败显示错误码)、行操作「编辑」「复制」「试运行」「删除」(删除用 `Dialog` 确认)。复制=以原定义打开编辑器、名称加「 副本」。
- `RuleEditorSheet`(`ManagementSheet wide`,`dirty` 为表单是否改动):卡片顺序与设计第 7.2 节一致:适用账户(`RadioGroup` 全部账户 / 按 BC / 指定账户;按 BC 时 BC 多选用 `Checkbox` 列表,数据来自 `/bcs`;指定账户时文本框按行或逗号输入账户 ID——账户勾选列表放阶段 3)、应用至(层级 `Select`、状态 `RadioGroup`、指定对象时文本框输入 `账户ID:对象ID` 每行一个、可展开「更多过滤」:剧名(逗号分隔)、名称包含、名称不包含)、判断对象(`RadioGroup` 三项,`aria-label` 即中文名)、条件(`ConditionRows`)、执行操作(操作 `Select`、执行层级 `Select` 只列 `actionLevels(...)`,为空时显示提示「当前判断对象不支持该操作」;调整类显示调整方式、数值、上/下限、频控;执行模式 `RadioGroup`,「自动执行」「确认后执行」`disabled` 并附说明「第二阶段开放」;单次执行对象上限输入框)、运行排期(频率 `Select`,低于 `minFrequency` 的选项禁用;生效时段 `RadioGroup`:持续 / 自定义(时间段行:日期类型、星期或日期多选、开始、结束,最多 7 行)/ 仅一次(日期时间输入,按浏览器时区转 ISO))、飞书通知(启用、无命中也发送;租户未配置时显示「去设置」链接到 `/tenants/$tenantId/notifications`)、规则名称(`aria-label="规则名称"`,默认「规则 yyyyMMddHHmm」)。底部「试运行」「保存」;保存时组装 `RuleDefinition`(数值转字符串、去空值),创建调 `POST`,编辑调 `PUT` 带 `expected_version`;409 版本冲突提示「规则已被他人修改,请刷新后重试」;422 显示后端错误。
- `ConditionRows`:每行:指标 `Select`(`aria-label="条件 n 指标"`,选项按分组 投放数据 / 收益数据 / 对象属性,只列 `metricOptions(catalog, level, aggregation)`)、时间周期 `Select`(`ranged` 时显示,`aria-label="条件 n 时间周期"`)、运算符 `Select`(`aria-label="条件 n 运算符"`,中文:大于 / 小于 / 介于 / 等于 / 包含…)、数值(数值类 `Input type="number"`,`aria-label="条件 n 数值"`;介于时两个输入 `条件 n 下限`、`条件 n 上限`;文本类 `type="text"`)、单位后缀、删除按钮;下方「添加条件 (n/5)」按钮(`aria-label="添加条件"`)、提示「全部满足才触发」。切换指标时重置运算符与时间周期为该指标第一个可选值。
- `DryRunPanel`:在编辑器内显示;调用 `dry-run`;展示「判断 X 个对象,命中 Y 个」、跳过账户列表(「数据未更新,已跳过」)、命名不规范跳过数、命中表格(对象、剧、指标值、涉及对象数,点击展开前 50 个目标)。
- 路由:`_layout/automation-rules.tsx` 用 `WorkspaceEntry`(title「自动规则」);`tenants.$tenantId.automation-rules.tsx` 渲染 `AutomationRulesPage`,`validateSearch` 接收 `tab`、`run`。

- [ ] **Step 5: 运行测试**

Run: `cd frontend && bunx playwright test tests/automation-rules.spec.ts tests/automation-rule-form.spec.ts && bun run lint && bun run build`
Expected: 全部通过,构建成功。

- [ ] **Step 6: 提交** `frontend: 自动规则管理、编辑器与试运行`。

---

### Task 12: 执行记录与通知设置页面、进度记录

**Files:**
- Create: `frontend/src/features/automation-rules/RunHistory.tsx`、`NotificationSettingsPage.tsx`、`frontend/src/routes/_layout/tenants.$tenantId.notifications.tsx`
- Modify: `frontend/src/features/automation-rules/AutomationRulesPage.tsx`(新增「执行记录」标签)、`frontend/src/routes/_layout.tsx`(租户管理菜单在 `canManage` 时加入「通知设置」)
- Modify: `frontend/tests/automation-rules.spec.ts`、`docs/implementation-progress.md`

**Interfaces:**
- Consumes: `runsQuery`、`runQuery`、`settingsQuery`;接口 `PUT /automation-settings`、`POST /automation-settings/test-feishu`。

- [ ] **Step 1: 写失败测试**(追加到 `automation-rules.spec.ts`)

```ts
test("执行记录下钻到命中明细", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs`)
  const row = page.getByRole("row", { name: /剧级止损/ })
  await expect(row).toContainText("命中 1")
  await expect(row).toContainText("已发送")
  await row.getByRole("button", { name: "查看明细" }).click()
  await expect(page.getByRole("cell", { name: "总裁归来" })).toBeVisible()
})

test("飞书卡片链接直接打开指定运行", async ({ page }) => {
  await automationBoundary(page)
  await page.goto(`/tenants/${TENANT}/automation-rules?tab=runs&run=run-1`)
  await expect(page.getByRole("cell", { name: "总裁归来" })).toBeVisible()
})

test("通知设置:掩码显示、测试与保存", async ({ page }) => {
  const saved: unknown[] = []
  const tested: unknown[] = []
  await automationBoundary(page, { saved, tested })
  await page.goto(`/tenants/${TENANT}/notifications`)
  await expect(page.getByText("https://open.feishu.cn/open-apis/bot/v2/hook/…ab12")).toBeVisible()
  await page.getByRole("textbox", { name: "Webhook 地址" }).fill("https://open.feishu.cn/open-apis/bot/v2/hook/new-1")
  await page.getByRole("button", { name: "发送测试消息" }).click()
  await expect.poll(() => tested.length).toBe(1)
  await page.getByRole("button", { name: "保存" }).click()
  await expect.poll(() => saved.length).toBe(1)
  expect(saved[0]).toMatchObject({ webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/new-1" })
})

test("非管理员没有通知设置入口", async ({ page }) => {
  await automationBoundary(page, { role: "operator" })
  await page.goto(`/tenants/${TENANT}/automation-rules`)
  await expect(page.getByRole("link", { name: "通知设置" })).toHaveCount(0)
})
```

边界补充:`GET /automation-runs` 返回一条 `{id:"run-1", rule_name:"剧级止损", hit_count:1, notify_status:"SENT", ...}`;`GET /automation-runs/run-1` 返回明细;`GET /automation-settings` 返回 `feishu_url_hint` 为上面的掩码;`PUT`、`test-feishu` 分别记录请求体到 `saved`、`tested`。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

- `RunHistory`:筛选「规则」(`Select`,来自规则列表,含「全部规则」);表格列:规则、开始时间、判断对象数、命中、跳过(账户数 + 命名不规范数)、状态(完成 / 失败 + 错误码中文:`rule_owner_forbidden`「规则创建人已无权限」、`evaluation_failed`「判断失败」)、飞书(待发送 / 已发送 / 发送失败 / 无需发送,失败时悬停显示 `notify_error`)、「查看明细」。明细用 `Sheet`:运行摘要 + 命中表(对象、剧、账户、指标值按「时间周期+指标名」显示、涉及对象数,可展开目标列表)。搜索参数 `run` 存在时自动打开该运行。
- `NotificationSettingsPage`(仅 `canManage`;否则显示无权限):卡片「飞书群机器人」:当前状态(已配置 + 掩码 / 未配置)、`Webhook 地址` 输入(留空表示保持原值)、`签名密钥` 输入(`PasswordInput`,留空保持)、启用 `Checkbox`、按钮「发送测试消息」(有输入时测试输入值,否则测试已保存值)、「删除配置」;卡片「运行设置」:运行时区 `Select`(Asia/Shanghai、Asia/Hong_Kong、UTC、America/Los_Angeles、America/New_York、Europe/London)、「暂停全部规则」`Checkbox`;底部「保存」调用 `PUT`。
- `_layout.tsx` 租户管理菜单:`canManage` 时加入 `{ icon: Bell, title: "通知设置", path: \`${prefix}/notifications\` }`。

- [ ] **Step 4: 运行全部前端测试与构建**

Run: `cd frontend && bunx playwright test tests/automation-rules.spec.ts tests/automation-rule-form.spec.ts && bun run lint && bun run build`
Expected: 通过。

- [ ] **Step 5: 后端回归**

Run: `cd backend && source /tmp/arres/testenv.sh && uv run --frozen pytest tests/modules/automation_rules tests/modules/reporting tests/jobs tests/test_migration_graph.py -q && uv run --frozen ruff check app tests && uv run --frozen ruff format --check app tests && uv run --frozen mypy app/modules/automation_rules`
Expected: 全部通过(如 mypy 在既有模块有历史诊断,只要求新模块零诊断)。

- [ ] **Step 6: 记录进度**:在 `docs/implementation-progress.md` 顶部新增「2026-10-14:自动规则阶段 1」小节,写明交付内容、验证命令与结果、未验证项(真实飞书群发送、生产开关取值待确认)、下一步阶段 2。

- [ ] **Step 7: 提交** `frontend: 自动规则执行记录与飞书通知设置;docs: 记录自动规则阶段 1`(前端文件与进度文档分两次提交:`frontend: ...`、`docs: ...`)。
