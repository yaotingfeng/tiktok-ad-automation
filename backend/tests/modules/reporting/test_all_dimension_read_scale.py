"""显式启用的六维生产量级合成基准；输出分阶段耗时，不用机器相关时间门限。

ALL_DIMENSION_READ_SCALE=production pytest -s .../test_all_dimension_read_scale.py
首日100k层级实体，随后6日每天10k、余23日每天5k；5000账户、200剧和100跨户VID。
所有数据都在测试租户和测试事务中，真实源触发器保持启用，写入也单独计时。
"""

import hashlib
import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import text

from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import METRIC_COLUMNS

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("ALL_DIMENSION_READ_SCALE") != "production",
        reason="explicit production-shape benchmark",
    ),
]
END = date(2026, 10, 31)
ACCOUNTS = 5000
ENTITIES = 100_000
HISTORICAL_ACTIVE = 10_000
LONG_WINDOW_ACTIVE = 5_000
MATERIAL_USES = 10_000
VIDEOS = 100
DRAMAS = 200


def _record(stage, **values):
    sys.stdout.write(
        "READ_SCALE " + json.dumps({"stage": stage, **values}, sort_keys=True) + "\n"
    )
    sys.stdout.flush()


def _write(session, seed, label, sql, **params):
    started = perf_counter()
    result = session.execute(
        text(sql), {"t": seed.tenant_id, "connection": seed.connection.id, **params}
    )
    session.commit()
    _record(
        "write",
        label=label,
        seconds=round(perf_counter() - started, 4),
        affected=result.rowcount,
    )


def _seed_directory(session, seed):
    _write(
        session,
        seed,
        "5000 accounts",
        """INSERT INTO advertiser_account
        (tenant_id,advertiser_id,name,currency,timezone,remote_status,ownership_conflict)
        SELECT :t,'scale-'||i,'合成账户'||i,'USD','UTC','ENABLE',false FROM generate_series(1,5000)i""",
    )
    _write(
        session,
        seed,
        "5000 grants",
        """INSERT INTO bc_account_access
        (tenant_id,bc_id,advertiser_id,connection_id,in_bc,authorized,active,can_upload,can_build,permission_state,checked_at)
        SELECT :t,'bc-a','scale-'||i,:connection,true,true,true,true,true,'VERIFIED',now() FROM generate_series(1,5000)i""",
    )
    for kind, prefix, parent_kind, parent_prefix in (
        ("campaign", "c", None, None),
        ("adgroup", "g", "campaign", "c"),
        ("ad", "a", "adgroup", "g"),
    ):
        _write(
            session,
            seed,
            f"100k {kind} directory",
            """INSERT INTO ad_object
            (tenant_id,advertiser_id,kind,remote_id,parent_kind,parent_remote_id,ad_type,name,
             operation_status,review_status,configuration,observed_at,published_version)
            SELECT :t,'scale-'||((i-1)/20+1),:kind,:prefix||i,:parent_kind,
                CASE WHEN CAST(:parent_prefix AS text) IS NULL THEN NULL ELSE CAST(:parent_prefix AS text)||i END,
                'SMART_PLUS',:prefix||'-'||i,'ENABLE','APPROVED','{}',now(),1 FROM generate_series(1,100000)i""",
            kind=kind,
            prefix=prefix,
            parent_kind=parent_kind,
            parent_prefix=parent_prefix,
        )
    _write(
        session,
        seed,
        "100k names / 200 dramas",
        """INSERT INTO campaign_name_projection
        (tenant_id,advertiser_id,campaign_remote_id,name_revision,raw_name,provider_label,drama_name,status,
         parser_revision,grouping_revision,observed_at)
        SELECT :t,'scale-'||((i-1)/20+1),'c'||i,1,'合成版权-剧'||((i-1)%200+1),'合成版权','剧'||((i-1)%200+1),
            'VALID',1,1,now() FROM generate_series(1,100000)i""",
    )
    _write(
        session,
        seed,
        "10k material uses / 100 videos",
        """INSERT INTO ad_material_reference
        (id,tenant_id,advertiser_id,ad_remote_id,platform_material_id,ad_material_id,material_type,
         name,creative_ids,complete,published_version)
        SELECT gen_random_uuid(),:t,'scale-'||((i-1)/20+1),'a'||i,'scale-video-'||((i-1)%100+1),
            'asset-'||i,'VIDEO','合成视频'||((i-1)%100+1),'[]',true,1 FROM generate_series(1,10000)i""",
    )


def _seed_days(session, seed, first, last, count):
    # 每个对象每天1美元；上级一对一，六维汇总有直接可核验的解析期望。
    metric_names = ",".join(METRIC_COLUMNS)
    metric_values = ",".join(
        "1" if m == "spend" else "2" if m.startswith("revenue") else "10"
        for m in METRIC_COLUMNS
    )
    flags = ",".join(f"{m}_has_null" for m in METRIC_COLUMNS)
    false_flags = ",".join("false" for _ in METRIC_COLUMNS)
    base_fields = (
        f"tenant_id,advertiser_id,stat_date,currency,timezone,fetched_at,{metric_names}"
    )
    base_values = f":t,'scale-'||((i-1)/20+1),CAST(:end AS date)-d,'USD','UTC',now(),{metric_values}"
    params = {"first": first, "last": last, "end": END, "n": count}
    series = " FROM generate_series(1,:n)i CROSS JOIN generate_series(CAST(:first AS integer),CAST(:last AS integer))d"
    _write(
        session,
        seed,
        f"campaign days {first}-{last}",
        f"""INSERT INTO campaign_report_day
        ({base_fields},campaign_id,campaign_name,sync_job_id)
        SELECT {base_values},'c'||i,repeat('历史系列名称-',10)||i,gen_random_uuid(){series}""",
        **params,
    )
    _write(
        session,
        seed,
        f"adgroup days {first}-{last}",
        f"""INSERT INTO adgroup_report_day
        ({base_fields},{flags},adgroup_id,campaign_id,campaign_name,adgroup_name,source_revision)
        SELECT {base_values},{false_flags},'g'||i,'c'||i,repeat('历史系列名称-',10)||i,
            repeat('历史广告组名称-',10)||i,1{series}""",
        **params,
    )
    _write(
        session,
        seed,
        f"ad days {first}-{last}",
        f"""INSERT INTO ad_report_day
        ({base_fields},ad_id,campaign_id,adgroup_id,campaign_name,adgroup_name,ad_name,sync_job_id)
        SELECT {base_values},'a'||i,'c'||i,'g'||i,repeat('历史系列名称-',10)||i,
            repeat('历史广告组名称-',10)||i,repeat('历史广告名称-',10)||i,gen_random_uuid(){series}""",
        **params,
    )
    _write(
        session,
        seed,
        f"material days {first}-{last}",
        f"""INSERT INTO material_entity_day
        ({base_fields},{flags},row_key,main_material_type,main_material_name,post_ids,post_names,source_revision)
        SELECT {base_values},{false_flags},json_build_array('material','scale-video-'||((i-1)%100+1))::text,'VIDEO',
            '合成视频'||((i-1)%100+1),ARRAY['post-'||i],jsonb_build_object('post-'||i,'合成视频'||((i-1)%100+1)),1
        {series}""",
        **{**params, "n": min(MATERIAL_USES, count)},
    )
    _write(
        session,
        seed,
        f"coverage days {first}-{last}",
        """INSERT INTO report_sync_state
        (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,last_success_at,observed_at,publication_version,updated_at)
        SELECT :t,'scale-'||i,k,CAST(:end AS date)-d,'COMPLETE',20,now(),now(),1,now()
        FROM generate_series(1,5000)i CROSS JOIN generate_series(CAST(:first AS integer),CAST(:last AS integer))d
        CROSS JOIN unnest(ARRAY['ad_day','material_day'])k""",
        **params,
    )
    _write(
        session,
        seed,
        f"derived coverage days {first}-{last}",
        """INSERT INTO read_bucket_state
        (tenant_id,advertiser_id,kind,bucket,source_revision,derived_revision,observed_at,derived_observed_at,derived_status)
        SELECT :t,'scale-'||i,'material_day',CAST(:end AS date)-d,1,1,now(),now(),'COMPLETE'
        FROM generate_series(1,5000)i CROSS JOIN generate_series(CAST(:first AS integer),CAST(:last AS integer))d
        ON CONFLICT(tenant_id,advertiser_id,kind,bucket) DO UPDATE
            SET derived_revision=read_bucket_state.source_revision,derived_status='COMPLETE',derived_observed_at=now()""",
        **params,
    )


def _analyze(session):
    for table in (
        "advertiser_account",
        "bc_account_access",
        "ad_object",
        "ad_entity_read",
        "campaign_name_current",
        "account_report_day",
        "campaign_report_day",
        "adgroup_report_day",
        "ad_report_day",
        "material_entity_day",
        "entity_metric_day",
        "report_sync_state",
        "read_bucket_state",
        "current_ad_material",
        "material_account_member_current",
        "material_ad_member_current",
    ):
        session.execute(text(f"ANALYZE {table}"))


def _measure(
    session,
    seed,
    dimension,
    days,
    advertisers,
    *,
    changes=None,
    case="default",
    expected_total=None,
    expected_spend_override=None,
):
    from app.modules.reporting.queries import _build_snapshot, _read_page

    session.execute(
        text("SET LOCAL jit=off")
    )  # 与生产snapshot_transaction的局部设置一致。
    filters = ReportingFilter(
        **(
            {
                "dimension": dimension,
                "start_date": END - timedelta(days=days - 1),
                "end_date": END,
                "advertiser_ids": advertisers,
                "sort_by": "spend",
                "sort_direction": "desc",
            }
            | (changes or {})
        )
    )
    _record("start", dimension=dimension, days=days, case=case)
    started = perf_counter()
    snapshot = _build_snapshot(
        session, context=seed.context, bc_id="bc-a", filters=filters
    )
    built = perf_counter()
    page = _read_page(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=filters,
        snapshot=snapshot,
        cursor=None,
        limit=50,
    )
    read = perf_counter()
    payload = page.model_dump_json()
    serialized = perf_counter()
    expected_count = {
        "account": ACCOUNTS,
        "campaign": ENTITIES,
        "adgroup": ENTITIES,
        "ad": ENTITIES,
        "drama": DRAMAS,
        "material": VIDEOS,
    }[dimension]
    expected_spend = (
        MATERIAL_USES * min(days, 7) + LONG_WINDOW_ACTIVE * max(days - 7, 0)
        if dimension == "material"
        else ENTITIES
        + HISTORICAL_ACTIVE * min(days - 1, 6)
        + LONG_WINDOW_ACTIVE * max(days - 7, 0)
    )
    if expected_total is not None:
        expected_count = expected_total
    if expected_spend_override is not None:
        expected_spend = expected_spend_override
    _record(
        "query",
        case=case,
        dimension=dimension,
        days=days,
        snapshot_seconds=round(built - started, 4),
        page_seconds=round(read - built, 4),
        json_seconds=round(serialized - read, 4),
        json_bytes=len(payload.encode()),
        total=page.total,
        items=len(page.items),
        summary_spend=page.summary["buckets"][0]["values"]["spend"],
        returned_refs=sum(len(item.refs) for item in page.items),
        returned_uses=sum(len(item.material_uses) for item in page.items),
    )
    assert page.total == expected_count
    assert len(page.items) == min(50, expected_count)
    assert Decimal(page.summary["buckets"][0]["values"]["spend"]) == expected_spend
    assert len({item.row_key for item in page.items}) == len(page.items)
    assert all(
        bucket.values["spend"] is not None
        for item in page.items
        for bucket in item.metric_buckets
    )
    if case == "default":
        expected_row_spend = (
            expected_spend // VIDEOS
            if dimension == "material"
            else expected_spend // DRAMAS
            if dimension == "drama"
            else days * 20
            if dimension == "account"
            else days
        )
        assert all(
            bucket.values["spend"] == expected_row_spend
            for item in page.items
            for bucket in item.metric_buckets
        )
    session.delete(snapshot)
    session.commit()


@pytest.fixture
def scale_case():
    from sqlmodel import Session, SQLModel

    from app.core.db import engine
    from tests.modules.reporting.sync_conftest import sync_seed as make_seed

    with Session(engine, expire_on_commit=False) as db:
        seed = make_seed.__wrapped__(db)
        db.commit()
        try:
            yield db, seed
        finally:
            db.rollback()
            # 每表独立提交释放触发器advisory locks，不占共享测试PG的整批锁预算。
            canonical = (
                "campaign_report_day",
                "ad_report_day",
                "material_report_day",
                "ad_object",
                "campaign_name_projection",
                "ad_material_reference",
            )
            for name in canonical:
                table = SQLModel.metadata.tables[name]
                db.execute(table.delete().where(table.c.tenant_id == seed.tenant_id))
                db.commit()
            for table in reversed(SQLModel.metadata.sorted_tables):
                if "tenant_id" in table.c and table.name not in canonical:
                    db.execute(
                        table.delete().where(table.c.tenant_id == seed.tenant_id)
                    )
                    db.commit()
            for table_name, value in (
                ("tenant", seed.tenant_id),
                ("user", seed.actor_id),
            ):
                table = SQLModel.metadata.tables[table_name]
                db.execute(table.delete().where(table.c.id == value))
                db.commit()


def test_all_dimension_production_shape(scale_case):
    session, sync_seed = scale_case
    from app.modules.reporting.partitions import ensure_month_partitions_for_range

    runtime = Path(__file__).parents[3] / "app/modules/reporting"
    _record(
        "configuration",
        accounts=ACCOUNTS,
        entities_per_level=ENTITIES,
        historical_daily_active=HISTORICAL_ACTIVE,
        days_after_seven_active=LONG_WINDOW_ACTIVE,
        material_uses=MATERIAL_USES,
        videos=VIDEOS,
        dramas=DRAMAS,
        runtime_hashes={
            name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()[:12]
            for name in (
                "row_set.py",
                "queries.py",
                "dimension_query.py",
                "material_query.py",
                "snapshot_members.py",
            )
        },
    )
    session.execute(text("SET statement_timeout='240s'"))
    ensure_month_partitions_for_range(session, END - timedelta(days=29), END)
    _seed_directory(session, sync_seed)
    advertisers = tuple(f"scale-{i}" for i in range(1, ACCOUNTS + 1))
    previous = 0
    periods = tuple(
        int(v) for v in os.environ.get("READ_SCALE_PERIODS", "1,7,30").split(",")
    )
    dimensions = os.environ.get(
        "READ_SCALE_DIMENSIONS", "account,campaign,adgroup,ad,drama,material"
    ).split(",")
    for days in periods:
        if previous == 0:
            _seed_days(session, sync_seed, 0, 0, ENTITIES)
            previous = 1
        if min(days, 7) > previous:
            _seed_days(
                session, sync_seed, previous, min(days, 7) - 1, HISTORICAL_ACTIVE
            )
        if days > max(previous, 7):
            _seed_days(
                session, sync_seed, max(previous, 7), days - 1, LONG_WINDOW_ACTIVE
            )
        previous = days
        _analyze(session)
        for dimension in dimensions:
            _measure(session, sync_seed, dimension, days, advertisers)
    measure_representative_filters(session, sync_seed, advertisers)


def measure_representative_filters(session, seed, advertisers):
    for dimension, case, changes, count, spend in (
        (
            "campaign",
            "operation-status",
            {"operation_statuses": ("ENABLE",)},
            ENTITIES,
            ENTITIES,
        ),
        ("campaign", "exact-id", {"ids": ("c1",)}, 1, 1),
        ("adgroup", "parent-id", {"ids": ("c1",)}, 1, 1),
        ("ad", "parent-id", {"ids": ("g1",)}, 1, 1),
        ("drama", "query", {"query": "剧199"}, 1, ENTITIES // DRAMAS),
        ("material", "query", {"query": "scale-video-99"}, 1, MATERIAL_USES // VIDEOS),
        ("material", "ad-id", {"ids": ("a1",)}, 1, MATERIAL_USES // VIDEOS),
    ):
        _measure(
            session,
            seed,
            dimension,
            1,
            advertisers,
            changes=changes,
            case=case,
            expected_total=count,
            expected_spend_override=spend,
        )
