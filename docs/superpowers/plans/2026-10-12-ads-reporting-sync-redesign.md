# 广告报表数据同步重新设计 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用"BC 报表筛选活跃账户 + 只拉广告日明细（另加系列小时、UTC 系列日、素材日、账户对账）"替换现有报表同步与存储，所有维度由广告数据在数据库内汇总，支撑单应用 1 万个以上账户。

**Architecture:** 新建一套按"租户 + 广告账户"存放的分区事实表，每个"账户 × 报表类型 × 日期段"整份替换发布。调度由每个 BC 一行的计划表驱动，Beat 每 30 秒扫描到期计划，生成一次请求一行的任务（`report_sync_job`），任务按租户轮转、按优先级领取，只经官方 SDK 通道调用并复用现有额度准入。查询层改为 SQL 汇总，保留现有 `ReportRow`/快照/冻结选择合同，旧的事实、覆盖、暂存、观测、调度和 MCP/异步报表路径在最后一个任务删除。

**Tech Stack:** Python 3.14、FastAPI、SQLModel/SQLAlchemy、PostgreSQL 声明式分区、Alembic、Celery + Redis、官方 `business_api_client` SDK；前端 React + TypeScript + TanStack Query + shadcn/ui。

**Spec:** `docs/superpowers/specs/2026-10-12-ads-reporting-sync-redesign-design.md`（同时阅读 `docs/superpowers/specs/2026-09-30-ads-management-reporting-design.md` 第 3、4、8 节：工作台、剧识别、批量操作规则继续有效）。

## Global Constraints

- 报表同步只走 `OFFICIAL_API` 通道；只绑定 MCP 的 BC 不同步，页面提示"未接入 API，无法同步报表"。不使用异步报表、订阅推送。
- 授权后自动同步，无按 BC 开关；`ADS_SYNC_ENABLED=false` 时不生成、不执行任何报表或目录同步任务（含手动刷新）。
- 广告日明细：`report_type=BASIC`、`data_level=AUCTION_AD`、维度 `["ad_id_v2","stat_time_day"]`、过滤 `ad_status=STATUS_ALL`、`advertiser_ids` 最多 5 个、`page_size=1000`。
- 日期按广告账户时区；UTC 版本只存系列日（`multi_adv_report_in_utc_time=true`）。
- 回收指标列名固定：`revenue_d0, revenue_d3, revenue_d7, revenue_d14, revenue_d30, revenue_total`；基础报表映射 `native_growth_ad_revenue_value_d0/d2/d6/d13/d29`、`native_growth_total_ad_impression_value`；素材报表映射 `onsite_ad_impression_ad_revenue_day0/day6/day13`、`onsite_total_ad_impression_value`，素材无 D3、D30。
- 平台返回 `-` 存 NULL，不存 0；金额 `numeric` 不限精度；汇总时任一成员为 NULL 则合计为 NULL。
- 同步报表返回 `total_number >= 20000` 视为截断风险：拆分重拉，无法拆分则任务失败、不发布。
- 回刷窗口 32 天；每日回刷在 UTC 12:30 之后；热刷 30 分钟、素材 60 分钟。
- 平台限流 `code 40100`：延后不失败（QPM 等 300 秒，QPD 等到次日 UTC 0 点）；读取错误最多 5 次指数退避（60s 起，最长 30 分钟）；合同错误（40002 等）立即失败。
- 所有新增运行参数（`TIKTOK_REPORT_DAILY_CALL_BUDGET`）同一提交补齐 Settings、`.env.example`、部署手册。
- 遵循仓库 `AGENTS.md`：中文业务注释、不保留旧行为兼容代码、每个任务验证后聚焦提交、只暂存本任务文件、禁止 `git add .`。
- 测试使用真实 PostgreSQL 与 Redis（见下方"测试环境"），外部 TikTok 只在 SDK 传输边界替身。

## Review Focus

1. **账户日期边界与时区**：不同时区账户合并在同一请求里，返回的 `stat_time_day` 已是各账户本地日期；发布时必须按每行自身账户归属日期替换，不能用请求的日期段统一删除别的账户的行。（Task 5 测试 `test_replace_only_touches_requested_accounts_and_dates`）
2. **今天被删除的广告**：热刷也带 `STATUS_ALL`（规格要求不漏数据），删除的广告当天消耗仍计入汇总。（Task 4 测试 `test_ad_day_payload_always_includes_status_all`）
3. **账户今天由活跃变为 0 消耗**：热刷筛选不到它，但它今天早些时候已写入的数据不能被遗留为"过期"或被误删；下一轮每日回刷才覆盖。（Task 7 测试 `test_inactive_account_keeps_published_rows_until_daily_refresh`）
4. **同一账户被两个 BC 授权**：两个 BC 都能看到数据，计划不重复拉取同一账户。（Task 7 测试 `test_shared_account_is_planned_once`；Task 11 测试 `test_shared_account_visible_in_both_bcs`）
5. **平台截断**：5 个账户合并时触顶自动减少合并数，单账户触顶按系列 30 个一批，系列批仍触顶则失败可见。（Task 6 测试 `test_truncation_splits_then_fails_visibly`）

## 测试环境

每次运行后端测试前：

```bash
docker rm -fv rpt-pg rpt-redis >/dev/null 2>&1
docker run -d --name rpt-pg -p 127.0.0.1:55432:5432 --tmpfs /var/lib/postgresql:rw,size=2g \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=localtest -e POSTGRES_DB=rpt_test postgres:18
docker run -d --name rpt-redis -p 127.0.0.1:56379:6379 redis:8 --databases 64
export DATABASE_URL=postgresql://postgres:localtest@127.0.0.1:55432/rpt_test
export REDIS_URL=redis://127.0.0.1:56379/0 TEST_REDIS_URL=redis://127.0.0.1:56379/1
export FASTAPI_ENV=development APP_ENV_FILE=/dev/null DOTENV_CONFIG_PATH=/dev/null UV_NO_ENV_FILE=1
export FIRST_SUPERUSER=admin FIRST_SUPERUSER_PASSWORD=ci-only SECRET_KEY=ci-only
cd backend
```

下文 `pytest` 均指 `uv run --frozen pytest -q -p no:cacheprovider`。

## 文件结构

新增：

| 文件 | 职责 |
| --- | --- |
| `backend/app/alembic/versions/0023_reporting_store.py` | 新表与分区 |
| `backend/app/modules/reporting/store_models.py` | 新表 ORM 模型 |
| `backend/app/modules/reporting/partitions.py` | 按月分区创建 |
| `backend/app/modules/reporting/metrics.py` | 平台字段 → 存储列映射、解析 |
| `backend/app/integrations/tiktok/contracts/report_reader.py` | 读取接口与 `RawReportPage` |
| `backend/app/integrations/tiktok/adapters/sdk_report_reader.py` | 官方 SDK 报表请求 |
| `backend/app/modules/reporting/publish.py` | 整份替换、系列汇总、同步状态 |
| `backend/app/modules/reporting/jobs.py` | 任务入队、领取、执行、重试 |
| `backend/app/modules/reporting/planner.py` | BC/账户计划、筛选、分批、档位调度 |
| `backend/app/modules/reporting/job_tasks.py` | Celery 入口 |
| `backend/app/modules/ads/directory_requests.py` | 目录同步请求（从旧调度迁出） |
| `backend/app/modules/reporting/dimension_query.py` | 五维 SQL 汇总、排序、完整性 |
| `backend/app/modules/reporting/material_query.py` | 素材维度 SQL 汇总与使用位置 |
| `backend/tests/modules/reporting/sync_conftest.py`（由 `conftest.py` 导入） | 新测试种子 |

修改：`integrations/tiktok/official/accounts.py`、`integrations/tiktok/gateway.py`、`modules/reporting/{api.py,queries.py,schemas.py,filters.py,exports.py,selection.py}`、`modules/ads/sync.py`、`modules/ad_management/execution.py`、`jobs/celery_app.py`、`core/config.py`、`.env.example`、`docs/runbooks/deployment.md`、前端 `features/ads/*`、`frontend/src/client/*`（生成）。

删除（Task 14）：`modules/reporting/{contracts.py,facts.py,sync.py,scheduling.py,tasks.py,aggregation.py,trends.py}`、`modules/reporting/models.py` 中 `ReportFact/ReportCoverage/ReportObservation`、`modules/reporting/sync_models.py`、`integrations/tiktok/adapters/{sdk_reporting.py,mcp_reporting.py}`、`integrations/tiktok/contracts/reporting.py` 及对应测试。

---

### Task 1: 新存储表与按月分区

**Files:**
- Create: `backend/app/modules/reporting/store_models.py`
- Create: `backend/app/modules/reporting/partitions.py`
- Create: `backend/app/alembic/versions/0023_reporting_store.py`
- Modify: `backend/app/models.py`（导入新模型以注册元数据，查看文件现有导入写法后追加一行）
- Test: `backend/tests/modules/reporting/test_store_schema.py`

**Interfaces:**
- Produces: ORM 模型 `AdReportDay`, `CampaignReportDay`, `CampaignReportDayUtc`, `CampaignReportHour`, `MaterialReportDay`, `AccountReconciliationDay`, `ReportSyncState`, `ReportSyncJob`, `AdSyncBc`, `AdSyncAccount`；常量 `METRIC_COLUMNS: tuple[str, ...]`；函数 `ensure_month_partitions(session, *, months_ahead: int = 3, today: date | None = None) -> None`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_store_schema.py
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.modules.reporting.partitions import ensure_month_partitions
from app.modules.reporting.store_models import AdReportDay, ReportSyncJob


def test_ad_report_day_is_partitioned_and_accepts_rows(session):
    ensure_month_partitions(session, today=date(2026, 10, 12))
    parts = session.execute(text(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
        "JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='ad_report_day'"
    )).scalars().all()
    assert "ad_report_day_2026_10" in parts and "ad_report_day_2027_01" in parts
    assert "ad_report_day_default" in parts
    session.add(AdReportDay(
        tenant_id=uuid4(), advertiser_id="a1", stat_date=date(2026, 10, 5), ad_id="ad1",
        campaign_id="c1", adgroup_id="g1", currency="USD", timezone="UTC",
        spend=Decimal("1.23"), revenue_d0=None, fetched_at=datetime.now(UTC),
        sync_job_id=uuid4(),
    ))
    session.flush()


def test_ensure_month_partitions_is_idempotent(session):
    ensure_month_partitions(session, today=date(2026, 10, 12))
    ensure_month_partitions(session, today=date(2026, 10, 12))


def test_job_status_check_rejects_unknown(session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    session.add(ReportSyncJob(
        tenant_id=uuid4(), bc_id="bc", kind="ad_day", priority=1, status="BOGUS",
        advertiser_ids=["a"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1),
        job_key="k", frozen_route={}, actor_id=uuid4(), due_at=datetime.now(UTC),
    ))
    with pytest.raises(IntegrityError):
        session.flush()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/modules/reporting/test_store_schema.py`
Expected: FAIL（`ModuleNotFoundError: app.modules.reporting.store_models`）

- [ ] **Step 3: 写模型**

```python
# backend/app/modules/reporting/store_models.py
"""报表事实按“租户 + 广告账户”存放，不挂在 BC 下；BC 可见性由当前授权决定。

一行一个对象一个时间桶，指标各占一列；平台 '-' 存 NULL，金额保留平台精度。
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, Index, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime

# 所有日/小时事实共享的指标列；顺序即 API 与导出列序。
METRIC_COLUMNS = (
    "spend", "impressions", "clicks", "ad_impression_events",
    "revenue_d0", "revenue_d3", "revenue_d7", "revenue_d14", "revenue_d30", "revenue_total",
)


def _num() -> Any:
    return Field(default=None, sa_column=Column(Numeric(), nullable=True))


class _Metrics(SQLModel):
    spend: Decimal | None = _num()
    impressions: Decimal | None = _num()
    clicks: Decimal | None = _num()
    ad_impression_events: Decimal | None = _num()
    revenue_d0: Decimal | None = _num()
    revenue_d3: Decimal | None = _num()
    revenue_d7: Decimal | None = _num()
    revenue_d14: Decimal | None = _num()
    revenue_d30: Decimal | None = _num()
    revenue_total: Decimal | None = _num()


class _Fact(_Metrics):
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    currency: str = Field(max_length=16)
    timezone: str = Field(max_length=64)
    fetched_at: datetime = Field(sa_type=AwareDateTime)
    sync_job_id: UUID


class AdReportDay(_Fact, table=True):
    """唯一明细来源：一条广告（ad_id_v2）一天；按月分区（迁移中声明）。"""
    __tablename__ = "ad_report_day"
    stat_date: date = Field(primary_key=True)
    ad_id: str = Field(primary_key=True, max_length=64)
    campaign_id: str = Field(max_length=64)
    adgroup_id: str = Field(max_length=64)
    campaign_automation_type: str | None = Field(default=None, max_length=64)
    ad_name: str | None = None
    campaign_name: str | None = None
    adgroup_name: str | None = None


class CampaignReportDay(_Fact, table=True):
    """由 ad_report_day 在同一事务汇总写入的查询加速表，不独立拉取。"""
    __tablename__ = "campaign_report_day"
    stat_date: date = Field(primary_key=True)
    campaign_id: str = Field(primary_key=True, max_length=64)
    campaign_name: str | None = None


class CampaignReportDayUtc(_Fact, table=True):
    """UTC 自然日系列数据，供日报与跨账户 UTC 汇总。"""
    __tablename__ = "campaign_report_day_utc"
    stat_date: date = Field(primary_key=True)
    campaign_id: str = Field(primary_key=True, max_length=64)


class CampaignReportHour(_Fact, table=True):
    """系列小时数据，只保留最近 30 天；stat_hour 为该账户本地整点对应的 UTC 时刻。"""
    __tablename__ = "campaign_report_hour"
    stat_hour: datetime = Field(primary_key=True, sa_type=AwareDateTime)
    campaign_id: str = Field(primary_key=True, max_length=64)


class MaterialReportDay(_Fact, table=True):
    """Smart+ 素材分时报表：主素材一天；按月分区。素材无 D3、D30（恒为 NULL）。"""
    __tablename__ = "material_report_day"
    stat_date: date = Field(primary_key=True)
    main_material_id: str = Field(primary_key=True, max_length=64)
    main_material_type: str = Field(max_length=64)
    main_material_name: str | None = None


class AccountReconciliationDay(SQLModel, table=True):
    """账户报表原值与广告汇总值的每日对账，只用于提示，不进入展示。"""
    __tablename__ = "account_reconciliation_day"
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    stat_date: date = Field(primary_key=True)
    account_spend: Decimal | None = _num()
    ad_sum_spend: Decimal | None = _num()
    account_revenue_total: Decimal | None = _num()
    ad_sum_revenue_total: Decimal | None = _num()
    checked_at: datetime = Field(sa_type=AwareDateTime)


REPORT_KINDS = ("ad_day", "campaign_hour", "campaign_day_utc", "material_day", "account_day")


class ReportSyncState(SQLModel, table=True):
    """每个账户、每种报表、每一天的发布状态；页面据此显示更新时间与缺口。"""
    __tablename__ = "report_sync_state"
    __table_args__ = (
        CheckConstraint("status IN ('COMPLETE','STALE','FAILED')", name="ck_report_sync_state_status"),
    )
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    report_kind: str = Field(primary_key=True, max_length=32)
    stat_date: date = Field(primary_key=True)
    status: str = Field(max_length=16)
    row_count: int = 0
    last_success_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    error_code: str | None = Field(default=None, max_length=128)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime)


class ReportSyncJob(SQLModel, table=True):
    """一次报表请求（含全部分页）一行；领取有租约，重复投递按 job_key 幂等。"""
    __tablename__ = "report_sync_job"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','WAITING','COMPLETE','FAILED','CANCELLED')",
            name="ck_report_sync_job_status",
        ),
        Index("ix_report_sync_job_due", "status", "priority", "due_at"),
        Index(
            "uq_report_sync_job_active_key", "job_key", unique=True,
            postgresql_where="status IN ('QUEUED','RUNNING','WAITING')",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID
    bc_id: str = Field(max_length=128)
    kind: str = Field(max_length=32)
    priority: int
    status: str = Field(default="QUEUED", max_length=16)
    advertiser_ids: list[str] = Field(sa_column=Column(JSONB, nullable=False))
    campaign_ids: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    start_date: date
    end_date: date
    job_key: str = Field(max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    actor_id: UUID
    due_at: datetime = Field(sa_type=AwareDateTime)
    claim_token: UUID | None = None
    claimed_until: datetime | None = Field(default=None, sa_type=AwareDateTime)
    attempts: int = 0
    error_code: str | None = Field(default=None, max_length=128)
    request_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class AdSyncBc(SQLModel, table=True):
    """每个已绑定 API 连接的 BC 一行：冻结路由、后台身份与各档位下次执行时间。"""
    __tablename__ = "ad_sync_bc"
    tenant_id: UUID = Field(primary_key=True)
    bc_id: str = Field(primary_key=True, max_length=128)
    frozen_route: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    actor_id: UUID
    enabled: bool = True
    disabled_reason: str | None = Field(default=None, max_length=128)
    # None=未检查；False=授权用户无 BC 报表权限，退回账户级报表筛选。
    bc_report_supported: bool | None = None
    next_hot_at: datetime = Field(sa_type=AwareDateTime)
    next_material_at: datetime = Field(sa_type=AwareDateTime)
    next_directory_at: datetime = Field(sa_type=AwareDateTime)
    next_daily_at: datetime = Field(sa_type=AwareDateTime)
    last_active_advertiser_ids: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), sa_type=AwareDateTime)


class AdSyncAccount(SQLModel, table=True):
    """账户同步归属：一个账户只归一个 BC 计划拉取，避免多 BC 重复调用。"""
    __tablename__ = "ad_sync_account"
    __table_args__ = (UniqueConstraint("tenant_id", "advertiser_id", name="uq_ad_sync_account"),)
    tenant_id: UUID = Field(primary_key=True)
    advertiser_id: str = Field(primary_key=True, max_length=128)
    owner_bc_id: str = Field(max_length=128)
    history_planned_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
```

- [ ] **Step 4: 写分区工具**

```python
# backend/app/modules/reporting/partitions.py
"""按月分区：大表（广告日、素材日）每月一个分区，另有 DEFAULT 兜底。"""

from datetime import date

from sqlalchemy import text
from sqlmodel import Session

PARTITIONED_TABLES = ("ad_report_day", "material_report_day")


def _month_start(value: date, offset: int) -> date:
    month = value.month - 1 + offset
    return date(value.year + month // 12, month % 12 + 1, 1)


def ensure_month_partitions(session: Session, *, months_ahead: int = 3, today: date | None = None) -> None:
    """创建当月及未来 months_ahead 个月的分区；已存在则跳过，可重复调用。"""
    today = today or date.today()
    for table in PARTITIONED_TABLES:
        for offset in range(0, months_ahead + 1):
            start, end = _month_start(today, offset), _month_start(today, offset + 1)
            name = f"{table}_{start:%Y_%m}"
            session.execute(text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {table} "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            ))
```

注意：DEFAULT 分区若已含某月数据，`CREATE ... PARTITION OF` 会失败。历史回补写入的过去月份在 Task 7 规划历史前调用 `ensure_month_partitions_for_range(session, start, end)`；本步骤同时实现：

```python
def ensure_month_partitions_for_range(session: Session, start: date, end: date) -> None:
    """为历史区间逐月建分区，必须在写入该区间数据之前调用。"""
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        nxt = _month_start(cursor, 1)
        for table in PARTITIONED_TABLES:
            session.execute(text(
                f"CREATE TABLE IF NOT EXISTS {table}_{cursor:%Y_%m} PARTITION OF {table} "
                f"FOR VALUES FROM ('{cursor.isoformat()}') TO ('{nxt.isoformat()}')"
            ))
        cursor = nxt
```

- [ ] **Step 5: 写迁移**

```python
# backend/app/alembic/versions/0023_reporting_store.py
"""New per-account reporting store: partitioned facts, sync state, jobs and plans."""

from alembic import op

revision = "0023_reporting_store"
down_revision = "0022_provider_link_defaults"
branch_labels = None
depends_on = None

_METRICS = """
    spend numeric, impressions numeric, clicks numeric, ad_impression_events numeric,
    revenue_d0 numeric, revenue_d3 numeric, revenue_d7 numeric, revenue_d14 numeric,
    revenue_d30 numeric, revenue_total numeric,
    currency varchar(16) NOT NULL, timezone varchar(64) NOT NULL,
    fetched_at timestamptz NOT NULL, sync_job_id uuid NOT NULL
"""


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE ad_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        ad_id varchar(64) NOT NULL, campaign_id varchar(64) NOT NULL, adgroup_id varchar(64) NOT NULL,
        campaign_automation_type varchar(64), ad_name varchar, campaign_name varchar, adgroup_name varchar,
        {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, ad_id)
    ) PARTITION BY RANGE (stat_date);
    CREATE TABLE ad_report_day_default PARTITION OF ad_report_day DEFAULT;
    CREATE INDEX ix_ad_report_day_period ON ad_report_day (tenant_id, stat_date, advertiser_id);
    CREATE INDEX ix_ad_report_day_campaign ON ad_report_day (tenant_id, campaign_id, stat_date);

    CREATE TABLE material_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        main_material_id varchar(64) NOT NULL, main_material_type varchar(64) NOT NULL,
        main_material_name varchar,
        {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, main_material_id)
    ) PARTITION BY RANGE (stat_date);
    CREATE TABLE material_report_day_default PARTITION OF material_report_day DEFAULT;
    CREATE INDEX ix_material_report_day_period ON material_report_day (tenant_id, stat_date, advertiser_id);
    CREATE INDEX ix_material_report_day_material ON material_report_day (tenant_id, main_material_id, stat_date);

    CREATE TABLE campaign_report_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        campaign_id varchar(64) NOT NULL, campaign_name varchar, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, campaign_id)
    );
    CREATE INDEX ix_campaign_report_day_period ON campaign_report_day (tenant_id, stat_date, advertiser_id);

    CREATE TABLE campaign_report_day_utc (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        campaign_id varchar(64) NOT NULL, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_date, campaign_id)
    );

    CREATE TABLE campaign_report_hour (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_hour timestamptz NOT NULL,
        campaign_id varchar(64) NOT NULL, {_METRICS},
        PRIMARY KEY (tenant_id, advertiser_id, stat_hour, campaign_id)
    );

    CREATE TABLE account_reconciliation_day (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, stat_date date NOT NULL,
        account_spend numeric, ad_sum_spend numeric, account_revenue_total numeric,
        ad_sum_revenue_total numeric, checked_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, advertiser_id, stat_date)
    );

    CREATE TABLE report_sync_state (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, report_kind varchar(32) NOT NULL,
        stat_date date NOT NULL, status varchar(16) NOT NULL, row_count integer NOT NULL DEFAULT 0,
        last_success_at timestamptz, error_code varchar(128), updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, advertiser_id, report_kind, stat_date),
        CONSTRAINT ck_report_sync_state_status CHECK (status IN ('COMPLETE','STALE','FAILED'))
    );

    CREATE TABLE report_sync_job (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, bc_id varchar(128) NOT NULL,
        kind varchar(32) NOT NULL, priority integer NOT NULL, status varchar(16) NOT NULL,
        advertiser_ids jsonb NOT NULL, campaign_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        start_date date NOT NULL, end_date date NOT NULL, job_key varchar(128) NOT NULL,
        frozen_route jsonb NOT NULL, actor_id uuid NOT NULL, due_at timestamptz NOT NULL,
        claim_token uuid, claimed_until timestamptz, attempts integer NOT NULL DEFAULT 0,
        error_code varchar(128), request_count integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL, finished_at timestamptz,
        CONSTRAINT ck_report_sync_job_status CHECK
            (status IN ('QUEUED','RUNNING','WAITING','COMPLETE','FAILED','CANCELLED'))
    );
    CREATE INDEX ix_report_sync_job_due ON report_sync_job (status, priority, due_at);
    CREATE UNIQUE INDEX uq_report_sync_job_active_key ON report_sync_job (job_key)
        WHERE status IN ('QUEUED','RUNNING','WAITING');

    CREATE TABLE ad_sync_bc (
        tenant_id uuid NOT NULL, bc_id varchar(128) NOT NULL, frozen_route jsonb NOT NULL,
        actor_id uuid NOT NULL, enabled boolean NOT NULL DEFAULT true, disabled_reason varchar(128),
        bc_report_supported boolean, next_hot_at timestamptz NOT NULL,
        next_material_at timestamptz NOT NULL, next_directory_at timestamptz NOT NULL,
        next_daily_at timestamptz NOT NULL, last_active_advertiser_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        updated_at timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, bc_id),
        FOREIGN KEY (tenant_id, bc_id) REFERENCES tenant_bc (tenant_id, bc_id)
    );

    CREATE TABLE ad_sync_account (
        tenant_id uuid NOT NULL, advertiser_id varchar(128) NOT NULL, owner_bc_id varchar(128) NOT NULL,
        history_planned_at timestamptz,
        PRIMARY KEY (tenant_id, advertiser_id)
    );
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE ad_sync_account; DROP TABLE ad_sync_bc; DROP TABLE report_sync_job;
    DROP TABLE report_sync_state; DROP TABLE account_reconciliation_day;
    DROP TABLE campaign_report_hour; DROP TABLE campaign_report_day_utc;
    DROP TABLE campaign_report_day; DROP TABLE material_report_day; DROP TABLE ad_report_day;
    """)
```

在执行本步骤前运行 `uv run --frozen alembic heads`；若 head 已不是 `0022_provider_link_defaults`，把 `down_revision` 改为实际 head，并在提交说明中写明。

- [ ] **Step 6: 运行测试**

Run: `pytest tests/modules/reporting/test_store_schema.py`
Expected: 3 passed

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/reporting/store_models.py backend/app/modules/reporting/partitions.py \
  backend/app/alembic/versions/0023_reporting_store.py backend/app/models.py \
  backend/tests/modules/reporting/test_store_schema.py
git commit -m "reporting: 新增按账户存放的分区报表事实表与同步任务表"
```

---

### Task 2: 指标映射与行解析

**Files:**
- Create: `backend/app/modules/reporting/metrics.py`
- Test: `backend/tests/modules/reporting/test_metrics.py`

**Interfaces:**
- Consumes: `METRIC_COLUMNS`（Task 1）
- Produces:
  - `AD_DAY_METRICS: tuple[str, ...]`、`CAMPAIGN_METRICS`、`MATERIAL_METRICS`、`ACCOUNT_METRICS`（请求平台的 metrics 列表）
  - `parse_decimal(value: object) -> Decimal | None`
  - `basic_metric_values(metrics: dict) -> dict[str, Decimal | None]`（键为 `METRIC_COLUMNS`）
  - `material_metric_values(metrics: dict) -> dict[str, Decimal | None]`
  - `parse_local_day(raw: str) -> date`、`parse_local_hour(raw: str, timezone: str) -> datetime`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_metrics.py
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.errors import DomainError
from app.modules.reporting.metrics import (
    AD_DAY_METRICS, basic_metric_values, material_metric_values, parse_decimal,
    parse_local_day, parse_local_hour,
)


def test_dash_is_null_not_zero():
    assert parse_decimal("-") is None and parse_decimal(None) is None
    assert parse_decimal("0.00") == Decimal("0.00")


def test_rejects_non_finite_and_float():
    for bad in ("NaN", "Infinity", 1.5, "abc"):
        with pytest.raises(DomainError):
            parse_decimal(bad)


def test_basic_mapping_uses_platform_day_offsets():
    values = basic_metric_values({
        "spend": "10", "impressions": "100", "clicks": "5",
        "native_growth_total_ad_impression_event_count": "3",
        "native_growth_ad_revenue_value_d0": "1", "native_growth_ad_revenue_value_d2": "2",
        "native_growth_ad_revenue_value_d6": "3", "native_growth_ad_revenue_value_d13": "4",
        "native_growth_ad_revenue_value_d29": "5", "native_growth_total_ad_impression_value": "-",
    })
    assert values["revenue_d3"] == Decimal("2") and values["revenue_d30"] == Decimal("5")
    assert values["revenue_total"] is None
    assert "campaign_id" in AD_DAY_METRICS and "native_growth_ad_revenue_value_d29" in AD_DAY_METRICS


def test_material_has_no_d3_d30():
    values = material_metric_values({
        "spend": "1", "impressions": "2", "clicks": "1", "onsite_total_ad_impression": "1",
        "onsite_ad_impression_ad_revenue_day0": "0.5", "onsite_ad_impression_ad_revenue_day6": "0.6",
        "onsite_ad_impression_ad_revenue_day13": "0.7", "onsite_total_ad_impression_value": "0.8",
    })
    assert values["revenue_d3"] is None and values["revenue_d30"] is None
    assert values["revenue_d7"] == Decimal("0.6")


def test_day_and_hour_parsing():
    assert parse_local_day("2026-10-05 00:00:00") == date(2026, 10, 5)
    assert parse_local_day("2026-10-05") == date(2026, 10, 5)
    # 香港 10:00 = UTC 02:00
    assert parse_local_hour("2026-10-05 10:00:00", "Asia/Hong_Kong") == datetime(2026, 10, 5, 2, tzinfo=UTC)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/modules/reporting/test_metrics.py`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# backend/app/modules/reporting/metrics.py
"""平台报表字段 → 存储列。

基础报表 dN = 点击后 N+1 天累计，因此 D3/D7/D14/D30 取 d2/d6/d13/d29；
素材报表字段名不同且没有 D3、D30，恒为 NULL，页面显示“平台未提供”。
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.core.errors import DomainError

_BASIC = {
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "ad_impression_events": "native_growth_total_ad_impression_event_count",
    "revenue_d0": "native_growth_ad_revenue_value_d0",
    "revenue_d3": "native_growth_ad_revenue_value_d2",
    "revenue_d7": "native_growth_ad_revenue_value_d6",
    "revenue_d14": "native_growth_ad_revenue_value_d13",
    "revenue_d30": "native_growth_ad_revenue_value_d29",
    "revenue_total": "native_growth_total_ad_impression_value",
}
_MATERIAL = {
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "ad_impression_events": "onsite_total_ad_impression",
    "revenue_d0": "onsite_ad_impression_ad_revenue_day0",
    "revenue_d7": "onsite_ad_impression_ad_revenue_day6",
    "revenue_d14": "onsite_ad_impression_ad_revenue_day13",
    "revenue_total": "onsite_total_ad_impression_value",
}
COLUMNS = tuple(_BASIC)

# 广告报表随行带回上级 ID、名称和类型，新建对象无需等待目录同步即可归组。
AD_DAY_METRICS = (*_BASIC.values(), "campaign_id", "adgroup_id", "campaign_name",
                  "adgroup_name", "ad_name", "campaign_automation_type")
CAMPAIGN_METRICS = (*_BASIC.values(), "campaign_name")
ACCOUNT_METRICS = ("spend", "native_growth_total_ad_impression_value")
MATERIAL_METRICS = (*_MATERIAL.values(), "main_material_name")


def parse_decimal(value: object) -> Decimal | None:
    if value is None or value == "-":
        return None
    if type(value) not in (str, int):
        raise DomainError("report_value_invalid", "报表指标必须是定点数字文本")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("report_value_invalid", "报表指标不是有效定点数") from exc
    if not amount.is_finite():
        raise DomainError("report_value_invalid", "报表指标不能是无穷值")
    return amount


def basic_metric_values(metrics: dict) -> dict[str, Decimal | None]:
    return {column: parse_decimal(metrics.get(field)) for column, field in _BASIC.items()}


def material_metric_values(metrics: dict) -> dict[str, Decimal | None]:
    return {column: parse_decimal(metrics.get(_MATERIAL[column])) if column in _MATERIAL else None
            for column in COLUMNS}


def parse_local_day(raw: str) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise DomainError("report_row_invalid", "报表日期维度无效") from exc


def parse_local_hour(raw: str, timezone: str) -> datetime:
    try:
        local = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        return local.replace(tzinfo=ZoneInfo(timezone)).astimezone(UTC)
    except (ValueError, KeyError) as exc:
        raise DomainError("report_row_invalid", "报表小时维度无效") from exc
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/modules/reporting/test_metrics.py`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/metrics.py backend/tests/modules/reporting/test_metrics.py
git commit -m "reporting: 统一报表平台字段到存储列的映射与解析"
```

---

### Task 3: 平台限流业务码 40100 进入延后

**Files:**
- Modify: `backend/app/integrations/tiktok/official/accounts.py`（`_strict_sdk_envelope.deserialize`）
- Test: `backend/tests/integrations/tiktok/test_official_read_throttle.py`（追加）

**Interfaces:**
- Produces: 任何官方只读请求收到 `{"code": 40100}` 时抛 `AccountAdmissionDeferred(retry_after_ms=300_000)`；文案含 QPD 时（`message` 含 `"QPD"` 或 `"per day"`，不区分大小写）延后到下一个 UTC 0 点。

- [ ] **Step 1: 写失败测试**（先读该测试文件现有的替身客户端写法，沿用其构造方式）

```python
def test_business_code_40100_defers_instead_of_failing(fake_official_client):
    from datetime import UTC, datetime, timedelta

    from app.integrations.tiktok.official.accounts import OfficialReadRequests
    from app.integrations.tiktok.sdk import AccountAdmissionDeferred

    client = fake_official_client(body='{"code": 40100, "message": "Too many requests (QPM)", "request_id": "r", "data": {}}')
    requests = OfficialReadRequests(client, request_scope=_no_scope, deadline=datetime.now(UTC) + timedelta(seconds=30))
    import pytest
    with pytest.raises(AccountAdmissionDeferred) as caught:
        requests.invoke("reports.integrated", "adv", lambda timeout: client.send(timeout), exact_numbers=True)
    assert caught.value.retry_after_ms == 300_000
```

若现有文件没有 `fake_official_client`/`_no_scope`，在该文件内新增：`_no_scope` 为返回 `contextlib.nullcontext()` 的函数；`fake_official_client(body)` 构造一个对象，其 `deserialize(response, response_type)` 由真实 `ApiClient.deserialize` 提供，`send(timeout)` 调用 `self.deserialize(SimpleNamespace(data=body), "object")`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/integrations/tiktok/test_official_read_throttle.py -k 40100`
Expected: FAIL（抛 `DomainError tiktok_response_error`）

- [ ] **Step 3: 实现**——在 `deserialize` 内、`raise DomainError("tiktok_response_error", ...)` 判断之后、`if exact is not None` 之前插入：

```python
        if raw["code"] == 40100:
            # 平台应用级限流：请求未被执行。QPM 需等 5 分钟，QPD 需等到次日
            # UTC 0 点；统一走额度延后，不能计为读取失败。
            message = str(raw.get("message") or "").lower()
            if "qpd" in message or "per day" in message:
                now = datetime.now(UTC)
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
                raise AccountAdmissionDeferred(int((tomorrow - now).total_seconds() * 1000))
            raise AccountAdmissionDeferred(300_000)
```

并在文件顶部 `from datetime import UTC, datetime` 改为 `from datetime import UTC, datetime, timedelta`。

- [ ] **Step 4: 运行测试**

Run: `pytest tests/integrations/tiktok/test_official_read_throttle.py`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add backend/app/integrations/tiktok/official/accounts.py backend/tests/integrations/tiktok/test_official_read_throttle.py
git commit -m "tiktok: 平台限流业务码 40100 进入延后重试而非判失败"
```

---

### Task 4: 官方 SDK 报表读取器与网关接入

**Files:**
- Create: `backend/app/integrations/tiktok/contracts/report_reader.py`
- Create: `backend/app/integrations/tiktok/adapters/sdk_report_reader.py`
- Modify: `backend/app/integrations/tiktok/gateway.py`（`TikTokGateway` 增加 `report_reader`；`_OPERATION_CAPABILITIES` 增加 `"reports.bc": "read"`，并把 `reports.bc` 加入无账户操作集合）
- Test: `backend/tests/integrations/tiktok/test_sdk_report_reader.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class RawReportPage:
    rows: tuple[dict, ...]          # 平台原始 {"dimensions": {...}, "metrics": {...}}
    page: int
    total_page: int
    total_number: int

class ReportReader(Protocol):
    def bc_spend(self, *, bc_id: str, start: date, end: date, page: int) -> RawReportPage: ...
    def ad_day(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int,
               campaign_ids: tuple[str, ...] = ()) -> RawReportPage: ...
    def campaign_hour(self, *, advertiser_ids: tuple[str, ...], day: date, page: int) -> RawReportPage: ...
    def campaign_day_utc(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int) -> RawReportPage: ...
    def account_day(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int) -> RawReportPage: ...
    def material_day(self, *, advertiser_id: str, start: date, end: date, page: int) -> RawReportPage: ...
```

  `TikTokGateway.report_reader: ReportReader | None`（MCP 通道为 `None`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integrations/tiktok/test_sdk_report_reader.py
import json
from datetime import date

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_report_reader import SdkReportReader


class _Recorder:
    def __init__(self, body):
        self.body, self.calls = body, []

    def __call__(self, operation, advertiser_id, arguments):
        self.calls.append((operation, advertiser_id, arguments))
        return self.body


def _reader(body):
    recorder = _Recorder(body)
    return SdkReportReader(send=recorder), recorder


PAGE = {"list": [], "page_info": {"page": 1, "page_size": 1000, "total_page": 1, "total_number": 0}}


def test_ad_day_payload_always_includes_status_all():
    reader, rec = _reader(PAGE)
    reader.ad_day(advertiser_ids=("a", "b"), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1)
    op, adv, args = rec.calls[0]
    assert op == "reports.integrated" and adv == "a"
    assert args["advertiser_ids"] == ["a", "b"] and args["data_level"] == "AUCTION_AD"
    assert args["dimensions"] == ["ad_id_v2", "stat_time_day"] and args["page_size"] == 1000
    assert {"field_name": "ad_status", "filter_type": "IN", "filter_value": json.dumps(["STATUS_ALL"])} in args["filtering"]
    assert "campaign_automation_type" in args["metrics"]


def test_ad_day_campaign_filter_limit_30():
    reader, rec = _reader(PAGE)
    reader.ad_day(advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1,
                  campaign_ids=tuple(str(i) for i in range(30)))
    assert any(f["field_name"] == "campaign_ids" for f in rec.calls[0][2]["filtering"])
    with pytest.raises(DomainError):
        reader.ad_day(advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1,
                      campaign_ids=tuple(str(i) for i in range(31)))


def test_rejects_more_than_five_accounts_and_long_ranges():
    reader, _ = _reader(PAGE)
    with pytest.raises(DomainError):
        reader.ad_day(advertiser_ids=tuple("abcdef"), start=date(2026, 10, 1), end=date(2026, 10, 1), page=1)
    with pytest.raises(DomainError):
        reader.ad_day(advertiser_ids=("a",), start=date(2026, 9, 1), end=date(2026, 10, 5), page=1)


def test_bc_spend_has_no_advertiser_scope():
    reader, rec = _reader(PAGE)
    reader.bc_spend(bc_id="bc1", start=date(2026, 9, 4), end=date(2026, 10, 5), page=1)
    op, adv, args = rec.calls[0]
    assert op == "reports.bc" and adv is None and args["report_type"] == "BC" and args["bc_id"] == "bc1"


def test_page_info_validation_and_empty_object():
    reader, _ = _reader({})
    page = reader.account_day(advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=1)
    assert page.rows == () and page.total_number == 0
    reader, _ = _reader({"list": [{}], "page_info": {"page": 2, "page_size": 1000, "total_page": 1, "total_number": 1}})
    with pytest.raises(DomainError):
        reader.account_day(advertiser_ids=("a",), start=date(2026, 10, 5), end=date(2026, 10, 5), page=2)


def test_material_day_single_account():
    reader, rec = _reader(PAGE)
    reader.material_day(advertiser_id="a", start=date(2026, 10, 5), end=date(2026, 10, 5), page=1)
    op, adv, args = rec.calls[0]
    assert op == "reports.material_breakdown" and args["dimensions"] == ["main_material_id", "stat_time_day"]
    assert args["page_size"] == 100
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/integrations/tiktok/test_sdk_report_reader.py`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写合同**

```python
# backend/app/integrations/tiktok/contracts/report_reader.py
"""报表读取合同：只走官方 SDK；返回平台原始行，解析与发布由报表模块负责。"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.core.errors import DomainError


@dataclass(frozen=True)
class RawReportPage:
    rows: tuple[dict, ...]
    page: int
    total_page: int
    total_number: int

    @property
    def next_page(self) -> int | None:
        return self.page + 1 if self.page < self.total_page else None


def page_from_data(data: dict, *, requested_page: int) -> RawReportPage:
    """校验 page_info；合法零行时平台可能返回空对象，视为完整空结果。"""
    if data == {}:
        return RawReportPage((), requested_page, 0, 0)
    rows, info = data.get("list"), data.get("page_info")
    if type(rows) is not list or type(info) is not dict or any(type(r) is not dict for r in rows):
        raise DomainError("report_response_invalid", "报表回执无效")
    page, size, total_page, total = (info.get(k) for k in ("page", "page_size", "total_page", "total_number"))
    if any(type(v) is not int for v in (page, size, total_page, total)):
        raise DomainError("report_response_invalid", "报表分页信息无效")
    expected_pages = 0 if total == 0 else (total + size - 1) // size
    expected_rows = 0 if total == 0 else min(size, max(0, total - (page - 1) * size))
    if page != requested_page or total_page != expected_pages and not (total == 0 and total_page in (0, 1)) \
            or len(rows) != expected_rows:
        raise DomainError("report_response_invalid", "报表分页与声明不一致")
    return RawReportPage(tuple(rows), page, total_page, total)


class ReportReader(Protocol):
    def bc_spend(self, *, bc_id: str, start: date, end: date, page: int) -> RawReportPage: ...
    def ad_day(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int,
               campaign_ids: tuple[str, ...] = ()) -> RawReportPage: ...
    def campaign_hour(self, *, advertiser_ids: tuple[str, ...], day: date, page: int) -> RawReportPage: ...
    def campaign_day_utc(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int) -> RawReportPage: ...
    def account_day(self, *, advertiser_ids: tuple[str, ...], start: date, end: date, page: int) -> RawReportPage: ...
    def material_day(self, *, advertiser_id: str, start: date, end: date, page: int) -> RawReportPage: ...
```

- [ ] **Step 4: 写读取器**

```python
# backend/app/integrations/tiktok/adapters/sdk_report_reader.py
"""官方 SDK 报表请求。每个物理请求都经网关传入的 send（冻结路由 + 额度准入）。"""

import json
from collections.abc import Callable
from datetime import date
from typing import Any

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage, page_from_data
from app.modules.reporting.metrics import (
    ACCOUNT_METRICS, AD_DAY_METRICS, CAMPAIGN_METRICS, MATERIAL_METRICS,
)

Send = Callable[[str, str | None, dict[str, Any]], dict]
MAX_ACCOUNTS = 5
MAX_CAMPAIGN_FILTER = 30  # 官方防超时建议：广告级按系列过滤不超过 30 个
_STATUS_ALL = {"field_name": "ad_status", "filter_type": "IN", "filter_value": json.dumps(["STATUS_ALL"])}


def _check_range(start: date, end: date, *, max_days: int) -> None:
    if start > end or (end - start).days + 1 > max_days:
        raise DomainError("report_query_invalid", "报表日期段超出平台上限")


def _check_accounts(ids: tuple[str, ...]) -> None:
    if not 1 <= len(ids) <= MAX_ACCOUNTS or len(set(ids)) != len(ids):
        raise DomainError("report_query_invalid", "一次报表请求须带 1–5 个不同账户")


class SdkReportReader:
    def __init__(self, *, send: Send) -> None:
        self._send = send

    def _basic(self, *, advertiser_ids, data_level, dimensions, metrics, start, end, page,
               filtering=None, utc=False) -> RawReportPage:
        _check_accounts(advertiser_ids)
        arguments: dict[str, Any] = {
            "report_type": "BASIC", "service_type": "AUCTION",
            "advertiser_ids": list(advertiser_ids), "data_level": data_level,
            "dimensions": list(dimensions), "metrics": list(metrics),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "page": page, "page_size": 1000,
        }
        if filtering:
            arguments["filtering"] = filtering
        if utc:
            arguments["multi_adv_report_in_utc_time"] = True
        data = self._send("reports.integrated", advertiser_ids[0], arguments)
        return page_from_data(data, requested_page=page)

    def bc_spend(self, *, bc_id, start, end, page):
        _check_range(start, end, max_days=365)
        data = self._send("reports.bc", None, {
            "report_type": "BC", "bc_id": bc_id, "dimensions": ["advertiser_id"],
            "metrics": ["spend"], "start_date": start.isoformat(), "end_date": end.isoformat(),
            "page": page, "page_size": 1000,
        })
        return page_from_data(data, requested_page=page)

    def ad_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
        _check_range(start, end, max_days=30)
        filtering = [_STATUS_ALL]
        if campaign_ids:
            if len(campaign_ids) > MAX_CAMPAIGN_FILTER or len(advertiser_ids) != 1:
                raise DomainError("report_query_invalid", "按系列过滤只支持单账户且最多 30 个系列")
            filtering.append({"field_name": "campaign_ids", "filter_type": "IN",
                              "filter_value": json.dumps(list(campaign_ids))})
        return self._basic(advertiser_ids=advertiser_ids, data_level="AUCTION_AD",
                           dimensions=("ad_id_v2", "stat_time_day"), metrics=AD_DAY_METRICS,
                           start=start, end=end, page=page, filtering=filtering)

    def campaign_hour(self, *, advertiser_ids, day, page):
        return self._basic(advertiser_ids=advertiser_ids, data_level="AUCTION_CAMPAIGN",
                           dimensions=("campaign_id", "stat_time_hour"), metrics=CAMPAIGN_METRICS,
                           start=day, end=day, page=page,
                           filtering=[{**_STATUS_ALL, "field_name": "campaign_status"}])

    def campaign_day_utc(self, *, advertiser_ids, start, end, page):
        _check_range(start, end, max_days=30)
        return self._basic(advertiser_ids=advertiser_ids, data_level="AUCTION_CAMPAIGN",
                           dimensions=("campaign_id", "stat_time_day"), metrics=CAMPAIGN_METRICS,
                           start=start, end=end, page=page, utc=True,
                           filtering=[{**_STATUS_ALL, "field_name": "campaign_status"}])

    def account_day(self, *, advertiser_ids, start, end, page):
        _check_range(start, end, max_days=30)
        return self._basic(advertiser_ids=advertiser_ids, data_level="AUCTION_ADVERTISER",
                           dimensions=("advertiser_id", "stat_time_day"), metrics=ACCOUNT_METRICS,
                           start=start, end=end, page=page)

    def material_day(self, *, advertiser_id, start, end, page):
        _check_range(start, end, max_days=30)
        data = self._send("reports.material_breakdown", advertiser_id, {
            "advertiser_id": advertiser_id, "dimensions": ["main_material_id", "stat_time_day"],
            "metrics": list(MATERIAL_METRICS), "start_date": start.isoformat(),
            "end_date": end.isoformat(), "page": page, "page_size": 100,
        })
        return page_from_data(data, requested_page=page)
```

- [ ] **Step 5: 网关接入**——在 `gateway.py`：
  1. `_OPERATION_CAPABILITIES` 加 `"reports.bc": "read"`；`_capability` 中允许 `advertiser_id is None` 的集合加入 `"reports.bc"`（与 `FINANCE_OPERATION` 同处理）。
  2. `TikTokGateway` 增加字段 `report_reader: ReportReader | None = None`。
  3. SDK 分支构造 `SdkReportReader(send=_sdk_report_send(official, request_scope, task_deadline))`，其中：

```python
def _sdk_report_send(client, request_scope, deadline):
    """把报表请求映射到 SDK 生成方法；每个物理请求单独准入并保留精确十进制。"""
    requests = OfficialReadRequests(client, request_scope=request_scope, deadline=deadline)
    api = sdk.ReportingApi(client)
    token = client.default_headers["Access-Token"]

    def send(operation, advertiser_id, arguments):
        args = dict(arguments)
        if operation in {"reports.integrated", "reports.bc"}:
            call = lambda timeout: api.report_integrated_get(
                args.pop("report_type"), token, **{**args, "_request_timeout": timeout})
        elif operation == "reports.material_breakdown":
            call = lambda timeout: api.smart_plus_material_report_breakdown(
                args.pop("advertiser_id"), args.pop("dimensions"), args.pop("start_date"),
                args.pop("end_date"), token, **{**args, "_request_timeout": timeout})
        else:
            raise DomainError("report_operation_invalid", "报表操作未登记")
        return requests.invoke(operation, advertiser_id, call, exact_numbers=True).data

    return send
```

  `sdk` 为 `business_api_client`；`advertiser_ids`、`bc_id`、`multi_adv_report_in_utc_time` 若生成 SDK 方法签名不接受，改用 `api.api_client.call_api("/open_api/v1.3/report/integrated/get/", "GET", ...)`，参考 `sdk_reporting.py` 中 `_download_metadata` 的写法，把 list/dict 参数 `json.dumps` 后放入 query。先在测试替身下验证再联调。
  4. MCP 分支 `report_reader=None`。

- [ ] **Step 6: 运行测试**

Run: `pytest tests/integrations/tiktok/test_sdk_report_reader.py tests/integrations/tiktok`
Expected: 新测试通过，原网关测试仍通过

- [ ] **Step 7: 提交**

```bash
git add backend/app/integrations/tiktok/contracts/report_reader.py \
  backend/app/integrations/tiktok/adapters/sdk_report_reader.py backend/app/integrations/tiktok/gateway.py \
  backend/tests/integrations/tiktok/test_sdk_report_reader.py
git commit -m "tiktok: 新增只走官方 SDK 的报表读取器（BC 筛选、广告日、系列小时、素材日）"
```

---

### Task 5: 整份替换发布、系列汇总与同步状态

**Files:**
- Create: `backend/app/modules/reporting/publish.py`
- Create: `backend/tests/modules/reporting/sync_conftest.py`；Modify: `backend/tests/modules/reporting/conftest.py` 末尾加 `from tests.modules.reporting.sync_conftest import *  # noqa: F401,F403`
- Test: `backend/tests/modules/reporting/test_publish.py`

**Interfaces:**
- Consumes: Task 1 模型、Task 2 解析函数。
- Produces:

```python
def publish_ad_day(session, *, tenant_id: UUID, job_id: UUID, accounts: dict[str, AccountMeta],
                   start: date, end: date, rows: Sequence[dict],
                   campaign_ids: tuple[str, ...] = ()) -> int
def publish_campaign_hour(session, *, tenant_id, job_id, accounts, day: date, rows) -> int
def publish_campaign_day_utc(session, *, tenant_id, job_id, accounts, start, end, rows) -> int
def publish_material_day(session, *, tenant_id, job_id, accounts, start, end, rows) -> int
def publish_account_reconciliation(session, *, tenant_id, accounts, start, end, rows) -> None
def mark_failed(session, *, tenant_id, advertiser_ids, kind, start, end, error_code) -> None
@dataclass(frozen=True)
class AccountMeta: currency: str; timezone: str
```

  返回值为写入行数。`campaign_ids` 非空表示按系列分批的部分替换：只删除这些系列的广告行，并在同一事务重算整个账户日期段的系列汇总。

- [ ] **Step 1: 写测试种子**

```python
# backend/tests/modules/reporting/sync_conftest.py
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import User
from app.modules.accounts.connection_models import BCConnectionBinding, ConnectionAuthorization
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess, TenantBC, TikTokConnection
from app.modules.tenants.models import Tenant, TenantMembership


@pytest.fixture
def sync_seed(session):
    """真实 PostgreSQL：一个租户、一个 API 连接、两个 BC（bc-a、bc-b），
    账户 acc-1（香港时区）只在 bc-a，acc-2（UTC）同时在 bc-a 与 bc-b。"""
    actor, tenant = uuid4(), uuid4()
    session.add(User(id=actor, username=f"sync-{actor.hex[:10]}", hashed_password="x", is_active=True))
    session.add(Tenant(id=tenant, name=f"t-{tenant.hex[:8]}"))
    session.flush()
    session.add(TenantMembership(tenant_id=tenant, user_id=actor, role="operator", active=True))
    connection = TikTokConnection(tenant_id=tenant, status="ACTIVE", kind="OFFICIAL_API",
                                  authorization_revision=0, adapter_contract_revision="official-api-v1")
    session.add_all([TenantBC(tenant_id=tenant, bc_id="bc-a"), TenantBC(tenant_id=tenant, bc_id="bc-b"), connection])
    session.flush()
    session.add_all([
        AdvertiserAccount(tenant_id=tenant, advertiser_id="acc-1", currency="USD", timezone="Asia/Hong_Kong", name="账户一"),
        AdvertiserAccount(tenant_id=tenant, advertiser_id="acc-2", currency="USD", timezone="UTC", name="账户二"),
        ConnectionAuthorization(tenant_id=tenant, connection_id=connection.id, authorization_revision=0,
                                source="OFFLINE_TEST", verified_at=datetime.now(UTC),
                                permission_summary={"read_authorized": True}),
    ])
    session.flush()
    for bc in ("bc-a", "bc-b"):
        session.add(BCConnectionBinding(tenant_id=tenant, bc_id=bc, connection_id=connection.id,
                                        kind="OFFICIAL_API", status="ACTIVE", authorization_revision=0, revision=0))
    for bc, acc in (("bc-a", "acc-1"), ("bc-a", "acc-2"), ("bc-b", "acc-2")):
        session.add(BCAccountAccess(tenant_id=tenant, bc_id=bc, advertiser_id=acc, connection_id=connection.id,
                                    in_bc=True, authorized=True, active=True, checked_at=datetime.now(UTC)))
    session.flush()
    return SimpleNamespace(tenant_id=tenant, actor_id=actor, connection=connection,
                           context=SimpleNamespace(tenant_id=tenant, actor_id=actor, role="operator"))


def ad_row(ad, day, *, campaign="c1", adgroup="g1", spend="1.00", d0="0.50", total="0.50", adv="acc-1"):
    return {
        "dimensions": {"ad_id_v2": ad, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {"advertiser_id": adv, "campaign_id": campaign, "adgroup_id": adgroup,
                    "spend": spend, "impressions": "10", "clicks": "1",
                    "native_growth_total_ad_impression_event_count": "1",
                    "native_growth_ad_revenue_value_d0": d0, "native_growth_ad_revenue_value_d2": d0,
                    "native_growth_ad_revenue_value_d6": d0, "native_growth_ad_revenue_value_d13": d0,
                    "native_growth_ad_revenue_value_d29": d0,
                    "native_growth_total_ad_impression_value": total,
                    "campaign_name": f"嘉书-剧{campaign}-备注", "adgroup_name": "组", "ad_name": "广告",
                    "campaign_automation_type": "UPGRADED_SMART_PLUS"},
    }
```

单账户请求时平台不返回 `metrics.advertiser_id`；发布函数在 `accounts` 只有一个键时用该键补齐，多于一个且缺失时报 `report_row_invalid`。

- [ ] **Step 2: 写失败测试**

```python
# backend/tests/modules/reporting/test_publish.py
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlmodel import select

from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import AdReportDay, CampaignReportDay, ReportSyncState
from tests.modules.reporting.sync_conftest import ad_row

HK = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong")}
D = date(2026, 10, 5)


def test_publish_replaces_range_and_rolls_up_campaigns(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D,
                   rows=[ad_row("a1", D), ad_row("a2", D, spend="2.00")])
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D,
                   rows=[ad_row("a1", D, spend="5.00")])
    ads = session.exec(select(AdReportDay).where(AdReportDay.tenant_id == t)).all()
    assert [(r.ad_id, r.spend) for r in ads] == [("a1", Decimal("5.00"))]
    camp = session.exec(select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)).one()
    assert camp.spend == Decimal("5.00") and camp.campaign_name == "嘉书-剧c1-备注"
    state = session.get(ReportSyncState, (t, "acc-1", "ad_day", D))
    assert state.status == "COMPLETE" and state.row_count == 1


def test_empty_result_deletes_and_marks_complete(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D, rows=[ad_row("a1", D)])
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D, rows=[])
    assert session.exec(select(AdReportDay).where(AdReportDay.tenant_id == t)).all() == []
    assert session.get(ReportSyncState, (t, "acc-1", "ad_day", D)).status == "COMPLETE"


def test_replace_only_touches_requested_accounts_and_dates(session, sync_seed):
    t = sync_seed.tenant_id
    both = {"acc-1": AccountMeta("USD", "Asia/Hong_Kong"), "acc-2": AccountMeta("USD", "UTC")}
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=both, start=D, end=D,
                   rows=[ad_row("a1", D), ad_row("b1", D, adv="acc-2")])
    other_day = date(2026, 10, 4)
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=other_day, end=other_day,
                   rows=[ad_row("a9", other_day)])
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D, rows=[])
    left = {(r.advertiser_id, r.stat_date, r.ad_id) for r in session.exec(
        select(AdReportDay).where(AdReportDay.tenant_id == t)).all()}
    assert left == {("acc-2", D, "b1"), ("acc-1", other_day, "a9")}


def test_null_member_makes_rollup_null(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D,
                   rows=[ad_row("a1", D, total="-"), ad_row("a2", D)])
    camp = session.exec(select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)).one()
    assert camp.revenue_total is None and camp.spend == Decimal("2.00")


def test_row_outside_requested_range_is_rejected(session, sync_seed):
    import pytest

    from app.core.errors import DomainError

    with pytest.raises(DomainError):
        publish_ad_day(session, tenant_id=sync_seed.tenant_id, job_id=uuid4(), accounts=HK,
                       start=D, end=D, rows=[ad_row("a1", date(2026, 10, 6))])


def test_partial_campaign_replace_keeps_other_campaigns(session, sync_seed):
    t = sync_seed.tenant_id
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D,
                   rows=[ad_row("a1", D, campaign="c1"), ad_row("a2", D, campaign="c2")])
    publish_ad_day(session, tenant_id=t, job_id=uuid4(), accounts=HK, start=D, end=D,
                   rows=[ad_row("a1", D, campaign="c1", spend="9.00")], campaign_ids=("c1",))
    camps = {r.campaign_id: r.spend for r in session.exec(
        select(CampaignReportDay).where(CampaignReportDay.tenant_id == t)).all()}
    assert camps == {"c1": Decimal("9.00"), "c2": Decimal("1.00")}
```

- [ ] **Step 3: 运行确认失败**

Run: `pytest tests/modules/reporting/test_publish.py`
Expected: FAIL（模块不存在）

- [ ] **Step 4: 实现**

```python
# backend/app/modules/reporting/publish.py
"""整份替换发布：某个“账户 × 报表 × 日期段”全部分页成功后，在一个事务内
先删旧行再写新行。平台未返回的行就是当天没有数据，删除即正确结果。

汇总规则：任一成员指标为 NULL（平台 '-'）时合计为 NULL，绝不显示部分合计。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, text
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.reporting.metrics import (
    basic_metric_values, material_metric_values, parse_decimal, parse_local_day, parse_local_hour,
)
from app.modules.reporting.store_models import (
    METRIC_COLUMNS, AccountReconciliationDay, AdReportDay, CampaignReportDay, CampaignReportDayUtc,
    CampaignReportHour, MaterialReportDay, ReportSyncState,
)


@dataclass(frozen=True)
class AccountMeta:
    currency: str
    timezone: str


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _account(row: dict, accounts: dict[str, AccountMeta]) -> str:
    advertiser = (row.get("metrics") or {}).get("advertiser_id") or (row.get("dimensions") or {}).get("advertiser_id")
    if advertiser is None and len(accounts) == 1:
        advertiser = next(iter(accounts))
    if advertiser not in accounts:
        raise DomainError("report_row_invalid", "报表行账户不在本次请求范围内")
    return str(advertiser)


def _in_range(day: date, start: date, end: date) -> date:
    if not start <= day <= end:
        raise DomainError("report_row_invalid", "报表行日期超出请求范围")
    return day


def _set_state(session, *, tenant_id, advertiser_ids, kind, days, counts: dict[tuple[str, date], int]):
    now = datetime.now(UTC)
    for advertiser in advertiser_ids:
        for day in days:
            state = session.get(ReportSyncState, (tenant_id, advertiser, kind, day))
            if state is None:
                state = ReportSyncState(tenant_id=tenant_id, advertiser_id=advertiser, report_kind=kind,
                                        stat_date=day, status="COMPLETE")
                session.add(state)
            state.status, state.error_code = "COMPLETE", None
            state.row_count = counts.get((advertiser, day), 0)
            state.last_success_at = state.updated_at = now


def mark_failed(session: Session, *, tenant_id: UUID, advertiser_ids: Sequence[str], kind: str,
                start: date, end: date, error_code: str) -> None:
    """失败不删除旧数据：已有完整数据标为 STALE，从未成功的标为 FAILED。"""
    now = datetime.now(UTC)
    for advertiser in advertiser_ids:
        for day in _days(start, end):
            state = session.get(ReportSyncState, (tenant_id, advertiser, kind, day))
            if state is None:
                session.add(ReportSyncState(tenant_id=tenant_id, advertiser_id=advertiser, report_kind=kind,
                                            stat_date=day, status="FAILED", error_code=error_code, updated_at=now))
            else:
                state.status = "STALE" if state.last_success_at else "FAILED"
                state.error_code, state.updated_at = error_code, now
    session.flush()


def _rollup_sql() -> str:
    # COUNT(col)=COUNT(*) 才求和：任一成员为 NULL 则系列合计为 NULL。
    cols = ", ".join(
        f"CASE WHEN COUNT({c}) = COUNT(*) THEN SUM({c}) END AS {c}" for c in METRIC_COLUMNS)
    return f"""
        INSERT INTO campaign_report_day (tenant_id, advertiser_id, stat_date, campaign_id, campaign_name,
            {", ".join(METRIC_COLUMNS)}, currency, timezone, fetched_at, sync_job_id)
        SELECT tenant_id, advertiser_id, stat_date, campaign_id, MAX(campaign_name), {cols},
               MAX(currency), MAX(timezone), MAX(fetched_at), :job_id
        FROM ad_report_day
        WHERE tenant_id = :tenant_id AND advertiser_id = ANY(:advertisers)
          AND stat_date BETWEEN :start AND :end
        GROUP BY tenant_id, advertiser_id, stat_date, campaign_id
    """


def publish_ad_day(session: Session, *, tenant_id: UUID, job_id: UUID, accounts: dict[str, AccountMeta],
                   start: date, end: date, rows: Sequence[dict], campaign_ids: tuple[str, ...] = ()) -> int:
    now, records, counts = datetime.now(UTC), [], {}
    for row in rows:
        advertiser = _account(row, accounts)
        dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        ad_id, campaign, adgroup = dims.get("ad_id_v2"), metrics.get("campaign_id"), metrics.get("adgroup_id")
        if not ad_id or not campaign or not adgroup:
            raise DomainError("report_row_invalid", "广告行缺少广告、系列或广告组 ID")
        if campaign_ids and campaign not in campaign_ids:
            raise DomainError("report_row_invalid", "按系列分批结果包含范围外系列")
        meta = accounts[advertiser]
        records.append({
            "tenant_id": tenant_id, "advertiser_id": advertiser, "stat_date": day, "ad_id": str(ad_id),
            "campaign_id": str(campaign), "adgroup_id": str(adgroup),
            "campaign_automation_type": metrics.get("campaign_automation_type"),
            "ad_name": metrics.get("ad_name"), "campaign_name": metrics.get("campaign_name"),
            "adgroup_name": metrics.get("adgroup_name"), "currency": meta.currency, "timezone": meta.timezone,
            "fetched_at": now, "sync_job_id": job_id, **basic_metric_values(metrics),
        })
        counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1
    advertisers = sorted(accounts)
    statement = delete(AdReportDay).where(
        AdReportDay.tenant_id == tenant_id, AdReportDay.advertiser_id.in_(advertisers),
        AdReportDay.stat_date.between(start, end))
    if campaign_ids:
        statement = statement.where(AdReportDay.campaign_id.in_(campaign_ids))
    session.execute(statement)
    if records:
        session.execute(insert(AdReportDay), records)
    session.execute(delete(CampaignReportDay).where(
        CampaignReportDay.tenant_id == tenant_id, CampaignReportDay.advertiser_id.in_(advertisers),
        CampaignReportDay.stat_date.between(start, end)))
    session.execute(text(_rollup_sql()), {"tenant_id": tenant_id, "advertisers": advertisers,
                                          "start": start, "end": end, "job_id": job_id})
    if not campaign_ids:
        _set_state(session, tenant_id=tenant_id, advertiser_ids=advertisers, kind="ad_day",
                   days=_days(start, end), counts=counts)
    session.flush()
    return len(records)


def _publish_simple(session, *, model, kind, key_builder, tenant_id, job_id, accounts, start, end,
                    rows, value_parser, time_filter) -> int:
    now, records, counts = datetime.now(UTC), [], {}
    for row in rows:
        advertiser = _account(row, accounts)
        meta = accounts[advertiser]
        keys, day = key_builder(row, meta)
        records.append({"tenant_id": tenant_id, "advertiser_id": advertiser, "currency": meta.currency,
                        "timezone": meta.timezone, "fetched_at": now, "sync_job_id": job_id,
                        **keys, **value_parser(row.get("metrics") or {})})
        counts[(advertiser, day)] = counts.get((advertiser, day), 0) + 1
    advertisers = sorted(accounts)
    session.execute(delete(model).where(model.tenant_id == tenant_id, model.advertiser_id.in_(advertisers),
                                        time_filter(model)))
    if records:
        session.execute(insert(model), records)
    _set_state(session, tenant_id=tenant_id, advertiser_ids=advertisers, kind=kind,
               days=_days(start, end), counts=counts)
    session.flush()
    return len(records)


def publish_campaign_hour(session, *, tenant_id, job_id, accounts, day, rows) -> int:
    def keys(row, meta):
        dims = row.get("dimensions") or {}
        hour = parse_local_hour(dims.get("stat_time_hour"), meta.timezone)
        return {"stat_hour": hour, "campaign_id": str(dims.get("campaign_id"))}, day

    def values(metrics):
        parsed = basic_metric_values(metrics)
        return {c: parsed[c] for c in METRIC_COLUMNS}

    # 各账户“本地这一天”的 UTC 边界不同；按每个账户的时区分别删除。
    def window(model):
        clauses = []
        for advertiser, meta in accounts.items():
            start_hour = parse_local_hour(f"{day} 00:00:00", meta.timezone)
            end_hour = parse_local_hour(f"{day + timedelta(days=1)} 00:00:00", meta.timezone)
            clauses.append((model.advertiser_id == advertiser) & (model.stat_hour >= start_hour)
                           & (model.stat_hour < end_hour))
        from sqlalchemy import or_
        return or_(*clauses)

    return _publish_simple(session, model=CampaignReportHour, kind="campaign_hour", key_builder=keys,
                           tenant_id=tenant_id, job_id=job_id, accounts=accounts, start=day, end=day,
                           rows=rows, value_parser=values, time_filter=window)


def publish_campaign_day_utc(session, *, tenant_id, job_id, accounts, start, end, rows) -> int:
    def keys(row, meta):
        dims = row.get("dimensions") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        return {"stat_date": day, "campaign_id": str(dims.get("campaign_id"))}, day

    return _publish_simple(session, model=CampaignReportDayUtc, kind="campaign_day_utc", key_builder=keys,
                           tenant_id=tenant_id, job_id=job_id, accounts=accounts, start=start, end=end,
                           rows=rows, value_parser=basic_metric_values,
                           time_filter=lambda m: m.stat_date.between(start, end))


def publish_material_day(session, *, tenant_id, job_id, accounts, start, end, rows) -> int:
    def keys(row, meta):
        dims, metrics = row.get("dimensions") or {}, row.get("metrics") or {}
        day = _in_range(parse_local_day(dims.get("stat_time_day")), start, end)
        if not dims.get("main_material_id") or not dims.get("main_material_type"):
            raise DomainError("report_row_invalid", "素材行缺少主素材身份")
        return {"stat_date": day, "main_material_id": str(dims["main_material_id"]),
                "main_material_type": str(dims["main_material_type"]),
                "main_material_name": metrics.get("main_material_name")}, day

    return _publish_simple(session, model=MaterialReportDay, kind="material_day", key_builder=keys,
                           tenant_id=tenant_id, job_id=job_id, accounts=accounts, start=start, end=end,
                           rows=rows, value_parser=material_metric_values,
                           time_filter=lambda m: m.stat_date.between(start, end))


def publish_account_reconciliation(session, *, tenant_id, accounts, start, end, rows) -> None:
    """账户报表与同期广告汇总比对，结果只用于页面提示。"""
    reported: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        advertiser = _account(row, accounts)
        day = _in_range(parse_local_day((row.get("dimensions") or {}).get("stat_time_day")), start, end)
        metrics = row.get("metrics") or {}
        reported[(advertiser, day)] = {"spend": parse_decimal(metrics.get("spend")),
                                       "total": parse_decimal(metrics.get("native_growth_total_ad_impression_value"))}
    sums = session.execute(text("""
        SELECT advertiser_id, stat_date,
               CASE WHEN COUNT(spend)=COUNT(*) THEN SUM(spend) END,
               CASE WHEN COUNT(revenue_total)=COUNT(*) THEN SUM(revenue_total) END
        FROM ad_report_day WHERE tenant_id=:t AND advertiser_id = ANY(:a) AND stat_date BETWEEN :s AND :e
        GROUP BY advertiser_id, stat_date"""), {"t": tenant_id, "a": sorted(accounts), "s": start, "e": end}).all()
    summed = {(a, d): (s, r) for a, d, s, r in sums}
    now = datetime.now(UTC)
    for advertiser in accounts:
        for day in _days(start, end):
            rep = reported.get((advertiser, day), {"spend": None, "total": None})
            ad_spend, ad_total = summed.get((advertiser, day), (None, None))
            session.merge(AccountReconciliationDay(
                tenant_id=tenant_id, advertiser_id=advertiser, stat_date=day,
                account_spend=rep["spend"], ad_sum_spend=ad_spend,
                account_revenue_total=rep["total"], ad_sum_revenue_total=ad_total, checked_at=now))
    session.flush()
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/modules/reporting/test_publish.py`
Expected: 6 passed

- [ ] **Step 6: 补测试**（追加到 `test_publish.py`）：`publish_campaign_hour` 对香港账户 `2026-10-05 10:00:00` 写入 `stat_hour=2026-10-05T02:00Z`，再次发布空结果后该账户当天小时行被删、UTC 账户行保留；`mark_failed` 对已有成功日期写 `STALE`、新日期写 `FAILED`；`publish_account_reconciliation` 写入账户 499.29 与广告汇总值。运行 `pytest tests/modules/reporting/test_publish.py`，Expected: 全部通过。

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/reporting/publish.py backend/tests/modules/reporting/sync_conftest.py \
  backend/tests/modules/reporting/conftest.py backend/tests/modules/reporting/test_publish.py
git commit -m "reporting: 报表按账户日期段整份替换发布并在库内汇总系列"
```

---

### Task 6: 同步任务执行、重试与防截断

**Files:**
- Create: `backend/app/modules/reporting/jobs.py`
- Test: `backend/tests/modules/reporting/test_jobs.py`

**Interfaces:**
- Consumes: Task 4 `ReportReader`/`RawReportPage`，Task 5 发布函数。
- Produces:

```python
PRIORITY = {"hot": 1, "manual": 1, "material": 2, "daily": 3, "history": 4}
TRUNCATION_ROWS = 20_000
MAX_PROVIDER_RETRIES = 5
def enqueue_job(session, *, tenant_id, bc_id, kind: str, tier: str, advertiser_ids: Sequence[str],
                start: date, end: date, frozen_route: dict, actor_id: UUID,
                campaign_ids: Sequence[str] = (), due_at: datetime | None = None) -> UUID | None
def claim_jobs(session, *, now: datetime, limit: int) -> list[ReportSyncJob]
def execute_job(session_factory, *, job_id: UUID, claim_token: UUID, reader: ReportReader) -> str
    # 返回 "COMPLETE" | "WAITING" | "FAILED" | "SPLIT"
```

  `kind ∈ {"bc_spend","ad_day","campaign_hour","campaign_day_utc","material_day","account_day"}`；`bc_spend` 的执行结果由 Task 7 处理（`execute_job` 对 `bc_spend` 返回读取到的行，见 `run_bc_spend`）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_jobs.py
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting.jobs import claim_jobs, enqueue_job, execute_job
from app.modules.reporting.store_models import AdReportDay, ReportSyncJob, ReportSyncState
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


class FakeReader:
    def __init__(self, pages=None, error=None, total=None):
        self.pages, self.error, self.total, self.calls = pages or {}, error, total, []

    def ad_day(self, *, advertiser_ids, start, end, page, campaign_ids=()):
        self.calls.append((tuple(advertiser_ids), tuple(campaign_ids), page))
        if self.error:
            raise self.error
        rows = self.pages.get((tuple(advertiser_ids), tuple(campaign_ids), page), [])
        total = self.total(advertiser_ids, campaign_ids) if self.total else len(rows)
        return RawReportPage(tuple(rows), page, 1 if total else 0, total)


def _route(seed):
    return {"tenant_id": str(seed.tenant_id), "bc_id": "bc-a", "connection_id": str(seed.connection.id),
            "channel": "OFFICIAL_API", "authorization_revision": 0,
            "adapter_contract_revision": "official-api-v1", "binding_revision": 0}


def _claim_one(session):
    jobs = claim_jobs(session, now=datetime.now(UTC) + timedelta(seconds=1), limit=10)
    session.commit()
    return jobs[0]


def _factory(session):
    return lambda: Session(bind=session.get_bind())


def test_enqueue_is_idempotent_while_active(session, sync_seed):
    args = dict(tenant_id=sync_seed.tenant_id, bc_id="bc-a", kind="ad_day", tier="hot",
                advertiser_ids=["acc-1"], start=D, end=D, frozen_route=_route(sync_seed),
                actor_id=sync_seed.actor_id)
    assert enqueue_job(session, **args) is not None
    assert enqueue_job(session, **args) is None


def test_execute_publishes_complete(committed_sync_seed):
    seed, factory = committed_sync_seed
    with factory() as s:
        enqueue_job(s, tenant_id=seed.tenant_id, bc_id="bc-a", kind="ad_day", tier="hot",
                    advertiser_ids=["acc-1"], start=D, end=D, frozen_route=_route(seed), actor_id=seed.actor_id)
        s.commit()
        job = claim_jobs(s, now=datetime.now(UTC) + timedelta(seconds=1), limit=1)[0]
        s.commit()
    reader = FakeReader(pages={(("acc-1",), (), 1): [ad_row("a1", D)]})
    assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "COMPLETE"
    with factory() as s:
        assert len(s.exec(select(AdReportDay).where(AdReportDay.tenant_id == seed.tenant_id)).all()) == 1


def test_rate_limit_waits_without_counting_attempt(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=AccountAdmissionDeferred(300_000))
    assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "WAITING"
    with factory() as s:
        row = s.get(ReportSyncJob, job.id)
        assert row.attempts == 0 and row.due_at > datetime.now(UTC) + timedelta(seconds=290)


def test_provider_errors_back_off_then_fail(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=DomainError("tiktok_response_error", "x"))
    for attempt in range(1, 6):
        assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "WAITING"
        job = _reclaim(factory, job.id)
    assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "FAILED"
    with factory() as s:
        assert s.get(ReportSyncState, (seed.tenant_id, "acc-1", "ad_day", D)).status == "FAILED"


def test_contract_error_fails_immediately(committed_sync_seed):
    seed, factory = committed_sync_seed
    job = _enqueue_and_claim(factory, seed)
    reader = FakeReader(error=DomainError("report_query_invalid", "x"))
    assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "FAILED"


def test_truncation_splits_then_fails_visibly(committed_sync_seed):
    seed, factory = committed_sync_seed
    # 两个账户合并触顶 → 拆成单账户；单账户仍触顶且已有系列 ID → 按系列分批；系列批仍触顶 → 失败
    job = _enqueue_and_claim(factory, seed, advertisers=["acc-1", "acc-2"])
    reader = FakeReader(total=lambda advs, camps: 20_000)
    assert execute_job(factory, job_id=job.id, claim_token=job.claim_token, reader=reader) == "SPLIT"
    with factory() as s:
        children = s.exec(select(ReportSyncJob).where(ReportSyncJob.tenant_id == seed.tenant_id,
                                                      ReportSyncJob.status == "QUEUED")).all()
        assert sorted(c.advertiser_ids for c in children) == [["acc-1"], ["acc-2"]]
```

`committed_sync_seed`、`_enqueue_and_claim`、`_reclaim` 在本测试文件内实现：`committed_sync_seed` 用 `engine` 开独立会话写入与 `sync_seed` 相同的种子并 `commit`，测试结束删除该租户的 `report_sync_job/ad_report_day/campaign_report_day/report_sync_state/ad_sync_*` 行及种子行（参考 `tests/modules/reporting/test_sync_recovery.py` 中 `committed_recovery` 的写法）；`_enqueue_and_claim(factory, seed, advertisers=["acc-1"])` 入队一个 `ad_day/hot` 任务并领取返回；`_reclaim(factory, job_id)` 把该任务 `due_at` 改到过去后重新 `claim_jobs`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/modules/reporting/test_jobs.py`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# backend/app/modules/reporting/jobs.py
"""报表同步任务：一次请求（含全部分页）一行。

- 领取按租户轮转、按优先级与到期时间排序，大租户不能挤占小租户。
- 一次执行内读完全部分页再整份发布；超时或中断则下次从第 1 页重来，绝不发布半份。
- 额度/限流延后不计重试次数；读取错误最多 5 次指数退避；合同错误立即失败。
- 平台单次最多 2 万行，触顶即视为可能截断：先减少合并账户，再按系列 30 个一批。
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.report_reader import ReportReader
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import publish
from app.modules.reporting.store_models import CampaignReportDay, ReportSyncJob

PRIORITY = {"hot": 1, "manual": 1, "material": 2, "daily": 3, "history": 4}
TRUNCATION_ROWS = 20_000
MAX_PROVIDER_RETRIES = 5
LEASE = timedelta(seconds=240)
_CONTRACT_ERRORS = frozenset({"report_query_invalid", "report_response_invalid", "report_row_invalid",
                              "report_value_invalid", "report_operation_invalid"})
_DEFERRED_CODES = frozenset({"admission_deferred", "admission_unavailable", "tiktok_local_resources_unavailable",
                             "gateway_credentials_changed", "read_deadline_exceeded"})


def _key(kind, tier, advertiser_ids, start, end, campaign_ids) -> str:
    payload = json.dumps([kind, tier, sorted(advertiser_ids), start.isoformat(), end.isoformat(),
                          sorted(campaign_ids)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def enqueue_job(session: Session, *, tenant_id, bc_id, kind, tier, advertiser_ids, start, end,
                frozen_route, actor_id, campaign_ids=(), due_at=None) -> UUID | None:
    """相同 (类型, 档位, 账户, 日期段, 系列) 未结束时不重复入队；返回新任务 ID 或 None。"""
    job_id = uuid4()
    row = session.execute(text("""
        INSERT INTO report_sync_job (id, tenant_id, bc_id, kind, priority, status, advertiser_ids,
            campaign_ids, start_date, end_date, job_key, frozen_route, actor_id, due_at, attempts,
            request_count, created_at)
        VALUES (:id, :tenant, :bc, :kind, :priority, 'QUEUED', CAST(:advs AS jsonb), CAST(:camps AS jsonb),
            :start, :end, :key, CAST(:route AS jsonb), :actor, :due, 0, 0, now())
        ON CONFLICT (job_key) WHERE status IN ('QUEUED','RUNNING','WAITING') DO NOTHING
        RETURNING id"""), {
        "id": job_id, "tenant": tenant_id, "bc": bc_id, "kind": kind, "priority": PRIORITY[tier],
        "advs": json.dumps(list(advertiser_ids)), "camps": json.dumps(list(campaign_ids)),
        "start": start, "end": end, "key": _key(kind, tier, advertiser_ids, start, end, campaign_ids),
        "route": json.dumps(frozen_route), "actor": actor_id, "due": due_at or datetime.now(UTC),
    }).first()
    return row[0] if row else None


def claim_jobs(session: Session, *, now: datetime, limit: int) -> list[ReportSyncJob]:
    """按租户轮转领取：每个租户内按 (优先级, 到期) 排名，先取各租户第 1 名，再取第 2 名……"""
    ids = session.execute(text("""
        WITH ranked AS (
            SELECT id, priority, due_at,
                   ROW_NUMBER() OVER (PARTITION BY tenant_id ORDER BY priority, due_at, id) AS rn
            FROM report_sync_job
            WHERE status IN ('QUEUED','WAITING') AND due_at <= :now
              AND (claimed_until IS NULL OR claimed_until <= :now))
        SELECT id FROM ranked ORDER BY rn, priority, due_at, id LIMIT :limit"""),
        {"now": now, "limit": limit}).scalars().all()
    claimed = []
    for job_id in ids:
        job = session.exec(select(ReportSyncJob).where(ReportSyncJob.id == job_id)
                           .with_for_update(skip_locked=True)).one_or_none()
        if job is None or job.status not in ("QUEUED", "WAITING"):
            continue
        job.status, job.claim_token, job.claimed_until = "RUNNING", uuid4(), now + LEASE
        claimed.append(job)
    session.flush()
    return claimed


def _accounts(session, tenant_id, advertiser_ids) -> dict[str, publish.AccountMeta]:
    rows = session.exec(select(AdvertiserAccount).where(AdvertiserAccount.tenant_id == tenant_id,
                                                        AdvertiserAccount.advertiser_id.in_(advertiser_ids))).all()
    found = {r.advertiser_id: publish.AccountMeta(r.currency, r.timezone) for r in rows if r.currency and r.timezone}
    if set(found) != set(advertiser_ids):
        raise DomainError("account_metadata_incomplete", "广告账户缺少币种或时区")
    return found


def _read_all(read: Callable[[int], object]) -> list[dict]:
    rows, page = [], 1
    while True:
        result = read(page)
        if result.total_number >= TRUNCATION_ROWS:
            raise DomainError("report_truncation_risk", "报表行数达到平台上限，可能被截断")
        rows.extend(result.rows)
        if result.next_page is None:
            return rows
        page = result.next_page


def _split(session, job: ReportSyncJob) -> bool:
    """截断时拆分：多账户 → 单账户；单账户无系列过滤 → 按已知系列 30 个一批。不可拆返回 False。"""
    common = dict(tenant_id=job.tenant_id, bc_id=job.bc_id, kind=job.kind,
                  tier=next(t for t, p in PRIORITY.items() if p == job.priority),
                  start=job.start_date, end=job.end_date, frozen_route=job.frozen_route, actor_id=job.actor_id)
    if len(job.advertiser_ids) > 1:
        for advertiser in job.advertiser_ids:
            enqueue_job(session, advertiser_ids=[advertiser], **common)
        return True
    if job.kind != "ad_day" or job.campaign_ids:
        return False
    campaigns = sorted(set(session.exec(select(CampaignReportDay.campaign_id).where(
        CampaignReportDay.tenant_id == job.tenant_id,
        CampaignReportDay.advertiser_id == job.advertiser_ids[0],
        CampaignReportDay.stat_date >= job.start_date - timedelta(days=32))).all()))
    if not campaigns:
        return False
    for offset in range(0, len(campaigns), 30):
        enqueue_job(session, advertiser_ids=job.advertiser_ids, campaign_ids=campaigns[offset:offset + 30], **common)
    return True


def _run(session, job: ReportSyncJob, reader: ReportReader) -> int:
    advertisers = tuple(job.advertiser_ids)
    accounts = _accounts(session, job.tenant_id, advertisers)
    s, e, t, j = job.start_date, job.end_date, job.tenant_id, job.id
    if job.kind == "ad_day":
        rows = _read_all(lambda p: reader.ad_day(advertiser_ids=advertisers, start=s, end=e, page=p,
                                                 campaign_ids=tuple(job.campaign_ids)))
        return publish.publish_ad_day(session, tenant_id=t, job_id=j, accounts=accounts, start=s, end=e,
                                      rows=rows, campaign_ids=tuple(job.campaign_ids))
    if job.kind == "campaign_hour":
        rows = _read_all(lambda p: reader.campaign_hour(advertiser_ids=advertisers, day=s, page=p))
        return publish.publish_campaign_hour(session, tenant_id=t, job_id=j, accounts=accounts, day=s, rows=rows)
    if job.kind == "campaign_day_utc":
        rows = _read_all(lambda p: reader.campaign_day_utc(advertiser_ids=advertisers, start=s, end=e, page=p))
        return publish.publish_campaign_day_utc(session, tenant_id=t, job_id=j, accounts=accounts,
                                                start=s, end=e, rows=rows)
    if job.kind == "material_day":
        rows = _read_all(lambda p: reader.material_day(advertiser_id=advertisers[0], start=s, end=e, page=p))
        return publish.publish_material_day(session, tenant_id=t, job_id=j, accounts=accounts,
                                            start=s, end=e, rows=rows)
    if job.kind == "account_day":
        rows = _read_all(lambda p: reader.account_day(advertiser_ids=advertisers, start=s, end=e, page=p))
        publish.publish_account_reconciliation(session, tenant_id=t, accounts=accounts, start=s, end=e, rows=rows)
        return len(rows)
    raise DomainError("report_job_kind_invalid", "未知报表任务类型")


def execute_job(session_factory: Callable[[], Session], *, job_id: UUID, claim_token: UUID,
                reader: ReportReader) -> str:
    with session_factory() as session:
        job = session.exec(select(ReportSyncJob).where(ReportSyncJob.id == job_id).with_for_update()).one()
        if job.claim_token != claim_token or job.status != "RUNNING":
            return "FAILED"  # 旧领取者：不得改写新领取的任务
        try:
            _run(session, job, reader)
            job.status, job.error_code, job.finished_at = "COMPLETE", None, datetime.now(UTC)
            result = "COMPLETE"
        except AccountAdmissionDeferred as deferred:
            session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            job.status, job.error_code = "WAITING", "admission_deferred"
            job.due_at = datetime.now(UTC) + timedelta(milliseconds=max(1000, deferred.retry_after_ms))
            result = "WAITING"
        except DomainError as error:
            session.rollback()
            job = session.get(ReportSyncJob, job_id, with_for_update=True)
            result = _handle_error(session, job, error)
        job.claim_token = job.claimed_until = None
        session.commit()
        return result


def _handle_error(session, job: ReportSyncJob, error: DomainError) -> str:
    job.error_code = error.code
    if error.code == "report_truncation_risk":
        if _split(session, job):
            job.status, job.finished_at = "COMPLETE", datetime.now(UTC)
            return "SPLIT"
    elif error.code in _DEFERRED_CODES:
        job.status, job.due_at = "WAITING", datetime.now(UTC) + timedelta(seconds=30)
        return "WAITING"
    elif error.code not in _CONTRACT_ERRORS and job.attempts < MAX_PROVIDER_RETRIES:
        job.attempts += 1
        job.status = "WAITING"
        job.due_at = datetime.now(UTC) + timedelta(seconds=min(1800, 60 * 2 ** (job.attempts - 1)))
        return "WAITING"
    job.status, job.finished_at = "FAILED", datetime.now(UTC)
    publish.mark_failed(session, tenant_id=job.tenant_id, advertiser_ids=job.advertiser_ids,
                        kind=job.kind, start=job.start_date, end=job.end_date, error_code=error.code)
    return "FAILED"
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/modules/reporting/test_jobs.py`
Expected: 全部通过

- [ ] **Step 5: 补测试**——单账户无系列 ID 时截断（`campaign_report_day` 无数据）直接 `FAILED` 且 `report_sync_state` 标记；已有 3 个系列时拆成 1 个带 `campaign_ids` 的子任务。运行通过。

- [ ] **Step 6: 提交**

```bash
git add backend/app/modules/reporting/jobs.py backend/tests/modules/reporting/test_jobs.py
git commit -m "reporting: 报表任务按租户轮转执行，限流延后、读取退避并在截断时拆分"
```

---

### Task 7: BC 计划、活跃账户筛选与档位调度

**Files:**
- Create: `backend/app/modules/reporting/planner.py`
- Modify: `backend/app/core/config.py`（`TIKTOK_REPORT_DAILY_CALL_BUDGET: int = 691_200`）、`.env.example`（加同名项与注释）
- Test: `backend/tests/modules/reporting/test_planner.py`

**Interfaces:**
- Consumes: Task 6 `enqueue_job`, `PRIORITY`；Task 4 `ReportReader.bc_spend/account_day`。
- Produces:

```python
def ensure_bc_plans(session, *, now: datetime) -> int          # 为所有 API 绑定 BC 建/停计划，返回启用数
def account_owners(session, *, tenant_id: UUID) -> dict[str, str]  # advertiser_id -> owner_bc_id
def due_plans(session, *, now: datetime, limit: int = 50) -> list[AdSyncBc]
def plan_hot(session, plan: AdSyncBc, *, active: Sequence[str], now: datetime) -> int
def plan_material(session, plan, *, active, now) -> int
def plan_daily(session, plan, *, active_32d: Sequence[str], now) -> int
def plan_history(session, plan, *, now) -> int
def screen_active(reader: ReportReader, *, bc_id: str, start: date, end: date,
                  candidates: set[str]) -> set[str]
def batch_accounts(session, *, tenant_id, advertiser_ids: Sequence[str], days: int) -> list[list[str]]
def local_today(timezone: str, now: datetime) -> date
def calls_today(redis, now) -> int; def record_calls(redis, now, count) -> None
```

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_planner.py
from datetime import UTC, date, datetime, timedelta

from sqlmodel import select

from app.integrations.tiktok.contracts.report_reader import RawReportPage
from app.modules.reporting.planner import (
    account_owners, batch_accounts, ensure_bc_plans, plan_daily, plan_history, plan_hot,
    screen_active,
)
from app.modules.reporting.store_models import AdSyncBc, ReportSyncJob

NOW = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)


def test_auth_creates_plans_without_manual_refresh(session, sync_seed):
    assert ensure_bc_plans(session, now=NOW) == 2
    plans = session.exec(select(AdSyncBc).where(AdSyncBc.tenant_id == sync_seed.tenant_id)).all()
    assert {p.bc_id for p in plans} == {"bc-a", "bc-b"} and all(p.next_hot_at <= NOW for p in plans)


def test_shared_account_is_planned_once(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    owners = account_owners(session, tenant_id=sync_seed.tenant_id)
    assert owners == {"acc-1": "bc-a", "acc-2": "bc-a"}  # 按 bc_id 字典序选主 BC，稳定不抖动


def test_mcp_only_bc_is_not_planned(session, sync_seed):
    from app.modules.accounts.connection_models import BCConnectionBinding
    binding = session.get(BCConnectionBinding, (sync_seed.tenant_id, "bc-b", sync_seed.connection.id))
    binding.kind = "OFFICIAL_MCP"  # 仅测试：模拟只绑 MCP
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-b"))
    assert plan is None or (plan.enabled is False and plan.disabled_reason == "api_binding_required")


def test_screen_uses_bc_report_and_filters_candidates():
    class R:
        def bc_spend(self, *, bc_id, start, end, page):
            rows = ({"dimensions": {"advertiser_id": "acc-1"}, "metrics": {"spend": "3.10"}},
                    {"dimensions": {"advertiser_id": "acc-9"}, "metrics": {"spend": "1.00"}},
                    {"dimensions": {"advertiser_id": "acc-2"}, "metrics": {"spend": "0.000"}})
            return RawReportPage(rows, 1, 1, 3)
    assert screen_active(R(), bc_id="bc-a", start=date(2026, 10, 12), end=date(2026, 10, 12),
                         candidates={"acc-1", "acc-2"}) == {"acc-1"}


def test_hot_plan_jobs_and_utc_noon_rule(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=["acc-1", "acc-2"], now=NOW)
    jobs = session.exec(select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)).all()
    ad_jobs = [j for j in jobs if j.kind == "ad_day"]
    # UTC 12:00 前：香港账户今天 10-12 与昨天 10-11 都要刷；hour 任务今天、昨天各一个
    assert {(j.start_date, j.end_date) for j in ad_jobs} >= {(date(2026, 10, 11), date(2026, 10, 12))}
    assert {j.kind for j in jobs} == {"ad_day", "campaign_hour"}
    assert all(j.priority == 1 for j in jobs)


def test_inactive_account_keeps_published_rows_until_daily_refresh(session, sync_seed):
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_hot(session, plan, active=[], now=NOW)
    assert session.exec(select(ReportSyncJob).where(ReportSyncJob.tenant_id == sync_seed.tenant_id)).all() == []
    plan_daily(session, plan, active_32d=["acc-1"], now=NOW.replace(hour=13))
    kinds = {j.kind for j in session.exec(select(ReportSyncJob).where(
        ReportSyncJob.tenant_id == sync_seed.tenant_id)).all()}
    assert kinds == {"ad_day", "material_day", "campaign_day_utc", "account_day"}


def test_history_shards_30_days_from_account_creation(session, sync_seed):
    from app.modules.accounts.models import AdvertiserAccount
    acc = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    acc.remote_created_at = datetime(2026, 7, 1, tzinfo=UTC)
    session.flush()
    ensure_bc_plans(session, now=NOW)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    plan_history(session, plan, now=NOW)
    ad = sorted((j.start_date, j.end_date) for j in session.exec(select(ReportSyncJob).where(
        ReportSyncJob.tenant_id == sync_seed.tenant_id, ReportSyncJob.kind == "ad_day")).all())
    assert ad[0][0] == date(2026, 7, 1) and all((e - s).days < 30 for s, e in ad)
    assert ad[-1][1] < date(2026, 10, 12) - timedelta(days=31)
    plan_history(session, plan, now=NOW)  # 第二次不重复规划
    assert len(session.exec(select(ReportSyncJob).where(ReportSyncJob.kind == "ad_day",
               ReportSyncJob.tenant_id == sync_seed.tenant_id)).all()) == len(ad)


def test_batch_accounts_shrinks_for_large_accounts(session, sync_seed):
    assert batch_accounts(session, tenant_id=sync_seed.tenant_id, advertiser_ids=["acc-1", "acc-2"], days=1) == [["acc-1", "acc-2"]]


def test_ten_thousand_accounts_one_round_call_budget():
    """规模估算：1 万账户、2000 活跃、50 个 BC 的一轮热刷请求数在规格范围内。"""
    from app.modules.reporting.planner import estimate_round_requests
    assert estimate_round_requests(bcs=50, active=2000, batch=5, hour_pages=1, ad_pages=1.5) <= 2200
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/modules/reporting/test_planner.py`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# backend/app/modules/reporting/planner.py
"""报表同步计划：每个已绑定官方 API 连接的 BC 一行，授权完成即自动建立。

一个账户若被多个 BC 授权，只由 bc_id 字典序最小的 BC 计划拉取，避免重复调用；
数据按账户存放，各 BC 的可见性由查询层按当前授权判断。
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from math import ceil
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.integrations.tiktok.contracts.report_reader import ReportReader
from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess, DiscoveryRun
from app.modules.accounts.routing import freeze_route
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.reporting.jobs import enqueue_job
from app.modules.reporting.partitions import ensure_month_partitions_for_range
from app.modules.reporting.store_models import AdSyncAccount, AdSyncBc

HOT_INTERVAL = timedelta(minutes=30)
MATERIAL_INTERVAL = timedelta(minutes=60)
DIRECTORY_INTERVAL = timedelta(hours=3)
REFRESH_DAYS = 32
DAILY_AT = time(12, 30)          # 平台 UTC 12:00 修正数据之后
ROWS_PER_BATCH_TARGET = 15_000   # 预估行数超过即减少合并账户（平台上限 2 万）


def local_today(timezone: str, now: datetime) -> date:
    return now.astimezone(ZoneInfo(timezone)).date()


def _next_daily(now: datetime) -> datetime:
    target = datetime.combine(now.date(), DAILY_AT, UTC)
    return target if target > now else target + timedelta(days=1)


def _actor(session, tenant_id: UUID, bc_id: str, connection_id: UUID) -> UUID | None:
    """后台身份：该 BC 最近一次完成账户发现的操作者。"""
    run = session.exec(select(DiscoveryRun).where(
        DiscoveryRun.tenant_id == tenant_id, DiscoveryRun.connection_id == connection_id,
        DiscoveryRun.status == "COMPLETE").order_by(col(DiscoveryRun.id).desc())).first()
    return run.actor_id if run else None


def ensure_bc_plans(session: Session, *, now: datetime) -> int:
    enabled = 0
    bindings = session.exec(select(BCConnectionBinding).where(BCConnectionBinding.status == "ACTIVE")).all()
    api = {(b.tenant_id, b.bc_id): b for b in bindings if b.kind == "OFFICIAL_API"}
    for (tenant_id, bc_id) in {(b.tenant_id, b.bc_id) for b in bindings}:
        plan = session.get(AdSyncBc, (tenant_id, bc_id))
        binding = api.get((tenant_id, bc_id))
        if binding is None:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, "api_binding_required"
            continue
        actor = _actor(session, tenant_id, bc_id, binding.connection_id) or (plan.actor_id if plan else None)
        if actor is None:
            continue  # 尚无完成的账户发现：等待授权流程完成
        context = TenantContext(tenant_id=tenant_id, actor_id=actor, role="operator")
        try:
            route = freeze_route(session, context=context, bc_id=bc_id, connection_id=binding.connection_id)
        except DomainError as error:
            if plan is not None:
                plan.enabled, plan.disabled_reason = False, error.code
            continue
        route_json = route.model_dump(mode="json")
        if plan is None:
            plan = AdSyncBc(tenant_id=tenant_id, bc_id=bc_id, frozen_route=route_json, actor_id=actor,
                            next_hot_at=now, next_material_at=now, next_directory_at=now,
                            next_daily_at=_next_daily(now))
            session.add(plan)
        plan.frozen_route, plan.actor_id, plan.enabled, plan.disabled_reason = route_json, actor, True, None
        plan.updated_at = now
        enabled += 1
    session.flush()
    _refresh_owners(session)
    return enabled


def _refresh_owners(session) -> None:
    session.execute(text("""
        INSERT INTO ad_sync_account (tenant_id, advertiser_id, owner_bc_id)
        SELECT a.tenant_id, a.advertiser_id, MIN(a.bc_id)
        FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
        WHERE a.in_bc AND a.authorized AND a.active
        GROUP BY a.tenant_id, a.advertiser_id
        ON CONFLICT (tenant_id, advertiser_id) DO UPDATE SET owner_bc_id = EXCLUDED.owner_bc_id"""))
    session.execute(text("""
        DELETE FROM ad_sync_account s WHERE NOT EXISTS (
          SELECT 1 FROM bc_account_access a JOIN ad_sync_bc p ON p.tenant_id=a.tenant_id AND p.bc_id=a.bc_id AND p.enabled
          WHERE a.tenant_id=s.tenant_id AND a.advertiser_id=s.advertiser_id AND a.in_bc AND a.authorized AND a.active)"""))


def account_owners(session, *, tenant_id: UUID) -> dict[str, str]:
    return dict(session.exec(select(AdSyncAccount.advertiser_id, AdSyncAccount.owner_bc_id)
                             .where(AdSyncAccount.tenant_id == tenant_id)).all())


def owned_accounts(session, plan: AdSyncBc) -> list[str]:
    return sorted(session.exec(select(AdSyncAccount.advertiser_id).where(
        AdSyncAccount.tenant_id == plan.tenant_id, AdSyncAccount.owner_bc_id == plan.bc_id)).all())


def due_plans(session, *, now: datetime, limit: int = 50) -> list[AdSyncBc]:
    return list(session.exec(select(AdSyncBc).where(
        col(AdSyncBc.enabled).is_(True),
        (AdSyncBc.next_hot_at <= now) | (AdSyncBc.next_material_at <= now)
        | (AdSyncBc.next_daily_at <= now) | (AdSyncBc.next_directory_at <= now))
        .order_by(col(AdSyncBc.next_hot_at)).limit(limit).with_for_update(skip_locked=True)).all())


def screen_active(reader: ReportReader, *, bc_id: str, start: date, end: date, candidates: set[str]) -> set[str]:
    """BC 报表一次返回整个 BC 下账户的消耗；只保留本计划负责的账户。"""
    active, page = set(), 1
    while True:
        result = reader.bc_spend(bc_id=bc_id, start=start, end=end, page=page)
        for row in result.rows:
            advertiser = str((row.get("dimensions") or {}).get("advertiser_id"))
            spend = (row.get("metrics") or {}).get("spend")
            if advertiser in candidates and spend not in (None, "-") and float(spend) > 0:
                active.add(advertiser)
        if result.next_page is None:
            return active
        page = result.next_page


def batch_accounts(session, *, tenant_id, advertiser_ids: Sequence[str], days: int) -> list[list[str]]:
    """按近 7 天单日最大广告行数估计每个账户的行量，合并到不超过 5 个且预估 ≤ 1.5 万行。"""
    sizes = dict(session.execute(text("""
        SELECT advertiser_id, MAX(n) FROM (
          SELECT advertiser_id, stat_date, COUNT(*) n FROM ad_report_day
          WHERE tenant_id=:t AND advertiser_id = ANY(:a) AND stat_date >= CURRENT_DATE - 7
          GROUP BY advertiser_id, stat_date) x GROUP BY advertiser_id"""),
        {"t": tenant_id, "a": list(advertiser_ids)}).all())
    batches, current, load = [], [], 0
    for advertiser in sorted(advertiser_ids):
        estimate = sizes.get(advertiser, 100) * days
        if current and (len(current) == 5 or load + estimate > ROWS_PER_BATCH_TARGET):
            batches.append(current)
            current, load = [], 0
        current.append(advertiser)
        load += estimate
    if current:
        batches.append(current)
    return batches


def _timezones(session, tenant_id, advertiser_ids) -> dict[str, str]:
    return dict(session.exec(select(AdvertiserAccount.advertiser_id, AdvertiserAccount.timezone).where(
        AdvertiserAccount.tenant_id == tenant_id, AdvertiserAccount.advertiser_id.in_(advertiser_ids))).all())


def _hot_window(timezone: str, now: datetime) -> tuple[date, date]:
    today = local_today(timezone, now)
    # UTC 12:00 前平台还会修正昨天的数据，热刷一并覆盖昨天。
    start = today - timedelta(days=1) if now.time() < time(12, 0) else today
    return start, today


def _common(plan: AdSyncBc) -> dict:
    return dict(tenant_id=plan.tenant_id, bc_id=plan.bc_id, frozen_route=plan.frozen_route, actor_id=plan.actor_id)


def _group_by_window(session, plan, advertisers, window_fn) -> dict[tuple[date, date], list[str]]:
    grouped: dict[tuple[date, date], list[str]] = {}
    for advertiser, timezone in _timezones(session, plan.tenant_id, advertisers).items():
        grouped.setdefault(window_fn(timezone), []).append(advertiser)
    return grouped


def plan_hot(session, plan: AdSyncBc, *, active: Sequence[str], now: datetime, tier: str = "hot") -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(session, plan, active, lambda tz: _hot_window(tz, now)).items():
        for batch in batch_accounts(session, tenant_id=plan.tenant_id, advertiser_ids=advertisers,
                                    days=(end - start).days + 1):
            count += bool(enqueue_job(session, kind="ad_day", tier=tier, advertiser_ids=batch,
                                      start=start, end=end, **_common(plan)))
            day = start
            while day <= end:
                count += bool(enqueue_job(session, kind="campaign_hour", tier=tier, advertiser_ids=batch,
                                          start=day, end=day, **_common(plan)))
                day += timedelta(days=1)
    return count


def plan_material(session, plan, *, active, now, tier: str = "material") -> int:
    count = 0
    for (start, end), advertisers in _group_by_window(session, plan, active, lambda tz: _hot_window(tz, now)).items():
        for advertiser in advertisers:  # 素材报表不支持多账户合并
            count += bool(enqueue_job(session, kind="material_day", tier=tier, advertiser_ids=[advertiser],
                                      start=start, end=end, **_common(plan)))
    return count


def _shards(start: date, end: date, days: int = 30):
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        yield cursor, stop
        cursor = stop + timedelta(days=1)


def plan_daily(session, plan, *, active_32d, now) -> int:
    count = 0
    window = lambda tz: (local_today(tz, now) - timedelta(days=REFRESH_DAYS - 1), local_today(tz, now))
    for (start, end), advertisers in _group_by_window(session, plan, active_32d, window).items():
        for s, e in _shards(start, end):
            for batch in batch_accounts(session, tenant_id=plan.tenant_id, advertiser_ids=advertisers,
                                        days=(e - s).days + 1):
                for kind in ("ad_day", "campaign_day_utc", "account_day"):
                    count += bool(enqueue_job(session, kind=kind, tier="daily", advertiser_ids=batch,
                                              start=s, end=e, **_common(plan)))
            for advertiser in advertisers:
                count += bool(enqueue_job(session, kind="material_day", tier="daily", advertiser_ids=[advertiser],
                                          start=s, end=e, **_common(plan)))
    return count


def plan_history(session, plan, *, now) -> int:
    """授权后一次：开户日至回刷窗口之前，按 30 天切片；已规划的账户不再重复。"""
    count = 0
    rows = session.exec(select(AdSyncAccount, AdvertiserAccount).join(
        AdvertiserAccount, (AdvertiserAccount.tenant_id == AdSyncAccount.tenant_id)
        & (AdvertiserAccount.advertiser_id == AdSyncAccount.advertiser_id)).where(
        AdSyncAccount.tenant_id == plan.tenant_id, AdSyncAccount.owner_bc_id == plan.bc_id,
        col(AdSyncAccount.history_planned_at).is_(None))).all()
    for owner, account in rows:
        if account.remote_created_at is None or not account.timezone:
            continue  # 开户时间未知：等账户目录补齐后再规划
        end = local_today(account.timezone, now) - timedelta(days=REFRESH_DAYS)
        start = account.remote_created_at.astimezone(ZoneInfo(account.timezone)).date()
        if start <= end:
            ensure_month_partitions_for_range(session, start, end)
            for s, e in _shards(start, end):
                for kind in ("ad_day", "campaign_day_utc"):
                    count += bool(enqueue_job(session, kind=kind, tier="history",
                                              advertiser_ids=[account.advertiser_id], start=s, end=e, **_common(plan)))
                count += bool(enqueue_job(session, kind="material_day", tier="history",
                                          advertiser_ids=[account.advertiser_id], start=s, end=e, **_common(plan)))
        owner.history_planned_at = now
    session.flush()
    return count


def estimate_round_requests(*, bcs: int, active: int, batch: int, hour_pages: float, ad_pages: float) -> int:
    """一轮热刷请求量估算（素材按小时一次，折半计入每 30 分钟一轮）。"""
    batches = ceil(active / batch)
    return int(bcs + batches * ad_pages + batches * hour_pages + active / 2)


def _calls_key(now: datetime) -> str:
    return f"tiktok:report:calls:{now:%Y%m%d}"


def record_calls(redis, now: datetime, count: int) -> None:
    key = _calls_key(now)
    redis.incrby(key, count)
    redis.expire(key, 2 * 86400)


def calls_today(redis, now: datetime) -> int:
    return int(redis.get(_calls_key(now)) or 0)
```

`DiscoveryRun.status == "COMPLETE"` 与排序字段以 `models.py` 实际字段为准；若无可排序的完成时间列，改用 `col(DiscoveryRun.revision).desc()` 并在提交说明写明。

- [ ] **Step 4: 运行测试**

Run: `pytest tests/modules/reporting/test_planner.py`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/planner.py backend/app/core/config.py .env.example \
  backend/tests/modules/reporting/test_planner.py
git commit -m "reporting: 授权后自动建立 BC 同步计划，按 BC 报表筛选活跃账户分档规划"
```

---

### Task 8: 目录同步请求迁出并由新计划驱动

**Files:**
- Create: `backend/app/modules/ads/directory_requests.py`
- Modify: `backend/app/modules/ads/sync.py`（`_queue_missing_parent` 改为调用 `request_directory`，删除对 `SyncSchedule` 的引用）
- Modify: `backend/app/modules/ad_management/execution.py`（`_refresh_after_commit` 改为 `request_directory(..., scope="targeted")` + `enqueue` 手动热刷）
- Test: `backend/tests/modules/ads/test_directory_requests.py`

**Interfaces:**
- Produces: `request_directory(session, *, tenant_id, bc_id, frozen_route: dict, actor_id, advertiser_ids: Sequence[str], scope: Literal["directory","active","targeted"], refs: Sequence[EntityRef] = ()) -> int`（返回新建 `AdDirectoryRun` 数）。实现直接移植 `reporting/scheduling.py::_request_directory_sync` 的 `directory_specs`、`directory_filter_chunks` 分片与幂等逻辑；`request_id` 改为对 `(route, advertiser_ids, scope, refs, 当前 30 分钟时间槽)` 做 `uuid5`，使同一周期内重复调用幂等、下一周期生成新请求。目录同步只在计划路由 `channel == "OFFICIAL_API"` 时生成。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/ads/test_directory_requests.py
from sqlmodel import select

from app.modules.ads.directory_requests import request_directory
from app.modules.ads.sync_models import AdDirectoryRun


def test_full_directory_creates_one_run_per_level_and_is_idempotent(session, sync_seed):
    route = {"tenant_id": str(sync_seed.tenant_id), "bc_id": "bc-a", "connection_id": str(sync_seed.connection.id),
             "channel": "OFFICIAL_API", "authorization_revision": 0,
             "adapter_contract_revision": "official-api-v1", "binding_revision": 0}
    args = dict(tenant_id=sync_seed.tenant_id, bc_id="bc-a", frozen_route=route, actor_id=sync_seed.actor_id,
                advertiser_ids=["acc-1"], scope="directory")
    created = request_directory(session, **args)
    assert created == 10  # campaign/adgroup/ad × 3 类型 + Smart+ creative，与原调度一致
    assert request_directory(session, **args) == 0
    runs = session.exec(select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)).all()
    assert all(r.query["include_deleted"] for r in runs)
```

- [ ] **Step 2: 运行确认失败**；**Step 3: 移植实现**（把 `_request_directory_sync` 主体搬入，参数改为上面的签名，删除对 `SyncRequest` 的依赖）；**Step 4: 修改 `ads/sync.py::_queue_missing_parent`**，改为：

```python
    request_directory(session, tenant_id=run.tenant_id, bc_id=run.bc_id, frozen_route=run.frozen_route,
                      actor_id=run.actor_id, advertiser_ids=[ref[1]], scope="targeted",
                      refs=[EntityRef(ref[0], ref[1], ref[2], ref[3])])
```

  同时删除 `_schedule_key` 与 `SyncSchedule` 导入。

- [ ] **Step 5: 修改 `ad_management/execution.py::_refresh_after_commit`**：

```python
    with Session(database_engine) as session, session.begin():
        plan = session.get(AdSyncBc, (route.tenant_id, route.bc_id))
        if plan is None or not plan.enabled:
            return  # 未接入 API 同步：管理操作回执已保存，无报表可刷新
        request_directory(session, tenant_id=route.tenant_id, bc_id=route.bc_id,
                          frozen_route=plan.frozen_route, actor_id=plan.actor_id,
                          advertiser_ids=[ref.advertiser_id], scope="targeted", refs=[ref])
        plan_hot(session, plan, active=[ref.advertiser_id], now=datetime.now(UTC), tier="manual")
```

- [ ] **Step 6: 运行**

Run: `pytest tests/modules/ads tests/modules/ad_management`
Expected: 新测试通过；`test_directory_sync.py` 中依赖 `SyncSchedule` 的断言改为断言新建了 targeted `AdDirectoryRun`（按新行为更新测试，不保留旧断言）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/ads/directory_requests.py backend/app/modules/ads/sync.py \
  backend/app/modules/ad_management/execution.py backend/tests/modules/ads backend/tests/modules/ad_management
git commit -m "ads: 目录同步请求独立于报表调度，管理操作后定向刷新目录与当天报表"
```

---

### Task 9: Beat 扫描、Celery 执行入口与额度放缓

**Files:**
- Create: `backend/app/modules/reporting/job_tasks.py`
- Modify: `backend/app/jobs/celery_app.py`（beat：`scan-ads-reporting-due` 改指向 `reporting.plan_due`；新增 `reporting.cleanup_store` 每小时）
- Test: `backend/tests/modules/reporting/test_job_tasks.py`、更新 `backend/tests/jobs/test_ads_reporting_queues.py`

**Interfaces:**
- Consumes: Task 6/7/8 全部。
- Produces: Celery 任务 `reporting.plan_due`（control 队列）、`reporting.job_step`（ads-reporting 队列，`time_limit=200, soft_time_limit=180`，payload `{"job_id","claim_token"}`）、`reporting.cleanup_store`（ads-reporting）；函数 `plan_due_runs(*, database_engine, redis_client, now=None) -> int`、`run_job_step(*, database_engine, redis_client, job_id, claim_token) -> str`、`cleanup_store(session, *, now) -> dict`。

`plan_due_runs` 行为（一个事务内，`ADS_SYNC_ENABLED=false` 直接返回 0）：
1. `ensure_month_partitions(session)`；`ensure_bc_plans(session, now=now)`。
2. 额度放缓：`used = calls_today(redis, now)`；`budget = settings.TIKTOK_REPORT_DAILY_CALL_BUDGET`；剩余比例 < 30% 时本轮把热刷间隔改为 60 分钟、素材 120 分钟（写回计划的 next_* 时间）。
3. 对 `due_plans` 中每个计划：到期的档位依次执行——
   - 需要筛选时用 `open_tiktok_gateway(route=冻结路由)` 打开网关取 `report_reader`（为 `None` 时把计划停用为 `api_binding_required`），`plan.bc_report_supported is not False` 时调用 `screen_active`；`bc_report_supported is False` 时改用 `account_day` 每次 5 个账户筛选。`screen_active` 首次抛 `DomainError` 且错误码为权限类（`tiktok_response_error` 且消息含权限）时把 `bc_report_supported` 置 False 并在下一轮退回。
   - `next_hot_at` 到期：`plan_hot(active=今日活跃)`；`last_active_advertiser_ids` 更新；`next_hot_at += 30 分钟`（跳过已错过的槽）。
   - `next_material_at` 到期：`plan_material(active=last_active_advertiser_ids)`。
   - `next_daily_at` 到期：筛选最近 32 天活跃 → `plan_daily`；`plan_history`；清理过期。
   - `next_directory_at` 到期：`request_directory(scope="directory")` 对全部负责账户，`scope="active"` 对活跃账户；`next_directory_at += 3 小时`。
   - 每次物理筛选请求 `record_calls(redis, now, n)`。
4. `claim_jobs(limit=100)`，对每个领取的任务 `enqueue_after_commit(task_name="reporting.job_step", task_key=f"report-job:{job.id}:{job.claim_token}", payload=...)`。

`run_job_step`：读取任务 → 校验 `claim_token` → `verify_route` → `open_tiktok_gateway(route, task_deadline=now+170s)` → `execute_job(..., reader=gateway.report_reader)` → `record_calls(redis, now, job.request_count)`。`report_reader is None` 时任务失败 `api_binding_required`。

`cleanup_store`：删除 `finished_at < now - 7 天` 的任务；删除 `campaign_report_hour.stat_hour < now - 30 天`；删除过期 `query_snapshot`（`expires_at < now`，级联删除快照行）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_job_tasks.py
from datetime import UTC, datetime

from app.modules.reporting import job_tasks


def test_disabled_flag_plans_nothing(monkeypatch, committed_sync_seed, redis_client):
    _, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", False)
    from app.core.db import engine
    assert job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client) == 0


def test_plan_due_screens_with_bc_report_and_dispatches(monkeypatch, committed_sync_seed, redis_client):
    seed, factory = committed_sync_seed
    monkeypatch.setattr(job_tasks.settings, "ADS_SYNC_ENABLED", True)
    calls = []

    class Reader:
        def bc_spend(self, *, bc_id, start, end, page):
            from app.integrations.tiktok.contracts.report_reader import RawReportPage
            calls.append(bc_id)
            rows = ({"dimensions": {"advertiser_id": "acc-1"}, "metrics": {"spend": "1.00"}},)
            return RawReportPage(rows, 1, 1, 1)

    monkeypatch.setattr(job_tasks, "_reader_for", lambda **kwargs: _ctx(Reader()))
    from app.core.db import engine
    dispatched = job_tasks.plan_due_runs(database_engine=engine, redis_client=redis_client,
                                         now=datetime(2026, 10, 12, 3, tzinfo=UTC))
    assert calls == ["bc-a"] and dispatched >= 2  # 只为负责账户的主 BC 筛选；ad_day + campaign_hour 已派发
```

`_ctx(obj)` 是本文件内的 `contextlib.contextmanager`，`yield obj`；`job_tasks._reader_for(database_engine, redis_client, plan, deadline)` 是可替换的上下文管理器，正式实现为 `open_tiktok_gateway(...)` 后 `yield gateway.report_reader`。DiscoveryRun 种子：`committed_sync_seed` 需为 bc-a/bc-b 各写一条 `status="COMPLETE"` 的 `DiscoveryRun(actor_id=seed.actor_id, connection_id=..., bc_id=...)`；把该写入加到 `committed_sync_seed` 与 `sync_seed`（Task 5 文件）里。

- [ ] **Step 2–4:** 运行失败 → 按上面行为实现 `job_tasks.py`（Celery 装饰写法照抄现有 `reporting/tasks.py` 的 `scan_due`/`sync_step`，`register_dispatch_task("reporting.job_step", "ads-reporting")`、`register_dispatch_task("reporting.plan_due", "control")`）→ 运行 `pytest tests/modules/reporting/test_job_tasks.py tests/jobs/test_ads_reporting_queues.py` 通过（队列测试改为断言新任务名的路由）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/job_tasks.py backend/app/jobs/celery_app.py \
  backend/tests/modules/reporting/test_job_tasks.py backend/tests/jobs/test_ads_reporting_queues.py \
  backend/tests/modules/reporting/sync_conftest.py
git commit -m "reporting: Beat 扫描到期计划派发报表任务，额度紧张时自动放缓"
```

---

### Task 10: 手动刷新与同步状态接口

**Files:**
- Modify: `backend/app/modules/reporting/api.py`（替换 `/ad-sync-runs` 三个路由）、`backend/app/modules/reporting/schemas.py`
- Test: `backend/tests/modules/reporting/test_sync_api.py`

**Interfaces:**
- Produces:
  - `POST /api/v1/tenants/{tenant_id}/ad-sync-refresh?bc_id=` body `{"advertiser_ids": [...]}`（空=该 BC 全部负责账户）→ `{"queued": int, "accounts": int, "status": "QUEUED" | "SYNC_DISABLED" | "API_BINDING_REQUIRED"}`；对选中账户调用 `plan_hot(tier="manual")` 与 `plan_material(tier="manual")`。`ADS_SYNC_ENABLED=false` 返回 `SYNC_DISABLED` 不入队。
  - `GET /api/v1/tenants/{tenant_id}/ad-sync-status?bc_id=&start_date=&end_date=` → `{"accounts": [{"advertiser_id","last_success_at","incomplete_dates":[...],"failed_dates":[...]}], "plan": {"enabled","disabled_reason","next_hot_at"}}`，只返回该 BC 当前授权账户。
  - 路由前缀以 `api.py` 现有 router 为准（与 `query_ads` 同一 router）。

- [ ] **Step 1: 写失败测试**——用 `client`/`report_case` 风格的 API 测试（参照 `tests/modules/reporting/test_api.py` 的登录与 header 写法）：未开启开关时返回 `SYNC_DISABLED` 且无任务；开启后对 bc-a 刷新入队 ≥2 个 `priority=1` 任务；bc-b 状态接口只列 acc-2。
- [ ] **Step 2–4:** 实现并运行 `pytest tests/modules/reporting/test_sync_api.py` 通过。
- [ ] **Step 5: 提交** `git commit -m "reporting: 手动刷新只插队当天热刷，并提供按 BC 的同步状态"`

---

### Task 11: 五维 SQL 汇总、全列排序与完整性提示

**Files:**
- Create: `backend/app/modules/reporting/dimension_query.py`
- Modify: `backend/app/modules/reporting/schemas.py`（`CANONICAL_METRIC_KEYS` 改为新键）、`filters.py`（`SUPPORTED_SORT_FIELDS` 改为新键）、`queries.py`（`_build_snapshot` 调用新函数）、`exports.py`（导出列）
- Test: `backend/tests/modules/reporting/test_dimension_query.py`

**Interfaces:**
- Consumes: Task 1 表；现有 `apply_authorized_scope`/`authorized_grants`、`CampaignNameProjection`、`AdObject`。
- Produces: `build_rows(session, *, context, bc_id, filters: ReportingFilter) -> tuple[ReportRow, ...]`（替代 `aggregation.build_dimension_rows`）。

  公开指标键（`MetricVector.values`）：`spend, impressions, clicks, ad_impression_events, revenue_d0, revenue_d3, revenue_d7, revenue_d14, revenue_d30, revenue_total, roas_d0, roas_d3, roas_d7, roas_d14, roas_d30, roas_total, ctr, cost_per_ad_impression_event`；`MetricVector.attribution` 固定 `"default"`，`timezone` 为账户时区，跨时区汇总的行 `timezone="MIXED"`。

  排序字段：`row_key`、名称类（`name, account_name, campaign_name, adgroup_name, ad_name, material_name, drama_name`）、上述全部指标键、`target_roas, campaign_budget, adgroup_budget`。

SQL 规则：
- 来源：`account/campaign/drama` 用 `campaign_report_day`，`adgroup/ad` 用 `ad_report_day`。
- 授权范围：`advertiser_id IN (该 BC 当前 in_bc ∧ authorized ∧ active 的账户)`，不再按同步任务 BC 过滤（修复多 BC 共用账户不可见）。
- 日期：`stat_date BETWEEN filters.start_date AND filters.end_date`（账户本地日期）。
- 聚合：每列 `CASE WHEN COUNT(col)=COUNT(*) THEN SUM(col) END`；按币种分组，一行多币种时 `metric_buckets` 每币种一个。
- 派生：`roas_x = revenue_x / NULLIF(spend,0)`、`ctr = clicks/NULLIF(impressions,0)`、`cost_per_ad_impression_event = spend/NULLIF(ad_impression_events,0)`。
- 名称：优先 `ad_object.name`，缺失时用事实行带回的名称，最后用 ID。
- 剧：`campaign_id → CampaignNameProjection(provider_label, drama_name)`，VALID 的按 `bc:provider:drama` 归组，否则 `external:advertiser:campaign`。
- 排序：在 Python 层对已汇总行稳定排序即可（行数 = 对象数），空值始终末尾，并列按 `row_key`。
- 完整性：`coverage = {"status": "COMPLETE"|"INCOMPLETE"|"MISSING", "stale_dates":[...], "failed_dates":[...], "updated_at": max(last_success_at)}`，依据 `report_sync_state(report_kind="ad_day")` 覆盖所选日期的每个账户。
- 对账：账户维度行若 `account_reconciliation_day` 中所选日期任一天 `|account_spend - ad_sum_spend| > 0.01` 或收入差 > 0.01，`display["reconciliation"]="DIFF"` 并附 `display["account_report_spend"]`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/reporting/test_dimension_query.py
from datetime import date
from decimal import Decimal
from uuid import uuid4

from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.schemas import ReportingFilter
from tests.modules.reporting.sync_conftest import ad_row

D = date(2026, 10, 5)


def _filters(dimension, **kw):
    return ReportingFilter(dimension=dimension, start_date=D, end_date=D, **kw)


def _seed(session, seed):
    publish_ad_day(session, tenant_id=seed.tenant_id, job_id=uuid4(),
                   accounts={"acc-1": AccountMeta("USD", "Asia/Hong_Kong"), "acc-2": AccountMeta("USD", "UTC")},
                   start=D, end=D, rows=[
                       ad_row("a1", D, campaign="c1", spend="10.00", d0="5.00", total="6.00"),
                       ad_row("a2", D, campaign="c1", spend="5.00", d0="1.00", total="1.00"),
                       ad_row("b1", D, campaign="c9", spend="2.00", d0="-", total="2.00", adv="acc-2"),
                   ])


def test_levels_add_up_and_roas_is_sum_over_sum(session, sync_seed):
    _seed(session, sync_seed)
    ctx = sync_seed.context
    camp = {r.display["remote_id"]: r for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("campaign"))}
    assert camp["c1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert camp["c1"].metric_buckets[0].values["roas_d0"] == Decimal("6.00") / Decimal("15.00")
    acct = {r.display["remote_id"]: r for r in build_rows(session, context=ctx, bc_id="bc-a", filters=_filters("account"))}
    assert acct["acc-1"].metric_buckets[0].values["spend"] == Decimal("15.00")
    assert acct["acc-2"].metric_buckets[0].values["revenue_d0"] is None  # 有成员缺失 → 合计为空


def test_shared_account_visible_in_both_bcs(session, sync_seed):
    _seed(session, sync_seed)
    rows_b = build_rows(session, context=sync_seed.context, bc_id="bc-b", filters=_filters("account"))
    assert [r.display["remote_id"] for r in rows_b] == ["acc-2"]


def test_every_metric_sorts_with_null_last(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(session, context=sync_seed.context, bc_id="bc-a",
                      filters=_filters("ad", sort_by="roas_d0", sort_direction="desc"))
    assert [r.display["remote_id"] for r in rows] == ["a1", "a2", "b1"]


def test_missing_sync_state_marks_incomplete(session, sync_seed):
    _seed(session, sync_seed)
    rows = build_rows(session, context=sync_seed.context, bc_id="bc-a",
                      filters=ReportingFilter(dimension="account", start_date=date(2026, 10, 4), end_date=D))
    assert all(r.coverage["status"] == "INCOMPLETE" for r in rows)  # 10-04 未同步
```

- [ ] **Step 2–4:** 运行失败 → 实现 `dimension_query.py`（`_aggregate(session, *, model, group_cols, tenant_id, advertisers, start, end)` 返回每组 `{currency: {col: value}}`；`_directory(session, ...)` 读 `AdObject` 名称与配置；`_coverage(...)` 读 `ReportSyncState`；`_sort(rows, filters)` 实现排序）→ `queries.py` 的 `_build_snapshot` 改调 `build_rows` → 运行 `pytest tests/modules/reporting/test_dimension_query.py tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_exports.py`，旧测试中依赖 `ReportFact` 种子的改用 `publish_ad_day` 种子（按新行为改写，不保留旧断言）。

- [ ] **Step 5: 提交** `git commit -m "reporting: 五个维度改由广告日数据在库内汇总，所有指标列可排序并提示数据缺口"`

---

### Task 12: 素材维度查询与使用位置

**Files:**
- Create: `backend/app/modules/reporting/material_query.py`
- Modify: `backend/app/modules/reporting/dimension_query.py`（`dimension == "material"` 时委托）
- Test: `backend/tests/modules/reporting/test_material_query.py`

**Interfaces:**
- Produces: `build_material_rows(session, *, context, bc_id, filters) -> tuple[ReportRow, ...]`。行键 `json.dumps([advertiser_id, "material", main_material_id, main_material_type])`；`display` 含 `name/material_name`（`main_material_name` 或目录素材名）、`main_material_id`、`material_type`、`account_id`；`material_uses` 来自 `AdMaterialReference`（同账户 `main_material_id == 主素材 ID` 或 `ad_material_id` 关联），没有使用位置时 `material_uses=()`、`coverage["reason"]="usage_unknown"`，金额照常展示。`revenue_d3/d30` 的 `availability` 为 `"UNSUPPORTED"`。

- [ ] **Step 1: 写失败测试**——`publish_material_day` 写入两个素材（同账户），断言汇总消耗、`revenue_d3` 为 `UNSUPPORTED`、按 `spend` 降序排序、目录有 `AdMaterialReference(main_material_id=...)` 时 `material_uses` 非空。
- [ ] **Step 2–4:** 实现并运行 `pytest tests/modules/reporting/test_material_query.py` 通过。
- [ ] **Step 5: 提交** `git commit -m "reporting: 素材维度按素材日数据汇总并关联广告内使用位置"`

---

### Task 13: 前端改用新指标、全列排序与同步状态

**Files:**
- Modify: `frontend/src/features/ads/{AdsTable.tsx,AdsSummary.tsx,AdsWorkspace.tsx,search.ts}`
- Regenerate: `frontend/src/client/*`（`bash scripts/generate-client.sh`）
- Test: `frontend/tests/` 下现有 ads 相关 Playwright 用例（`grep -rl "AdsTable\|ads" frontend/tests` 定位后更新）

要点：
1. 指标列：消耗、总收入 ROAS（`roas_total`）、D0/D3/D7/D14/D30 ROAS、总收入（`revenue_total`）、展示事件数、单次展示事件成本、点击率；`METRIC_COLUMNS` 数组驱动表头与单元格，每列 `SortableHead` 的 `sortBy` 即指标键；素材维度 D3/D30 列显示"平台未提供"。
2. `rowMetric`：一行多个 `metric_buckets` 时按币种分别显示（`"$15.00 / €2.00"`），不只取第一个。
3. 刷新按钮改调 `AdsReportingService.requestAdSyncRefresh`；返回 `SYNC_DISABLED`/`API_BINDING_REQUIRED` 时显示对应中文提示（"报表同步未开启"/"该 BC 未接入 API，无法同步报表"）。
4. 表格上方显示同步状态：调用 `getAdSyncStatus`，展示"数据更新到 HH:mm（账户本地）"，有 `incomplete_dates` 时显示"以下日期数据不完整：…"。
5. 账户行 `display.reconciliation === "DIFF"` 时在消耗旁显示提示图标与"账户报表：$x"。
6. `AdsSummary` 改用新键（`spend`, `revenue_total`, `roas_total`, `roas_d0` 等）。

- [ ] **Step 1:** 生成客户端：`bash scripts/generate-client.sh`；确认差异只涉及报表接口与新增同步接口。
- [ ] **Step 2:** 按要点修改组件。
- [ ] **Step 3:** `cd frontend && ../.tools/node_modules/.bin/bun x biome check --write src/features/ads && ../.tools/node_modules/.bin/bun run build`，Expected: 构建成功。
- [ ] **Step 4:** 更新并运行相关 Playwright 用例（`bun x playwright test --project workspace <文件>`），Expected: 通过。
- [ ] **Step 5: 提交** `git commit -m "frontend: 报表展示 D0–D30 回收与全列排序，并显示同步状态与对账提示"`

---

### Task 14: 删除旧报表同步与存储

**Files:**
- Create: `backend/app/alembic/versions/0024_drop_legacy_reporting.py`
- Delete: `backend/app/modules/reporting/{contracts.py,facts.py,sync.py,scheduling.py,tasks.py,aggregation.py,trends.py,sync_models.py}`、`backend/app/integrations/tiktok/adapters/{sdk_reporting.py,mcp_reporting.py}`、`backend/app/integrations/tiktok/contracts/reporting.py`
- Modify: `backend/app/modules/reporting/models.py`（只保留 `AccountBalanceObservation`；如余额观测改由 Task 9 的余额任务写入则保留）、`backend/app/modules/reporting/api.py`（删除 `/reports/trend` 路由与相关 schema）、`backend/app/integrations/tiktok/gateway.py`（删除 `reports` 字段与 `reports.*` 旧操作登记，保留 `reports.integrated`、`reports.bc`、`reports.material_breakdown`）、`backend/app/integrations/tiktok/mcp/results.py` 与 `tool-contracts.json` 中报表工具（若仅供旧适配器使用）
- Delete tests: `tests/modules/reporting/{test_aggregation.py,test_facts.py,test_scheduling.py,test_report_sync.py,test_sync_recovery.py,test_trends.py,test_reporting_accuracy.py}`、`tests/integrations/tiktok/test_reporting_adapters.py`；`tests/modules/reporting/conftest.py` 删除旧夹具，只保留新导入
- Modify docs: `docs/runbooks/deployment.md`（`ADS_SYNC_ENABLED` 语义：开启后所有已授权 API 的 BC 自动同步并触发首次历史回补；新增 `TIKTOK_REPORT_DAILY_CALL_BUDGET`；迁移 0023/0024；Celery 新任务名）、`docs/implementation-progress.md`

迁移：

```python
"""Drop legacy reporting facts, coverage, staging, observations, runs and schedules."""

from alembic import op

revision = "0024_drop_legacy_reporting"
down_revision = "0023_reporting_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧事实不迁移：新存储上线后由历史回补重新拉取（规格 5.4）。
    op.execute("""
    DROP TABLE IF EXISTS report_observation, report_fact, report_coverage,
        report_staged_page, report_sync_run, sync_schedule CASCADE;
    """)


def downgrade() -> None:
    raise RuntimeError("legacy reporting tables are not restored; restore from the pre-release backup")
```

- [ ] **Step 1:** `grep -rn "reporting.contracts\|reporting.facts\|reporting.scheduling\|reporting.tasks\|reporting.aggregation\|reporting.trends\|sdk_reporting\|mcp_reporting\|contracts.reporting\|SyncSchedule\|ReportSyncRun\|ReportFact\|reportTrend" backend/app backend/tests frontend/src` 列出所有引用。
- [ ] **Step 2:** 删除文件、改写引用、新增迁移；`AdDirectoryRun` 的 `frozen_scope_constraints` 等若定义在被删的 `sync_models.py` 依赖链中，确认仍从 `ads/sync_models.py` 导入。
- [ ] **Step 3:** `uv run --frozen ruff check app tests && uv run --frozen ty check app`，Expected: 通过。
- [ ] **Step 4:** 后端全量：`uv run --frozen python ../deploy/ci/backend_tests.py --shards 6 --work /tmp/ci-backend --durations ../deploy/ci/test-durations.json tests`（环境变量同"测试环境"并加 CI 段的 `PROJECT_NAME/CI=1/AWS_*`），Expected: 除已知与报表无关的环境性失败外全部通过；把失败清单与是否相关写入实施进度。
- [ ] **Step 5:** 前端构建通过。
- [ ] **Step 6: 提交** `git commit -m "reporting: 删除旧报表事实、覆盖、调度及 MCP/异步报表路径"`

---

### Task 15: staging 联调与验收（新骏伯 BC）

非代码任务，按 `docs/runbooks/deployment.md` 发布到 staging（发布前备份、核对 `ADS_SYNC_ENABLED`、`TIKTOK_CALL_POLICIES`、新参数实际加载值），并新增 `docs/validation/2026-10-xx-ads-reporting-sync-redesign.md` 记录：

- [ ] 授权的新骏伯 BC（`7683817908149272592`）在开启总开关后无需点刷新即生成 `ad_sync_bc` 计划与任务。
- [ ] 抽 3 个有消耗账户、最近 30 天每天：`SUM(ad_report_day.spend)` = 账户报表消耗；收入差 ≤ 0.01 × 当日广告数；系列逐个一致（用 MCP `tiktok-ads-new-junbo` 只读对照）。
- [ ] 至少一个已删除广告的历史消耗出现在汇总中。
- [ ] `report_sync_state` 无长期 FAILED；`report_sync_job` 每轮请求数与第 3.5 节估算同量级；未出现 40100 导致的失败。
- [ ] 页面六个维度全部可用，所有列排序正确，数据更新时间与缺口提示显示正确。
- [ ] 验证规格第 10 节两项待验证事项（官方 API 通道 BC 报表权限；素材报表 2 万行截断行为），结果写入验收记录与规格。
- [ ] 更新 `docs/implementation-progress.md` 并提交。
