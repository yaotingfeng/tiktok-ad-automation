from datetime import UTC, date, datetime, timedelta

from app.modules.accounts.models import AdvertiserAccount
from app.modules.reporting.api import complete_data_start
from app.modules.reporting.store_models import ReportSyncState

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)


def _account(session, tenant_id, advertiser_id, opened: date | None):
    account = session.get(AdvertiserAccount, (tenant_id, advertiser_id))
    account.remote_created_at = (
        datetime.combine(opened, datetime.min.time(), UTC) if opened else None
    )
    session.add(account)
    return account


def _complete(session, tenant_id, advertiser_id, start: date, end: date):
    day = start
    while day <= end:
        session.add(
            ReportSyncState(
                tenant_id=tenant_id,
                advertiser_id=advertiser_id,
                report_kind="ad_day",
                stat_date=day,
                status="COMPLETE",
                last_success_at=NOW,
            )
        )
        day += timedelta(days=1)


def _accounts(session, sync_seed):
    return [
        session.get(AdvertiserAccount, (sync_seed.tenant_id, adv))
        for adv in ("acc-1", "acc-2")
    ]


def test_bounded_by_latest_gap_and_ignores_days_before_opening(session, sync_seed):
    tenant = sync_seed.tenant_id
    _account(session, tenant, "acc-1", date(2026, 9, 1))
    _account(session, tenant, "acc-2", date(2026, 9, 20))
    # acc-1 缺 9/4，之后连续完整到账户本地昨天 10/12。
    _complete(session, tenant, "acc-1", date(2026, 9, 1), date(2026, 9, 3))
    _complete(session, tenant, "acc-1", date(2026, 9, 5), date(2026, 10, 12))
    # acc-2 9/20 才开户，开户前没有记录不应把起始日拖到 9/20。
    _complete(session, tenant, "acc-2", date(2026, 9, 20), date(2026, 10, 12))
    session.flush()
    accounts = _accounts(session, sync_seed)
    assert complete_data_start(session, tenant, accounts, NOW) == date(2026, 9, 5)


def test_fully_synced_since_opening_starts_at_earliest_opening(session, sync_seed):
    tenant = sync_seed.tenant_id
    _account(session, tenant, "acc-1", date(2026, 9, 1))
    _account(session, tenant, "acc-2", date(2026, 9, 20))
    _complete(session, tenant, "acc-1", date(2026, 9, 1), date(2026, 10, 12))
    _complete(session, tenant, "acc-2", date(2026, 9, 20), date(2026, 10, 12))
    session.flush()
    accounts = _accounts(session, sync_seed)
    assert complete_data_start(session, tenant, accounts, NOW) == date(2026, 9, 1)


def test_unknown_opening_date_is_bounded_by_its_own_run(session, sync_seed):
    tenant = sync_seed.tenant_id
    _account(session, tenant, "acc-1", date(2026, 9, 1))
    _account(session, tenant, "acc-2", None)
    _complete(session, tenant, "acc-1", date(2026, 9, 1), date(2026, 10, 12))
    _complete(session, tenant, "acc-2", date(2026, 9, 12), date(2026, 10, 12))
    session.flush()
    accounts = _accounts(session, sync_seed)
    assert complete_data_start(session, tenant, accounts, NOW) == date(2026, 9, 12)


def test_yesterday_missing_means_no_complete_range(session, sync_seed):
    tenant = sync_seed.tenant_id
    _account(session, tenant, "acc-1", date(2026, 9, 1))
    _account(session, tenant, "acc-2", date(2026, 9, 1))
    _complete(session, tenant, "acc-1", date(2026, 9, 1), date(2026, 10, 12))
    _complete(session, tenant, "acc-2", date(2026, 9, 1), date(2026, 10, 11))
    session.flush()
    accounts = _accounts(session, sync_seed)
    assert complete_data_start(session, tenant, accounts, NOW) is None


def test_status_route_exposes_complete_from(client, session, sync_seed):
    from app.api.deps import get_current_user
    from app.models import User

    client.app.dependency_overrides[get_current_user] = lambda: session.get(
        User, sync_seed.actor_id
    )
    try:
        response = client.get(
            f"/api/tenants/{sync_seed.tenant_id}/ad-sync-status",
            params={
                "bc_id": "bc-a",
                "start_date": "2026-10-05",
                "end_date": "2026-10-05",
            },
        )
    finally:
        client.app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 200, response.text
    # 种子数据没有任何同步记录，昨天也不完整，因此没有完整区间。
    assert response.json()["complete_from"] is None
