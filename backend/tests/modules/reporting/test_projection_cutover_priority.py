"""初始化历史投影不能挡住新的广告账与素材日期。"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import event, insert, text

from app.modules.reporting.read_models.models import ReadBucketState
from app.modules.reporting.read_models.projector import rebuild_pending


def _seed(session, seed, *, history=1):
    today = date(2026, 11, 11)
    rows = [
        {
            "tenant_id": seed.tenant_id,
            "advertiser_id": "acc-1",
            "kind": "creative_material",
            "bucket": today - timedelta(days=i + 1),
            "source_revision": 1,
            "derived_revision": 0,
            "observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        for i in range(history)
    ]
    rows += [
        {
            "tenant_id": seed.tenant_id,
            "advertiser_id": "acc-1",
            "kind": kind,
            "bucket": today,
            "source_revision": 1,
            "derived_revision": 0,
            "observed_at": datetime(2026, 11, 11, tzinfo=UTC),
        }
        for kind in ["creative_material", "ad_day"]
    ]
    session.execute(insert(ReadBucketState), rows)
    return today


def _done(session, seed, kind, day):
    return session.execute(
        text("""SELECT derived_revision FROM read_bucket_state
      WHERE tenant_id=:t AND advertiser_id='acc-1' AND kind=:k AND bucket=:d"""),
        {"t": seed.tenant_id, "k": kind, "d": day},
    ).scalar_one()


def test_historical_shadow_cannot_block_current_ad_then_current_creative(
    session, sync_seed
):
    today = _seed(session, sync_seed)
    rebuild_pending(session, limit=1)
    assert _done(session, sync_seed, "ad_day", today) == 1
    assert _done(session, sync_seed, "creative_material", today) == 0
    rebuild_pending(session, limit=1)
    assert _done(session, sync_seed, "creative_material", today) == 1
    assert (
        _done(session, sync_seed, "creative_material", today - timedelta(days=1)) == 0
    )


def test_projection_batch_locks_one_kind_and_day_instead_of_mixing_sources(
    session, sync_seed
):
    today = _seed(session, sync_seed)
    assert len(rebuild_pending(session, limit=100)) == 1
    assert _done(session, sync_seed, "ad_day", today) == 1
    assert _done(session, sync_seed, "creative_material", today) == 0
    assert (
        _done(session, sync_seed, "creative_material", today - timedelta(days=1)) == 0
    )


def test_one_current_projection_does_not_scan_thousands_of_historical_buckets(
    session, sync_seed
):
    _seed(session, sync_seed, history=4096)
    captured = []

    def record(_conn, _cursor, statement, parameters, _context, _many):
        if (
            "read_bucket_state" in statement
            and "LIMIT" in statement
            and "FOR UPDATE" in statement
        ):
            captured.append((statement, parameters))

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", record)
    try:
        rebuild_pending(session, limit=1)
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert len(captured) == 1
    statement, params = captured[0]
    plan = connection.exec_driver_sql(
        "EXPLAIN (ANALYZE,FORMAT JSON) " + statement, params
    ).scalar_one()[0]["Plan"]

    def visited(node):
        return node.get("Actual Rows", 0) * node.get("Actual Loops", 1) + sum(
            visited(c) for c in node.get("Plans", [])
        )

    assert visited(plan) <= 100, "one bucket must not walk the historical dirty prefix"
