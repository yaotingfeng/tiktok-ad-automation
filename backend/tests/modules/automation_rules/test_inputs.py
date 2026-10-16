from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.modules.ads.models import (
    AdMaterialReference,
    AdObject,
    CampaignNameProjection,
)
from app.modules.automation_rules.inputs import (
    AccountInfo,
    _materials,
    expand_accounts,
    load_inputs,
    local_window,
    plan_next_run,
)
from app.modules.automation_rules.schemas import AccountScope, RuleDefinition
from app.modules.reporting.publish import AccountMeta, publish_ad_day
from app.modules.reporting.store_models import MaterialPostLink, ReportSyncState
from tests.modules.automation_rules.test_definition import base
from tests.modules.reporting.sync_conftest import ad_row

NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)  # 香港 11:00,UTC 03:00
TODAY = date(2026, 10, 14)


def _seed(session, seed, *, fresh=("acc-1", "acc-2")):
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={
            "acc-1": AccountMeta("USD", "Asia/Hong_Kong"),
            "acc-2": AccountMeta("USD", "UTC"),
        },
        start=TODAY - timedelta(days=2),
        end=TODAY,
        rows=[
            ad_row("a1", TODAY, campaign="c1", adgroup="g1", spend="10.00"),
            ad_row(
                "a1",
                TODAY - timedelta(days=2),
                campaign="c1",
                adgroup="g1",
                spend="4.00",
            ),
            ad_row("b1", TODAY, campaign="c9", adgroup="g9", spend="2.00", adv="acc-2"),
        ],
    )
    for adv in ("acc-1", "acc-2"):
        state = session.get(ReportSyncState, (seed.tenant_id, adv, "ad_day", TODAY))
        state.last_success_at = NOW - (
            timedelta(minutes=10) if adv in fresh else timedelta(hours=5)
        )
    for adv, kind, rid, parent, name, cfg, status in (
        (
            "acc-1",
            "campaign",
            "c1",
            None,
            "嘉书-总裁-备注",
            {"create_time": "2026-10-01 00:00:00", "budget": "50"},
            "ENABLE",
        ),
        (
            "acc-1",
            "adgroup",
            "g1",
            "c1",
            "组一",
            {"budget": "20", "roas_bid": "1.2"},
            "ENABLE",
        ),
        ("acc-1", "ad", "a1", "g1", "广告一", {}, "DISABLE"),
        ("acc-2", "campaign", "c9", None, "无效命名", {}, "ENABLE"),
        ("acc-2", "adgroup", "g9", "c9", "组九", {}, "ENABLE"),
        ("acc-2", "ad", "b1", "g9", "广告九", {}, "ENABLE"),
    ):
        session.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id=adv,
                kind=kind,
                remote_id=rid,
                parent_kind={"adgroup": "campaign", "ad": "adgroup"}.get(kind),
                parent_remote_id=parent,
                ad_type="SMART_PLUS",
                name=name,
                configuration=cfg,
                operation_status=status,
                observed_at=NOW,
                published_version=1,
            )
        )
    session.add(
        CampaignNameProjection(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-1",
            campaign_remote_id="c1",
            name_revision=1,
            raw_name="嘉书-总裁-备注",
            provider_label="嘉书",
            drama_name="总裁",
            status="VALID",
            parser_revision=1,
            grouping_revision=1,
        )
    )
    session.add(
        CampaignNameProjection(
            tenant_id=seed.tenant_id,
            advertiser_id="acc-2",
            campaign_remote_id="c9",
            name_revision=1,
            raw_name="无效命名",
            status="INVALID",
            parser_revision=1,
            grouping_revision=1,
        )
    )
    session.flush()


def definition(**patch):
    return RuleDefinition.model_validate(base(**patch))


def test_account_in_two_bcs_counted_once(session, sync_seed):
    accounts = expand_accounts(
        session, tenant_id=sync_seed.tenant_id, scope=AccountScope(mode="ALL")
    )
    assert sorted(accounts) == ["acc-1", "acc-2"]
    assert accounts["acc-2"].bc_id == "bc-a"  # 多 BC 时取最小 BC ID,结果稳定
    only_b = expand_accounts(
        session,
        tenant_id=sync_seed.tenant_id,
        scope=AccountScope(mode="BC", bc_ids=("bc-b",)),
    )
    assert list(only_b) == ["acc-2"] and only_b["acc-2"].bc_id == "bc-b"


def test_local_windows_follow_account_timezone():
    late = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)  # 香港已是 10-15
    assert local_window("TODAY", "Asia/Hong_Kong", late) == (date(2026, 10, 15),) * 2
    assert local_window("TODAY", "UTC", late) == (date(2026, 10, 14),) * 2
    assert local_window("PAST_3_DAYS", "UTC", late) == (
        date(2026, 10, 12),
        date(2026, 10, 14),
    )
    assert local_window("YESTERDAY", "UTC", late) == (date(2026, 10, 13),) * 2


def test_entities_facts_and_attributes(session, sync_seed):
    _seed(session, sync_seed)
    rule = definition(
        conditions=[
            {"metric": "spend", "range": "TODAY", "operator": "GT", "values": ["1"]},
            {
                "metric": "spend",
                "range": "PAST_3_DAYS",
                "operator": "GT",
                "values": ["1"],
            },
        ]
    )
    inputs = load_inputs(
        session, tenant_id=sync_seed.tenant_id, definition=rule, now=NOW
    )
    g1 = inputs.entities["adgroup:acc-1:g1"]
    assert g1.drama == "嘉书-总裁" and g1.naming_valid
    assert g1.attributes["budget"] == Decimal("20") and g1.attributes[
        "roas_bid"
    ] == Decimal("1.2")
    assert inputs.entities["adgroup:acc-2:g9"].naming_valid is False
    assert inputs.facts["TODAY"]["adgroup:acc-1:g1"]["spend"] == Decimal("10.00")
    assert inputs.facts["PAST_3_DAYS"]["adgroup:acc-1:g1"]["spend"] == Decimal("14.00")
    # 后代对象(广告)也加载,供执行层级展开使用。
    assert inputs.entities["ad:acc-1:a1"].parent_key == "adgroup:acc-1:g1"
    assert inputs.entities["campaign:acc-1:c1"].attributes[
        "days_since_creation"
    ] == Decimal("13")


def test_stale_accounts_are_skipped(session, sync_seed):
    _seed(session, sync_seed, fresh=("acc-1",))
    inputs = load_inputs(
        session, tenant_id=sync_seed.tenant_id, definition=definition(), now=NOW
    )
    assert "acc-2" not in inputs.accounts
    assert not any(e.advertiser_id == "acc-2" for e in inputs.entities.values())
    assert inputs.skipped_accounts == [
        {"advertiser_id": "acc-2", "reason": "STALE_REPORT"}
    ]


def test_account_timezone_rules_judge_each_account_at_its_local_time(
    session, sync_seed
):
    _seed(session, sync_seed)
    rule = definition(
        schedule={
            "frequency": "DAILY",
            "daily_at": "11:00",
            "window": "CONTINUOUS",
            "timezone_basis": "ACCOUNT",
        }
    )
    # 香港账户当地 11:00 = UTC 03:00 先到；UTC 账户当地 11:00 = UTC 11:00。
    first = plan_next_run(
        session,
        tenant_id=sync_seed.tenant_id,
        definition=rule,
        rule_timezone="Asia/Shanghai",
        after=NOW - timedelta(minutes=1),
    )
    assert first == NOW
    assert plan_next_run(
        session,
        tenant_id=sync_seed.tenant_id,
        definition=rule,
        rule_timezone="Asia/Shanghai",
        after=first,
    ) == datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
    # 定时运行只判断当地正处于槽位的账户；试运行不带槽位，判断全部账户。
    timed = load_inputs(
        session,
        tenant_id=sync_seed.tenant_id,
        definition=rule,
        now=NOW,
        slot_at=first,
    )
    assert list(timed.accounts) == ["acc-1"]
    preview = load_inputs(
        session, tenant_id=sync_seed.tenant_id, definition=rule, now=NOW
    )
    assert sorted(preview.accounts) == ["acc-1", "acc-2"]


def test_materials_carry_video_from_report_links(session, sync_seed):
    # 视频归属与素材报表同源（主素材 → 视频对照）；对不上视频的主素材为空，不参与视频合计。
    tenant = sync_seed.tenant_id
    for main, ad in (("p1", "a1"), ("p2", "a2")):
        session.add(
            AdMaterialReference(
                tenant_id=tenant,
                advertiser_id="acc-1",
                ad_remote_id=ad,
                platform_material_id=f"v-{main}",
                ad_material_id=f"am-{main}",
                material_type="VIDEO",
                name=main,
                main_material_id=main,
                main_material_type="VIDEO",
                operation_status="ENABLE",
                complete=True,
                published_version=1,
            )
        )
    session.add(
        MaterialPostLink(
            tenant_id=tenant,
            advertiser_id="acc-1",
            main_material_id="p1",
            ad_remote_id="a1",
            main_material_type="VIDEO",
            platform_material_id="v1",
            observed_at=NOW,
        )
    )
    session.flush()
    accounts = {"acc-1": AccountInfo("acc-1", "bc", "UTC", "USD", "A")}
    found = _materials(session, tenant, accounts, {})
    assert found["material:acc-1:p1"].video == "v1"
    assert found["material:acc-1:p2"].video is None
