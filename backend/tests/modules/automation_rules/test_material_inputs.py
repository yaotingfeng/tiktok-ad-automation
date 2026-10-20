"""生产断链与广告内素材误杀的回归：只用真实本地 PostgreSQL 数据。"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlmodel import select

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
    # 通用广告夹具把 a1 设为暂停；素材规则的正常基线需要整条上级链启用。
    ad = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == seed.tenant_id,
            AdObject.advertiser_id == "acc-1",
            AdObject.kind == "ad",
            AdObject.remote_id == "a1",
        )
    ).one()
    ad.operation_status = "ENABLE"
    session.add(ad)
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


def recovery_rule(*, selected=None, conditions=None):
    return definition(
        accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]},
        apply_to={
            "level": "material",
            "status": "SELECTED" if selected else "PAUSED",
            "object_ids": [selected] if selected else [],
        },
        action={"type": "ENABLE", "level": "material"},
        schedule={"frequency": "1H", "window": "CONTINUOUS"},
        conditions=conditions
        or [
            {
                "metric": "roas_total",
                "range": "LIFETIME",
                "operator": "GT",
                "values": ["0.9"],
            }
        ],
    )


@pytest.mark.parametrize(
    "kind,remote", [("campaign", "c1"), ("adgroup", "g1"), ("ad", "a1")]
)
@pytest.mark.parametrize("selected", [False, True])
def test_recovery_never_enables_material_under_paused_parent(
    session, sync_seed, kind, remote, selected
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", status="DISABLE")
    post(session, sync_seed, "a1", "p1", revenue="36")
    ancestor = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.advertiser_id == "acc-1",
            AdObject.kind == kind,
            AdObject.remote_id == remote,
        )
    ).one()
    ancestor.operation_status = "DISABLE"
    session.add(ancestor)
    session.flush()
    from app.modules.automation_rules.material_inputs import material_use_id

    d = recovery_rule(
        selected=f"acc-1:{material_use_id('a1', 'v1', 'VIDEO')}" if selected else None
    )
    loaded, found = result(session, sync_seed, d)
    assert not found.hits
    assert evaluate(d, loaded, ignore_status=True).hits


def test_material_status_conditions_use_own_and_ancestor_states(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", status="DISABLE")
    post(session, sync_seed, "a1", "p1", revenue="36")
    d = recovery_rule(
        conditions=[
            {"metric": "ad_status", "operator": "EQ", "values": ["ENABLE"]},
            {"metric": "material_status", "operator": "EQ", "values": ["DISABLE"]},
        ]
    )
    loaded, found = result(session, sync_seed, d)
    assert len(found.hits) == 1
    material = next(e for e in loaded.entities.values() if e.kind == "material")
    assert {
        k: material.attributes[k]
        for k in ("campaign_status", "adgroup_status", "ad_status", "material_status")
    } == {
        "campaign_status": "ENABLE",
        "adgroup_status": "ENABLE",
        "ad_status": "ENABLE",
        "material_status": "DISABLE",
    }


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


@pytest.mark.parametrize(
    "kind,remote", [("campaign", "c1"), ("adgroup", "g1"), ("ad", "a1")]
)
@pytest.mark.parametrize("has_report", [False, True])
def test_active_material_excludes_disabled_ancestors_from_hits_and_issues(
    session, sync_seed, kind, remote, has_report
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    if has_report:
        post(session, sync_seed, "a1", "p1")
    ancestor = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.advertiser_id == "acc-1",
            AdObject.kind == kind,
            AdObject.remote_id == remote,
        )
    ).one()
    ancestor.operation_status = "DISABLE"
    session.add(ancestor)
    session.flush()
    loaded, found = result(session, sync_seed)
    assert found.checked == 0 and found.hits == () and found.skipped_count == 0
    assert loaded.skipped_accounts == []
    # 反向冲突检查须忽略作用状态，仍能复核旧止损原因，不能被本次范围修正绕过。
    if has_report:
        assert evaluate(rule(), loaded, ignore_status=True).hits


def test_paused_material_does_not_include_enabled_material_below_disabled_ad(
    session, sync_seed
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1")
    ad = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.kind == "ad",
            AdObject.remote_id == "a1",
        )
    ).one()
    ad.operation_status = "DISABLE"
    session.add(ad)
    session.flush()
    d = rule()
    d = d.model_copy(
        update={"apply_to": d.apply_to.model_copy(update={"status": "PAUSED"})}
    )
    _, found = result(session, sync_seed, d)
    assert found.checked == 0 and not found.hits


def test_missing_ancestor_is_reported_instead_of_hidden_as_disabled(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "p1")
    ad = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.kind == "ad",
            AdObject.remote_id == "a1",
        )
    ).one()
    ad.parent_remote_id = "not-synced"
    session.add(ad)
    session.flush()
    loaded, found = result(session, sync_seed)
    assert found.checked == 0 and not found.hits and found.skipped_count == 1
    assert loaded.skipped_accounts == [
        {"advertiser_id": "acc-1", "reason": "MATERIAL_PARENT_UNKNOWN", "count": "1"}
    ]


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


@pytest.mark.parametrize("notify_empty", [False, True])
def test_waiting_material_is_recorded_but_only_notifies_when_opted_in(
    session, sync_seed, notify_empty
):
    from app.modules.automation_rules.models import AutomationRule
    from app.modules.automation_rules.runner import build_run_card, execute_run

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="广告内素材缺数",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=rule(
            notification={"enabled": True, "notify_without_hits": notify_empty}
        ).model_dump(mode="json"),
    )
    session.add(current)
    session.flush()
    run = execute_run(session, rule=current, slot_at=NOW, now=NOW)
    assert run.skipped_count == 1
    assert run.skipped_accounts[0]["reason"] == "MATERIAL_LINK_MISSING"
    import json

    card = json.dumps(build_run_card(current, run), ensure_ascii=False)
    assert run.notify_status == ("PENDING" if notify_empty else "NOT_REQUIRED")
    assert "素材与报表尚未关联" not in card
    assert "本轮无命中" in card
    assert "账户数据未更新" not in card
    if not notify_empty:
        from app.modules.automation_rules.runner import deliver_notification

        # 旧版已入队的普通待数据报告，也必须在发送前被拦住。
        run.notify_status = "PENDING"
        assert deliver_notification(session, run_id=run.id) == "NOT_REQUIRED"


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
    card = json.dumps(build_summary_card(current, runs), ensure_ascii=False)
    assert "素材与报表尚未关联" not in card
    assert "运行 2 次" in card
    assert runs[0].skipped_accounts[0]["count"] == "2"
    assert runs[1].skipped_accounts[0]["count"] == "2"


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


def test_ad_judgement_cannot_bypass_material_recovery_parent_guard(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", status="DISABLE")
    post(session, sync_seed, "a1", "p1", revenue="36")
    campaign = session.exec(
        select(AdObject).where(
            AdObject.tenant_id == sync_seed.tenant_id,
            AdObject.advertiser_id == "acc-1",
            AdObject.kind == "campaign",
            AdObject.remote_id == "c1",
        )
    ).one()
    campaign.operation_status = "DISABLE"
    session.add(campaign)
    session.flush()
    d = definition(
        accounts={"mode": "ACCOUNTS", "advertiser_ids": ["acc-1"]},
        apply_to={"level": "ad", "status": "ACTIVE"},
        action={"type": "ENABLE", "level": "material"},
        schedule={"frequency": "1H", "window": "CONTINUOUS"},
        conditions=[{"metric": "ad_status", "operator": "EQ", "values": ["ENABLE"]}],
    )
    loaded, found = result(session, sync_seed, d)
    assert loaded.entities["ad:acc-1:a1"].attributes["ad_status"] == "ENABLE"
    assert loaded.entities["ad:acc-1:a1"].attributes["campaign_status"] == "DISABLE"
    assert not found.hits


def test_status_parent_lookup_stays_inside_material_account(session, sync_seed):
    setup(session, sync_seed)
    use(session, sync_seed, "a1", status="DISABLE")
    post(session, sync_seed, "a1", "p1", revenue="36")
    session.add(
        AdObject(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-2",
            kind="campaign",
            remote_id="c1",
            ad_type="SMART_PLUS",
            operation_status="DISABLE",
            observed_at=NOW,
            published_version=1,
        )
    )
    session.flush()
    _, found = result(session, sync_seed, recovery_rule())
    assert len(found.hits) == 1
    assert found.hits[0].targets[0].advertiser_id == "acc-1"


def test_material_inputs_bound_temporary_memory_across_advertisements(
    session, sync_seed
):
    import tracemalloc
    from dataclasses import replace

    from sqlalchemy import insert

    from app.modules.automation_rules.inputs import AccountInfo, _objects
    from app.modules.automation_rules.material_inputs import load_material_entities

    setup(session, sync_seed)
    accounts = {
        "acc-1": AccountInfo("acc-1", "bc-a", "Asia/Hong_Kong", "USD", "账户一")
    }
    ads = _objects(session, sync_seed.tenant_id, accounts, NOW)
    for number in range(50):
        remote = f"history-ad-{number}"
        ads[f"ad:acc-1:{remote}"] = replace(
            ads["ad:acc-1:a1"], key=f"ad:acc-1:{remote}", remote_id=remote
        )
    # 同一视频的历史身份必须保留，但每个广告只有最近版本可作为执行身份。
    session.connection().execute(
        insert(AdMaterialReference),
        [
            {
                "tenant_id": sync_seed.tenant_id,
                "advertiser_id": "acc-1",
                "ad_remote_id": f"history-ad-{number}",
                "platform_material_id": "video",
                "ad_material_id": f"identity-{version}",
                "material_type": "VIDEO",
                "name": "素材",
                "complete": True,
                "published_version": version,
                "operation_status": "ENABLE",
                "creative_ids": [],
            }
            for number in range(50)
            for version in range(100)
            if version > 0
        ],
    )
    tracemalloc.start()
    try:
        entities = load_material_entities(session, sync_seed.tenant_id, accounts, ads)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(entities) == 50
    assert {
        use["ad_material_id"]
        for entity in entities.values()
        for use in entity.material_uses
    } == {"identity-99"}
    # 输入输出只有50个当前身份，不能同时持有全部4950条历史ORM对象。
    assert peak < 6 * 1024 * 1024


@pytest.mark.parametrize("mapping", ["owner_missing", "vid_missing", "empty_vid"])
@pytest.mark.parametrize(
    "spend,revenue,expected_hits", [("0", "0", 1), ("1", "0", 0), ("0", "1", 0)]
)
def test_only_valuable_unlinked_posts_block_rules(
    session, sync_seed, mapping, spend, revenue, expected_hits
):
    """零金额缺关联不能使本来应止损的账户/广告失效，收入独有的帖子仍保护。"""
    from sqlalchemy import text

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "paid")
    post(
        session,
        sync_seed,
        "a1",
        "gap",
        spend=spend,
        revenue=revenue,
        video="" if mapping == "empty_vid" else None,
    )
    if mapping == "owner_missing":
        session.connection().execute(
            text(
                "DELETE FROM material_post_link WHERE tenant_id=:t AND main_material_id='gap'"
            ),
            {"t": sync_seed.tenant_id},
        )
    loaded, found = result(session, sync_seed)
    assert len(found.hits) == expected_hits
    ignored = [
        x
        for x in loaded.skipped_accounts
        if x["reason"] == "ZERO_VALUE_UNLINKED_POSTS_IGNORED"
    ]
    if expected_hits:
        assert ignored == [
            {
                "advertiser_id": "acc-1",
                "reason": "ZERO_VALUE_UNLINKED_POSTS_IGNORED",
                "count": "1",
            }
        ]
    else:
        assert not ignored
        expected_reason = (
            "MATERIAL_REPORT_UNLINKED"
            if mapping == "owner_missing"
            else "MATERIAL_LINK_UNRESOLVED"
        )
        assert any(x["reason"] == expected_reason for x in loaded.skipped_accounts)


def test_ignored_posts_count_persists_in_rule_run_without_alert(session, sync_seed):
    from sqlalchemy import text

    from app.modules.automation_rules.data_issues import actionable_input_issues
    from app.modules.automation_rules.models import AutomationRule, AutomationRuleRun
    from app.modules.automation_rules.runner import execute_run

    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "paid")
    for pid in ("no-owner", "no-vid"):
        post(session, sync_seed, "a1", pid, spend="0", revenue="0", video=None)
        fact = session.get(
            MaterialReportDay, (sync_seed.tenant_id, "acc-1", TODAY, pid)
        )
        session.add(
            MaterialReportDay(
                **(fact.model_dump() | {"stat_date": TODAY - timedelta(days=1)})
            )
        )
        session.flush()
    session.connection().execute(
        text(
            "DELETE FROM material_post_link WHERE tenant_id=:t AND main_material_id='no-owner'"
        ),
        {"t": sync_seed.tenant_id},
    )
    definition = rule().model_dump(mode="json")
    current = AutomationRule(
        tenant_id=sync_seed.tenant_id,
        name="零金额排除计数",
        created_by=sync_seed.actor_id,
        updated_by=sync_seed.actor_id,
        definition=definition,
    )
    session.add(current)
    session.flush()
    run = execute_run(session, rule=current, slot_at=NOW, now=NOW)
    session.flush()
    session.expire(run)
    saved = session.get(AutomationRuleRun, run.id)
    assert saved.status == "COMPLETE" and saved.hit_count == 1
    assert saved.skipped_accounts == [
        {
            "advertiser_id": "acc-1",
            "reason": "ZERO_VALUE_UNLINKED_POSTS_IGNORED",
            "count": "2",
        }
    ]
    assert actionable_input_issues(saved.skipped_accounts) == []


@pytest.mark.parametrize("mapping", ["owner_missing", "vid_missing"])
@pytest.mark.parametrize("window,expected_hits", [("LIFETIME", 1), ("TODAY", 0)])
def test_rule_gap_uses_cross_day_total_in_its_own_window(
    session, sync_seed, mapping, window, expected_hits
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    post(session, sync_seed, "a1", "paid")
    post(session, sync_seed, "a1", "gap", spend="1", revenue="0", video=None)
    row = session.get(MaterialReportDay, (sync_seed.tenant_id, "acc-1", TODAY, "gap"))
    session.add(
        MaterialReportDay(
            **(
                row.model_dump()
                | {"stat_date": TODAY - timedelta(days=1), "spend": Decimal(-1)}
            )
        )
    )
    if mapping == "owner_missing":
        link = session.get(
            MaterialPostLink, (sync_seed.tenant_id, "acc-1", "gap", "a1")
        )
        session.delete(link)
    session.flush()
    d = rule()
    d = d.model_copy(
        update={
            "conditions": [c.model_copy(update={"range": window}) for c in d.conditions]
        }
    )
    loaded, found = result(session, sync_seed, d)
    assert len(found.hits) == expected_hits
    ignored = [
        x
        for x in loaded.skipped_accounts
        if x["reason"] == "ZERO_VALUE_UNLINKED_POSTS_IGNORED"
    ]
    assert bool(ignored) is bool(expected_hits)


def test_zero_value_mapped_impressions_do_not_dilute_paid_rule_metrics(
    session, sync_seed
):
    setup(session, sync_seed)
    use(session, sync_seed, "a1")
    for pid, spend, revenue, impressions in [
        ("paid", "30", "15", 100),
        ("organic", "0", "0", 10000),
    ]:
        post(session, sync_seed, "a1", pid, spend=spend, revenue=revenue)
        row = session.get(MaterialReportDay, (sync_seed.tenant_id, "acc-1", TODAY, pid))
        row.impressions = impressions
        session.add(row)
    session.flush()
    loaded, found = result(session, sync_seed)
    assert len(found.hits) == 1
    values = next(iter(loaded.facts["LIFETIME"].values()))
    assert values["impressions"] == 100
    assert values["spend"] == 30 and values["revenue_total"] == 15
    # 已有的真实映射和页面原始事实不改写，只有规则的付费指标排除零金额曝光。
    assert len(session.exec(select(MaterialPostLink)).all()) == 2
    assert (
        session.get(
            MaterialReportDay, (sync_seed.tenant_id, "acc-1", TODAY, "organic")
        ).impressions
        == 10000
    )
    assert loaded.skipped_accounts == []
