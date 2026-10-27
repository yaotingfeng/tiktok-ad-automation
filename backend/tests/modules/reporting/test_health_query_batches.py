"""健康扫描必须完整覆盖所有批次，任何一批失败都不能返回局部成功。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, insert

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting import planner
from app.modules.reporting.health import measure_health
from app.modules.reporting.store_models import AdReportDay, AdSyncBc, MaterialReportDay


@pytest.mark.parametrize("kind", ["ad", "material"])
@pytest.mark.parametrize("fail_second_batch", [False, True])
def test_health_batches_preserve_complete_scope(
    session, sync_seed, kind, fail_second_batch
):
    now = datetime(2026, 10, 12, 12, tzinfo=UTC)
    planner.ensure_bc_plans(session, now=now)
    plan = session.get(AdSyncBc, (sync_seed.tenant_id, "bc-a"))
    accounts = [f"health-{i:04d}" for i in range(513)]
    session.execute(
        insert(AdvertiserAccount),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": account,
                "timezone": "UTC",
            }
            for account in accounts
        ],
    )
    session.execute(
        insert(AdReportDay if kind == "ad" else MaterialReportDay),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": account,
                "stat_date": now.date(),
                "currency": "USD",
                "timezone": "UTC",
                "fetched_at": now,
                "sync_job_id": uuid4(),
                "spend": 1,
                **(
                    {"ad_id": "ad", "adgroup_id": "group", "campaign_id": "campaign"}
                    if kind == "ad"
                    else {"main_material_id": "post", "main_material_type": "VIDEO"}
                ),
            }
            for account in accounts
        ],
    )
    plan.active_screened_at = now
    queries = []
    target = (
        "SELECT r.advertiser_id,r.ad_id,r.adgroup_id,r.campaign_id"
        if kind == "ad"
        else "SELECT DISTINCT r.advertiser_id,r.main_material_id"
    )

    def capture(_conn, _cursor, sql, params, _context, _many):
        if target in sql:
            queries.append(params)
            if fail_second_batch and len(queries) == 2:
                raise RuntimeError("second batch failed")

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        if fail_second_batch:
            with pytest.raises(RuntimeError, match="second batch failed"):
                measure_health(session, plan, now=now, accounts=accounts)
        else:
            metrics, _, repairs = measure_health(
                session, plan, now=now, accounts=accounts
            )
            assert metrics[
                "missing_directory_ads" if kind == "ad" else "unlinked_material_posts"
            ] == len(accounts)
            assert repairs["ad" if kind == "ad" else "links"] == set(accounts)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert len(queries) == (2 if fail_second_batch else 3)
    assert all(len(params["ids"]) <= 256 for params in queries)
