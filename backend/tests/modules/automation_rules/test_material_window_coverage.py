"""整日双源空缺也必须阻断素材动作；有完整空报表才可视为零。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.modules.accounts.models import AdvertiserAccount
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_creative_day,
)
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.automation_rules.test_inputs import NOW
from tests.modules.automation_rules.test_material_inputs import post, rule, setup, use


def _empty_days(session, seed, start, end, timezone):
    args = {
        "tenant_id": seed.tenant_id,
        "job_id": uuid4(),
        "accounts": {"acc-1": AccountMeta("USD", timezone)},
        "start": start,
        "end": end,
        "rows": [],
    }
    publish_ad_day(session, **args)
    publish_creative_day(session, **args)
    session.execute(
        text("""UPDATE report_sync_state SET observed_at=:now,last_success_at=:now
        WHERE tenant_id=:tenant AND advertiser_id='acc-1' AND stat_date BETWEEN :start AND :end
          AND report_kind IN ('ad_day','creative_day')"""),
        {"tenant": seed.tenant_id, "start": start, "end": end, "now": NOW},
    )


def _case(session, seed, *, timezone="Asia/Hong_Kong", days=7):
    setup(session, seed)
    account = session.get(AdvertiserAccount, (seed.tenant_id, "acc-1"))
    account.timezone = timezone
    account.remote_created_at = NOW - timedelta(days=days - 1)
    today = NOW.astimezone(ZoneInfo(timezone)).date()
    _empty_days(session, seed, today - timedelta(days=days - 1), today, timezone)
    use(session, seed, "a1")
    post(session, seed, "a1", "creative-1", day=today)
    session.execute(
        text("""UPDATE ad_report_day SET timezone=:timezone
        WHERE tenant_id=:tenant AND advertiser_id='acc-1'"""),
        {"tenant": seed.tenant_id, "timezone": timezone},
    )
    session.execute(
        text("""UPDATE creative_report_day SET timezone=:timezone
        WHERE tenant_id=:tenant AND advertiser_id='acc-1'"""),
        {"tenant": seed.tenant_id, "timezone": timezone},
    )
    session.flush()
    return today


def _evaluate(session, seed, window="PAST_7_DAYS"):
    definition = rule()
    definition = definition.model_copy(
        update={
            "conditions": tuple(
                c.model_copy(update={"range": window}) for c in definition.conditions
            )
        }
    )
    loaded = load_inputs(
        session, tenant_id=seed.tenant_id, definition=definition, now=NOW
    )
    return loaded, evaluate(definition, loaded)


def _blocked(loaded, found):
    materials = [e for e in loaded.entities.values() if e.kind == "material"]
    assert materials and all(
        e.judgement_issue == "MATERIAL_SOURCE_INCOMPLETE" for e in materials
    )
    assert not found.hits
    assert {r["reason"] for r in loaded.skipped_accounts} == {
        "MATERIAL_SOURCE_INCOMPLETE"
    }


@pytest.mark.parametrize(
    "problem",
    [
        "both_missing",
        "ad_missing",
        "creative_missing",
        "ad_failed",
        "creative_stale",
        "ad_null_observation",
        "creative_null_observation",
        "skew",
    ],
)
def test_empty_historical_day_requires_paired_complete_observations(
    session, sync_seed, problem
):
    today = _case(session, sync_seed)
    missing = today - timedelta(days=3)
    for kind in ("ad_day", "creative_day"):
        state = session.get(
            ReportSyncState, (sync_seed.tenant_id, "acc-1", kind, missing)
        )
        if (
            problem == "both_missing"
            or problem == kind.removesuffix("_day") + "_missing"
        ):
            session.delete(state)
        elif problem == "ad_failed" and kind == "ad_day":
            state.status = "FAILED"
        elif problem == "creative_stale" and kind == "creative_day":
            state.status = "STALE"
        elif problem == kind.removesuffix("_day") + "_null_observation":
            state.observed_at = None
        elif problem == "skew" and kind == "creative_day":
            state.observed_at = NOW - timedelta(seconds=1801)
    session.flush()
    _blocked(*_evaluate(session, sync_seed))


@pytest.mark.parametrize("timezone", ["Asia/Hong_Kong", "America/Los_Angeles"])
def test_complete_empty_days_are_valid_zero_in_account_local_window(
    session, sync_seed, timezone
):
    today = _case(session, sync_seed, timezone=timezone)
    state = session.get(
        ReportSyncState,
        (sync_seed.tenant_id, "acc-1", "creative_day", today - timedelta(days=3)),
    )
    state.observed_at = NOW - timedelta(seconds=1800)
    session.flush()
    loaded, found = _evaluate(session, sync_seed)
    assert not loaded.skipped_accounts
    assert len(found.hits) == 1
    assert found.hits[0].metrics["spend:PAST_7_DAYS"] == "30"
    assert found.hits[0].metrics["roas_total:PAST_7_DAYS"] == "0.5"


@pytest.mark.parametrize("window", ["LIFETIME", "PAST_7_DAYS"])
def test_window_uses_official_local_creation_but_never_first_fact(
    session, sync_seed, window
):
    today = _case(session, sync_seed, days=1)
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    # UTC昨天的创建时刻属于香港今天；不能虚构创建前的缺口。
    account.remote_created_at = datetime(2026, 10, 13, 18, tzinfo=UTC)
    session.flush()
    loaded, found = _evaluate(session, sync_seed, window)
    assert not loaded.skipped_accounts and len(found.hits) == 1
    # 官方创建时间证明还有更早历史；不能用第一条事实/水位截断它。
    account.remote_created_at = NOW - timedelta(days=2)
    _empty_days(
        session,
        sync_seed,
        today - timedelta(days=1),
        today - timedelta(days=1),
        account.timezone,
    )
    session.flush()
    _blocked(*_evaluate(session, sync_seed, window))


def test_unknown_creation_cannot_prove_lifetime(session, sync_seed):
    _case(session, sync_seed)
    account = session.get(AdvertiserAccount, (sync_seed.tenant_id, "acc-1"))
    account.remote_created_at = None
    session.flush()
    _blocked(*_evaluate(session, sync_seed, "LIFETIME"))


def test_other_tenant_cannot_supply_missing_day(session, sync_seed):
    from tests.modules.reporting.sync_conftest import sync_seed as seed_fixture

    today = _case(session, sync_seed)
    missing = today - timedelta(days=3)
    for kind in ("ad_day", "creative_day"):
        session.delete(
            session.get(ReportSyncState, (sync_seed.tenant_id, "acc-1", kind, missing))
        )
    other = seed_fixture.__wrapped__(session)
    _empty_days(session, other, missing, missing, "Asia/Hong_Kong")
    session.flush()
    _blocked(*_evaluate(session, sync_seed))


@pytest.mark.parametrize("window", ["PAST_7_DAYS", "LIFETIME"])
def test_gap_blocks_every_current_group_in_account(session, sync_seed, window):
    from app.modules.ads.models import AdObject
    from app.modules.reporting.store_models import AdReportDay, CreativeReportDay

    today = _case(session, sync_seed)
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            kind="adgroup",
            remote_id="g2",
            parent_kind="campaign",
            parent_remote_id="c1",
            ad_type="SMART_PLUS",
            name="组二",
            operation_status="ENABLE",
            observed_at=NOW,
            published_version=1,
        )
    )
    session.get(
        AdObject, (sync_seed.tenant_id, "acc-1", "ad", "a2")
    ).parent_remote_id = "g2"
    use(session, sync_seed, "a2", "v2")
    post(session, sync_seed, "a2", "creative-2", video="v2")
    session.get(
        AdReportDay, (sync_seed.tenant_id, "acc-1", today, "a2")
    ).adgroup_id = "g2"
    session.get(
        CreativeReportDay, (sync_seed.tenant_id, "acc-1", today, "creative-2")
    ).adgroup_id = "g2"
    for kind in ("ad_day", "creative_day"):
        session.delete(
            session.get(
                ReportSyncState,
                (sync_seed.tenant_id, "acc-1", kind, today - timedelta(days=2)),
            )
        )
    session.flush()
    loaded, found = _evaluate(session, sync_seed, window)
    assert len([e for e in loaded.entities.values() if e.kind == "material"]) == 2
    _blocked(loaded, found)


def test_unknown_creation_still_allows_proven_bounded_window(session, sync_seed):
    _case(session, sync_seed)
    session.get(
        AdvertiserAccount, (sync_seed.tenant_id, "acc-1")
    ).remote_created_at = None
    session.flush()
    loaded, found = _evaluate(session, sync_seed)
    assert not loaded.skipped_accounts and len(found.hits) == 1


def test_wholly_empty_complete_window_is_not_a_source_gap(session, sync_seed):
    today = _case(session, sync_seed)
    _empty_days(session, sync_seed, today - timedelta(days=6), today, "Asia/Hong_Kong")
    loaded, found = _evaluate(session, sync_seed)
    assert not loaded.skipped_accounts
    assert found.checked == 1 and not found.hits
    assert all(
        e.judgement_issue is None
        for e in loaded.entities.values()
        if e.kind == "material"
    )


@pytest.mark.parametrize("pending", ["identity", "parent"])
def test_source_gap_remains_auditable_when_directory_is_pending(
    session, sync_seed, pending
):
    from sqlmodel import select

    from app.modules.ads.models import AdMaterialReference, AdObject

    today = _case(session, sync_seed)
    if pending == "parent":
        session.delete(
            session.get(AdObject, (sync_seed.tenant_id, "acc-1", "adgroup", "g1"))
        )
    else:
        session.exec(
            select(AdMaterialReference).where(
                AdMaterialReference.tenant_id == sync_seed.tenant_id
            )
        ).one().ad_material_id = None
    session.delete(
        session.get(
            ReportSyncState,
            (sync_seed.tenant_id, "acc-1", "ad_day", today - timedelta(days=2)),
        )
    )
    session.flush()
    _blocked(*_evaluate(session, sync_seed))


def test_rule_run_audits_missing_day_without_queuing_action(session, sync_seed):
    from sqlmodel import select

    from app.jobs.models import PendingDispatch
    from app.modules.automation_rules.models import AutomationRule, AutomationRuleHit
    from app.modules.automation_rules.runner import EXECUTE_TASK, execute_run

    today = _case(session, sync_seed)
    session.delete(
        session.get(
            ReportSyncState,
            (sync_seed.tenant_id, "acc-1", "creative_day", today - timedelta(days=2)),
        )
    )
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="窗口缺报",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=rule(mode="AUTO").model_dump(mode="json"),
    )
    session.add(current)
    session.flush()
    run = execute_run(session, rule=current, slot_at=NOW, now=NOW)
    assert run.status == "COMPLETE" and run.hit_count == 0
    assert run.skipped_accounts == [
        {"advertiser_id": "acc-1", "reason": "MATERIAL_SOURCE_INCOMPLETE", "count": "1"}
    ]
    assert not session.exec(
        select(AutomationRuleHit).where(AutomationRuleHit.run_id == run.id)
    ).all()
    assert not session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
            PendingDispatch.task_name == EXECUTE_TASK,
        )
    ).all()
