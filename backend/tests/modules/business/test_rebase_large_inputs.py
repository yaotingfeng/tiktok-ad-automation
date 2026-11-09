"""约33MB/185k真实状态的VERIFY全链耗时和峰值内存，不调用外部平台。"""

import gc
import resource
import sys
from datetime import UTC, date, datetime, timedelta
from time import monotonic

import pytest
from sqlalchemy import inspect, text
from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from app.modules.business import projection
from app.modules.business.projection import advance_run, request_rebuild
from app.modules.reporting.query_budget import query_budget


# 本profile明确提交独立DB的fixture事务；teardown重复rollback仅产生已知SAWarning。
@pytest.mark.filterwarnings(
    "ignore:transaction already deassociated from connection:sqlalchemy.exc.SAWarning"
)
def test_large_verify_decodes_compares_and_persists_within_heavy_budget(
    session, business_case, monkeypatch
):
    c = business_case
    params = {
        "tenant": c.context.tenant_id,
        "original": c.advertiser_id,
        "bc": c.bc_id,
        "actor": c.context.actor_id,
    }
    # SQL复制本地fixture授权，避免构造近20万ORM对象污染被测RSS。
    for model in (AdvertiserAccount, BCAccountAccess):
        table = inspect(model).local_table
        columns = [column.name for column in table.columns]
        values = [
            "'10000000000000'||lpad(g::text,5,'0')"
            if col == "advertiser_id"
            else "a." + col
            for col in columns
        ]
        session.execute(
            text(f"""INSERT INTO {table.name} ({",".join(columns)})
            SELECT {",".join(values)} FROM {table.name} a CROSS JOIN generate_series(0,4858) g
            WHERE a.tenant_id=:tenant AND a.advertiser_id=:original"""),
            params,
        )
    session.execute(
        text("""INSERT INTO business_member
        (tenant_id,bc_id,advertiser_id,start_date,end_date)
        SELECT :tenant,:bc,'10000000000000'||lpad(g::text,5,'0'),'2026-10-01',NULL
        FROM generate_series(0,4858) g"""),
        params,
    )
    session.execute(
        text("""INSERT INTO report_sync_state
        (tenant_id,advertiser_id,report_kind,stat_date,status,row_count,
         last_success_at,observed_at,publication_version,updated_at)
        SELECT :tenant,'10000000000000'||lpad(g::text,5,'0'),'ad_day',d::date,
               'COMPLETE',g%999,'2026-11-08 12:00:00.123456+00','2026-11-08 12:00:00.123456+00',
               g+1,'2026-11-08 12:00:00.123456+00'
        FROM generate_series(0,4858) g CROSS JOIN
             generate_series('2026-10-01'::date,'2026-11-07'::date,'1 day') d"""),
        params,
    )

    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    run = request_rebuild(
        session,
        context=c.context,
        bc_id=c.bc_id,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 7),
        now=now,
        defer_snapshot=True,
        coalesce_pending=True,
    )[0]
    with query_budget(session, seconds=18):
        assert not advance_run(session, run.id, now=now)
    size = session.execute(
        text("SELECT octet_length(inputs::text) FROM business_run WHERE id=:id"),
        {"id": run.id},
    ).scalar_one()
    assert 33_000_000 <= size <= 35_000_000
    run.phase, run.cursor = "VERIFY", run.end_date + timedelta(days=1)
    session.add(run)
    session.execute(
        text("""UPDATE report_sync_state SET publication_version=publication_version+1,
        updated_at=updated_at+interval '1 second',observed_at=observed_at+interval '1 second'
        WHERE tenant_id=:tenant AND report_kind='ad_day' AND stat_date='2026-11-07'
          AND advertiser_id BETWEEN '1000000000000000000' AND '1000000000000000007'"""),
        params,
    )
    run_id = run.id
    connection = session.get_bind()
    session.commit()
    # 独立测试DB由wrapper销毁；这里真正提交seed，profile在独立物理事务读取旧JSON。
    connection.commit()
    session.close()
    del run
    gc.collect()
    timings = {}
    original_source = projection.source_snapshot
    original_diff = projection.rebase_dirty_start
    original_refresh = Session.refresh

    def source(*args, **kwargs):
        started = monotonic()
        result = original_source(*args, **kwargs)
        timings["current_snapshot_s"] = monotonic() - started
        return result

    def compare(*args, **kwargs):
        started = monotonic()
        result = original_diff(*args, **kwargs)
        timings["exact_diff_s"] = monotonic() - started
        return result

    def refresh(self, *args, **kwargs):
        started = monotonic()
        result = original_refresh(self, *args, **kwargs)
        if kwargs.get("attribute_names") == ["inputs"]:
            timings["old_json_load_s"] = monotonic() - started
        return result

    monkeypatch.setattr(projection, "source_snapshot", source)
    monkeypatch.setattr(projection, "rebase_dirty_start", compare)
    monkeypatch.setattr(Session, "refresh", refresh)
    started = monotonic()
    with Session(engine) as local:
        with query_budget(local, seconds=18):
            assert not advance_run(local, run_id, now=now)
        local.commit()
    elapsed = monotonic() - started
    with Session(engine) as local:
        phase, progress = local.execute(
            text("SELECT phase,coverage->'_rebase' FROM business_run WHERE id=:id"),
            {"id": run_id},
        ).one()
        assert phase == "REBASE" and progress["dirty"] == "2026-11-07"
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    print(  # noqa: T201 - 本地性能证据
        f"VERIFY_BENCH seconds={elapsed:.3f} json_bytes={size} peak_rss_bytes={peak} stages={timings}"
    )
    assert elapsed < 18
    assert peak < 2 * 1024 * 1024 * 1024
    # 同一大JSON再走稳定核验/发布路径；本fixture只构造来源状态，完整事实等价另测。
    with Session(engine) as local:
        local.execute(
            text("UPDATE business_run SET phase='VERIFY' WHERE id=:id"), {"id": run_id}
        )
        local.commit()
    timings.clear()
    started = monotonic()
    with Session(engine) as local:
        with query_budget(local, seconds=18):
            assert advance_run(local, run_id, now=now)
        local.commit()
    elapsed = monotonic() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024
    print(  # noqa: T201 - 本地性能证据
        f"VERIFY_STABLE_BENCH seconds={elapsed:.3f} peak_rss_bytes={peak} stages={timings}"
    )
    assert elapsed < 18
    assert peak < 2 * 1024 * 1024 * 1024
