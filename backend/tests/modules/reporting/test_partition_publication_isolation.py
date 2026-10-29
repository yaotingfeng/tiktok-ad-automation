"""真实 PG：事实发布只准备本表分区，不能持另一父表锁形成交叉等待。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.modules.reporting import publish
from app.modules.reporting.store_models import AdReportDay, MaterialReportDay
from tests.modules.reporting.sync_conftest import ad_row

TABLES = {"ad": "ad_report_day", "material": "material_report_day"}


@pytest.fixture
def missing_month(committed_sync_seed):
    _, factory = committed_sync_seed
    value = uuid4().int
    day = date(3500 + value % 5000, 1 + value % 12, 3)
    with factory() as session:
        for table in TABLES.values():
            assert (
                session.execute(
                    text("SELECT to_regclass(:name)"),
                    {"name": f"{table}_{day:%Y_%m}"},
                ).scalar()
                is None
            )
    try:
        yield day
    finally:
        # 随机月份由本测试独占；DDL与事实测试数据都不留给后续用例。
        with factory() as session, session.begin():
            for table in TABLES.values():
                session.execute(text(f"DROP TABLE IF EXISTS {table}_{day:%Y_%m}"))


def _publish(session, seed, kind, day, account="acc-1"):
    row = (
        ad_row(f"new-{account}", day, adv=account)
        if kind == "ad"
        else {
            "dimensions": {
                "main_material_id": f"new-{account}",
                "main_material_type": "VIDEO",
                "stat_time_day": str(day),
            },
            "metrics": {"advertiser_id": account, "spend": "1"},
        }
    )
    function = publish.publish_ad_day if kind == "ad" else publish.publish_material_day
    return function(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={account: publish.AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[row],
    )


def _default_row(session, seed, kind, day):
    model = AdReportDay if kind == "ad" else MaterialReportDay
    identity = (
        {"ad_id": "existing", "campaign_id": "c", "adgroup_id": "g"}
        if kind == "ad"
        else {"main_material_id": "existing", "main_material_type": "VIDEO"}
    )
    session.add(
        model(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-2",
            stat_date=day,
            currency="USD",
            timezone="UTC",
            fetched_at=datetime.now(UTC),
            sync_job_id=uuid4(),
            spend=Decimal("7.25"),
            **identity,
        )
    )


def _facts(session, seed, kind, day):
    return session.execute(
        text(f"""SELECT advertiser_id,spend,tableoid::regclass::text
          FROM {TABLES[kind]} WHERE tenant_id=:tenant AND stat_date=:day
          ORDER BY advertiser_id"""),
        {"tenant": seed.tenant_id, "day": day},
    ).all()


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_publish_does_not_wait_for_other_fact_table_reader(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    other = TABLES["material" if kind == "ad" else "ad"]
    with factory() as reader, reader.begin():
        reader.execute(text(f"SELECT 1 FROM {other} LIMIT 1"))
        with factory() as writer, writer.begin():
            writer.execute(text("SET LOCAL lock_timeout='300ms'"))
            assert _publish(writer, seed, kind, missing_month) == 1
    with factory() as session:
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), f"{TABLES[kind]}_{missing_month:%Y_%m}")
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_publish_moves_only_its_default_rows(committed_sync_seed, missing_month, kind):
    seed, factory = committed_sync_seed
    other = "material" if kind == "ad" else "ad"
    with factory() as session, session.begin():
        _default_row(session, seed, kind, missing_month)
        _default_row(session, seed, other, missing_month)
    with factory() as session, session.begin():
        assert _publish(session, seed, kind, missing_month) == 1
    with factory() as session:
        partition = f"{TABLES[kind]}_{missing_month:%Y_%m}"
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), partition),
            ("acc-2", Decimal("7.25"), partition),
        ]
        assert _facts(session, seed, other, missing_month) == [
            ("acc-2", Decimal("7.25"), f"{TABLES[other]}_default")
        ]


def test_ad_creation_and_reverse_fact_reader_do_not_deadlock(
    committed_sync_seed, missing_month
):
    seed, factory = committed_sync_seed
    reader_ready, ad_locked, reader_continuing = Event(), Event(), Event()

    def read_reverse():
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            session.execute(text("SELECT 1 FROM material_report_day LIMIT 1"))
            reader_ready.set()
            assert ad_locked.wait(3)
            reader_continuing.set()
            # 与建分区的ad→material顺序相反，旧双表helper会出现真实PG锁环。
            return _facts(session, seed, "ad", missing_month)

    def create_ad():
        assert reader_ready.wait(3)
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))

            def after_lock(_conn, _cursor, statement, *_):
                if statement == "LOCK TABLE ad_report_day IN ACCESS EXCLUSIVE MODE":
                    ad_locked.set()
                    assert reader_continuing.wait(3)

            connection = session.connection()
            event.listen(connection, "after_cursor_execute", after_lock)
            try:
                return _publish(session, seed, "ad", missing_month)
            finally:
                event.remove(connection, "after_cursor_execute", after_lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read_reverse)
        writer = pool.submit(create_ad)
        assert writer.result(timeout=10) == 1
        assert reader.result(timeout=10) == [
            ("acc-1", Decimal("1"), f"ad_report_day_{missing_month:%Y_%m}")
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_same_month_concurrent_publish_creates_once_without_losing_rows(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    both_missing = Barrier(2)

    def write(account):
        with factory() as session, session.begin():
            session.execute(text("SET LOCAL statement_timeout='5s'"))
            reached = False

            def before_lock(_conn, _cursor, statement, *_):
                nonlocal reached
                if statement.startswith("LOCK TABLE") and not reached:
                    reached = True
                    both_missing.wait(timeout=3)

            connection = session.connection()
            event.listen(connection, "before_cursor_execute", before_lock)
            try:
                return _publish(session, seed, kind, missing_month, account)
            finally:
                event.remove(connection, "before_cursor_execute", before_lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(write, account) for account in ("acc-1", "acc-2")]
        assert [result.result(timeout=10) for result in results] == [1, 1]
    with factory() as session:
        partition = f"{TABLES[kind]}_{missing_month:%Y_%m}"
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-1", Decimal("1"), partition),
            ("acc-2", Decimal("1"), partition),
        ]


@pytest.mark.parametrize("kind", ["ad", "material"])
def test_partition_and_publication_rollback_then_retry(
    committed_sync_seed, missing_month, kind
):
    seed, factory = committed_sync_seed
    with factory() as session, session.begin():
        _default_row(session, seed, kind, missing_month)
    with pytest.raises(DBAPIError) as caught:
        with factory() as session, session.begin():
            assert _publish(session, seed, kind, missing_month) == 1
            session.execute(text("SELECT 1/0"))
    assert caught.value.orig.sqlstate == "22012"
    with factory() as session:
        assert _facts(session, seed, kind, missing_month) == [
            ("acc-2", Decimal("7.25"), f"{TABLES[kind]}_default")
        ]
        assert (
            session.execute(
                text("SELECT to_regclass(:name)"),
                {"name": f"{TABLES[kind]}_{missing_month:%Y_%m}"},
            ).scalar()
            is None
        )
    with factory() as session, session.begin():
        assert _publish(session, seed, kind, missing_month) == 1
    with factory() as session:
        assert len(_facts(session, seed, kind, missing_month)) == 2
