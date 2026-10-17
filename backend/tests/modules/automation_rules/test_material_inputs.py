"""生产断链与广告内素材误杀的回归：只用真实本地 PostgreSQL 数据。"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.automation_rules.evaluate import evaluate
from app.modules.automation_rules.inputs import load_inputs
from app.modules.automation_rules.pickers import search_objects
from app.modules.reporting.store_models import (
    MaterialPostLink,
    MaterialReportDay,
    ReportSyncState,
)
from tests.modules.automation_rules.test_inputs import NOW, TODAY, _seed, definition


def setup(session, seed):
    _seed(session, seed)
    session.add(
        AdObject(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            kind="ad",
            remote_id="a2",
            parent_kind="adgroup",
            parent_remote_id="g1",
            ad_type="SMART_PLUS",
            name="广告二",
            operation_status="ENABLE",
            observed_at=NOW,
            published_version=1,
        )
    )
    session.add(
        ReportSyncState(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            report_kind="material_day",
            stat_date=TODAY,
            status="COMPLETE",
            last_success_at=NOW,
        )
    )
    session.flush()


def use(session, seed, ad, video="v1", *, mid=None, version=1, status="ENABLE"):
    row = AdMaterialReference(
        tenant_id=seed.tenant_id,
        advertiser_id="acc-1",
        ad_remote_id=ad,
        platform_material_id=video,
        ad_material_id=mid or f"am-{ad}",
        material_type="VIDEO",
        name=f"{video}.mp4",
        operation_status=status,
        complete=True,
        published_version=version,
    )
    session.add(row)
    session.flush()
    return row


def post(session, seed, ad, pid, *, video="v1", spend="30", revenue="15", day=TODAY):
    session.add(
        MaterialPostLink(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            main_material_id=pid,
            ad_remote_id=ad,
            main_material_type="VIDEO_SPARK_ADS",
            platform_material_id=video,
            ad_material_id=f"am-{ad}",
            observed_at=NOW,
        )
    )
    if session.get(MaterialReportDay, (seed.tenant_id, "acc-1", day, pid)) is None:
        session.add(
            MaterialReportDay(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                stat_date=day,
                main_material_id=pid,
                main_material_type="VIDEO_SPARK_ADS",
                currency="USD",
                timezone="Asia/Hong_Kong",
                fetched_at=NOW,
                sync_job_id=uuid4(),
                spend=Decimal(spend),
                revenue_total=Decimal(revenue),
            )
        )
    session.flush()


def rule(**patch):
    patch.setdefault("schedule", {"frequency": "1H", "window": "CONTINUOUS"})
    return definition(
        accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]},
        apply_to={"level": "material", "status": "ACTIVE"},
        action={"type": "PAUSE", "level": "material"},
        conditions=[
            {
                "metric": "spend",
                "range": "LIFETIME",
                "operator": "GT",
                "values": ["20"],
            },
            {
                "metric": "roas_total",
                "range": "LIFETIME",
                "operator": "LT",
                "values": ["0.85"],
            },
        ],
        **patch,
    )


def result(session, seed, d=None):
    d = d or rule()
    loaded = load_inputs(session, tenant_id=seed.tenant_id, definition=d, now=NOW)
    return loaded, evaluate(d, loaded)


def test_empty_directory_main_id_uses_links_and_only_hits_bad_ad(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    use(session, sync_seed, "a2")
    post(session, sync_seed, "a1", "p1")
    post(session, sync_seed, "a2", "p2", revenue="36")
    loaded, found = result(session, sync_seed)
    assert found.checked == 2
    assert len(found.hits) == 1
    assert found.hits[0].metrics["roas_total:LIFETIME"] == "0.5"
    assert [
        u["ad_remote_id"] for t in found.hits[0].targets for u in t.material_uses
    ] == ["a1"]
    assert len([e for e in loaded.entities.values() if e.kind == "material"]) == 2


def test_old_posts_sum_into_current_use_without_old_execution_identity(
    session, sync_seed
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", mid="am-old", version=1)
    use(session, sync_seed, "a1", mid="am-new", version=2)
    post(
        session,
        sync_seed,
        "a1",
        "old",
        spend="15",
        revenue="5",
        day=TODAY - timedelta(days=1),
    )
    post(session, sync_seed, "a1", "new", spend="15", revenue="5")
    _, found = result(session, sync_seed)
    assert found.checked == 1 and len(found.hits) == 1
    assert found.hits[0].metrics["spend:LIFETIME"] == "30"
    assert found.hits[0].targets[0].material_uses[0]["ad_material_id"] == "am-new"


def test_shared_post_never_uses_aggregate_as_individual_ad_performance(
    session, sync_seed
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    use(session, sync_seed, "a2")
    post(session, sync_seed, "a1", "shared")
    post(session, sync_seed, "a2", "shared")
    loaded, found = result(session, sync_seed)
    assert found.hits == () and found.skipped_count == 2
    assert any(i["reason"] == "MATERIAL_SHARED_REPORT" for i in loaded.skipped_accounts)
    assert len(found.unknown_targets) == 2


def test_missing_link_is_reported_and_not_treated_as_zero_spend(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    loaded, found = result(session, sync_seed)
    assert found.hits == () and found.skipped_count == 1
    assert any(i["reason"] == "MATERIAL_LINK_MISSING" for i in loaded.skipped_accounts)


def test_ambiguous_current_identity_is_skipped(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", mid="am-x")
    use(session, sync_seed, "a1", mid="am-y")
    post(session, sync_seed, "a1", "p1")
    loaded, found = result(session, sync_seed)
    assert not found.hits
    assert any(
        i["reason"] == "MATERIAL_IDENTITY_AMBIGUOUS" for i in loaded.skipped_accounts
    )


def test_video_total_does_not_act_on_incomplete_member_data(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    use(session, sync_seed, "a2")
    post(session, sync_seed, "a1", "p1")
    _, found = result(
        session,
        sync_seed,
        rule(
            aggregation="VIDEO",
            schedule={
                "frequency": "1H",
                "window": "CONTINUOUS",
                "timezone_basis": "RULE",
            },
        ),
    )
    assert not found.hits and found.unknown_targets


def test_picker_and_selected_rule_identify_one_ad_material(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    use(session, sync_seed, "a2")
    post(session, sync_seed, "a1", "p1")
    post(session, sync_seed, "a2", "p2")
    choices = search_objects(
        session,
        context=sync_seed.context,
        level="material",
        query="v1",
        advertiser_ids=["acc-1"],
        limit=50,
    )
    assert len(choices) == 2 and choices[0].value != choices[1].value
    d = rule().model_copy(
        update={
            "apply_to": rule().apply_to.model_copy(
                update={"status": "SELECTED", "object_ids": (choices[0].value,)}
            )
        }
    )
    _, found = result(session, sync_seed, d)
    assert found.checked == 1 and len(found.hits) == 1
    assert len(found.hits[0].targets[0].material_uses) == 1


def test_decimal_roas_boundary_does_not_round_085_into_hit(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1", revenue="25.5")
    _, found = result(session, sync_seed)
    assert found.checked == 1 and found.hits == ()


def test_partial_null_revenue_is_not_hidden_by_other_days(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1", spend="15", revenue="5")
    post(
        session,
        sync_seed,
        "a1",
        "old",
        spend="15",
        revenue="5",
        day=TODAY - timedelta(days=1),
    )
    from sqlalchemy import text

    session.connection().execute(
        text(
            "UPDATE material_report_day SET revenue_total=NULL WHERE tenant_id=:t AND stat_date=:d"
        ),
        {"t": sync_seed.tenant_id, "d": TODAY - timedelta(days=1)},
    )
    loaded, found = result(session, sync_seed)
    assert found.hits == ()
    assert any(
        i["reason"] == "MATERIAL_METRIC_MISSING" for i in loaded.skipped_accounts
    )


def test_run_persists_partial_material_issue_and_card_explains_it(session, sync_seed):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.automation_rules.runner import build_run_card, execute_run

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="广告内素材缺数",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=rule().model_dump(mode="json"),
    )
    session.add(current)
    session.flush()
    run = execute_run(session, rule=current, slot_at=NOW, now=NOW)
    assert run.skipped_count == 1
    assert run.skipped_accounts[0]["reason"] == "MATERIAL_LINK_MISSING"
    import json

    card = json.dumps(build_run_card(current, run, []), ensure_ascii=False)
    assert "素材与报表尚未关联" in card
    assert "账户数据未更新" not in card


def test_one_missing_material_does_not_make_whole_account_unknown(session, sync_seed):
    from app.modules.automation_rules.executor import _still_matching
    from app.modules.automation_rules.models import AutomationRule

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    use(session, sync_seed, "a2")
    post(session, sync_seed, "a1", "p1", revenue="36")
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="素材反向复核",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=rule(mode="CONFIRM").model_dump(mode="json"),
    )
    session.add(current)
    session.flush()
    match = _still_matching(session, sync_seed.tenant_id, current.id, NOW)
    assert match.unknown_accounts == frozenset()
    assert len(match.unknown_keys) == 1


def test_summary_card_reports_material_issues_without_stale_account_claim(
    session, sync_seed
):
    import json

    from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
    from app.modules.automation_rules.summary import build_summary_card

    setup(session, sync_seed)
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="素材汇总",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=rule().model_dump(mode="json"),
    )
    runs = [
        AutomationRuleRun(
            tenant_id=sync_seed.tenant_id,
            rule_id=current.id,
            rule_version=1,
            definition=current.definition,
            slot_at=NOW + timedelta(hours=n),
            skipped_accounts=[
                {
                    "advertiser_id": "acc-1",
                    "reason": "MATERIAL_LINK_MISSING",
                    "count": "2",
                }
            ],
        )
        for n in (0, 1)
    ]
    card = json.dumps(
        build_summary_card(current, runs, [], "Asia/Shanghai"), ensure_ascii=False
    )
    assert "素材与报表尚未关联" in card
    assert "账户数据未更新" not in card
    assert "2 个广告内素材" in card
    assert "4 个广告内素材" not in card


def test_video_total_does_not_ignore_stale_selected_account(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1")
    d = rule(
        aggregation="VIDEO",
        schedule={"frequency": "1H", "window": "CONTINUOUS", "timezone_basis": "RULE"},
    )
    d = d.model_copy(update={"accounts": d.accounts.model_copy(update={"mode": "ALL"})})
    loaded, found = result(session, sync_seed, d)
    assert any(i["reason"] == "STALE_REPORT" for i in loaded.skipped_accounts)
    assert found.hits == () and found.unknown_targets


def test_unlinked_historical_post_prevents_false_low_roas_hit(session, sync_seed):
    from sqlalchemy import text

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1")
    post(
        session,
        sync_seed,
        "a1",
        "unlocated-old",
        revenue="90",
        day=TODAY - timedelta(days=1),
    )
    session.connection().execute(
        text(
            "DELETE FROM material_post_link WHERE tenant_id=:t AND main_material_id='unlocated-old'"
        ),
        {"t": sync_seed.tenant_id},
    )
    loaded, found = result(session, sync_seed)
    assert found.hits == ()
    assert any(
        i["reason"] == "MATERIAL_REPORT_UNLINKED" for i in loaded.skipped_accounts
    )
