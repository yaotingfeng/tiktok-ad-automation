from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.ads.models import AdMaterialReference, AdObject, CampaignNameProjection
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.publish import (
    AccountMeta,
    publish_ad_day,
    publish_material_day,
)
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import METRIC_COLUMNS
from tests.modules.reporting.sync_conftest import ad_row
from tests.modules.reporting.test_jobs import D


def seed_empty(session, seed, *, d0="5"):
    for campaign in ("c1", "c2"):
        session.add(
            AdObject(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                kind="campaign",
                remote_id=campaign,
                ad_type="SMART_PLUS",
                name="嘉书-同剧",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=seed.tenant_id,
                advertiser_id="acc-1",
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="嘉书-同剧",
                provider_label="嘉书",
                drama_name="同剧",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    publish_ad_day(
        session,
        tenant_id=seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a", D, campaign="c1", spend="10", d0=d0)],
    )


def rows(session, seed, dimension="campaign", start=D):
    return build_rows(
        session,
        context=seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension=dimension, start_date=start, end_date=D, advertiser_ids=("acc-1",)
        ),
    )


@pytest.mark.parametrize(
    "d0,expected,state",
    [("5", Decimal("5"), "AVAILABLE"), ("-", Decimal("0"), "PARTIAL")],
)
def test_complete_empty_campaign_does_not_poison_drama_and_platform_null_is_partial(
    session, sync_seed, d0, expected, state
):
    # 部分合计规则：平台显式 NULL 的成员不参与求和也不当成 0，剧行合计其余成员
    # （此处为确认零消耗的 c2）并标 PARTIAL。
    seed_empty(session, sync_seed, d0=d0)
    result = rows(session, sync_seed, "drama")
    assert len(result) == 1
    vector = result[0].metric_buckets[0]
    assert vector.values["spend"] == Decimal("10")
    assert vector.availability["spend"] == "AVAILABLE"
    assert vector.values["revenue_d0"] == expected
    assert vector.availability["revenue_d0"] == state
    assert vector.values["roas_d0"] == expected / Decimal("10")
    assert vector.availability["roas_d0"] == state
    assert result[0].coverage["status"] == "COMPLETE"


def test_complete_empty_object_has_zero_additive_metrics_and_null_ratios(
    session, sync_seed
):
    seed_empty(session, sync_seed)
    empty = next(r for r in rows(session, sync_seed) if r.display["remote_id"] == "c2")
    vector = empty.metric_buckets[0]
    assert all(vector.values[k] == 0 for k in METRIC_COLUMNS)
    assert all(
        vector.values[k] is None
        for k in (
            "roas_d0",
            "roas_d3",
            "roas_d7",
            "roas_d14",
            "roas_d30",
            "roas_total",
            "ctr",
            "cost_per_ad_impression_event",
        )
    )


def test_incomplete_empty_remains_null_but_existing_facts_keep_sum(session, sync_seed):
    seed_empty(session, sync_seed)
    result = {
        r.display["remote_id"]: r
        for r in rows(session, sync_seed, start=D - timedelta(days=1))
    }
    assert all(result["c2"].metric_buckets[0].values[k] is None for k in METRIC_COLUMNS)
    assert result["c1"].metric_buckets[0].values["spend"] == 10
    assert all(r.coverage["status"] == "INCOMPLETE" for r in result.values())
    assert result["c2"].coverage["stale_dates"] == [str(D - timedelta(days=1))]


@pytest.mark.parametrize("complete", [True, False])
def test_empty_directory_material_uses_own_coverage_and_unsupported_metrics(
    session, sync_seed, complete
):
    session.add(
        AdMaterialReference(
            tenant_id=sync_seed.tenant_id,
            advertiser_id="acc-1",
            ad_remote_id="a",
            platform_material_id="p",
            main_material_id="m",
            main_material_type="VIDEO",
            material_type="VIDEO",
            complete=True,
            published_version=1,
        )
    )
    if complete:
        publish_material_day(
            session,
            tenant_id=sync_seed.tenant_id,
            job_id=uuid4(),
            accounts={"acc-1": AccountMeta("USD", "UTC")},
            start=D,
            end=D,
            rows=[],
        )
    session.flush()
    result = rows(session, sync_seed, "material")
    assert len(result) == 1
    vector = result[0].metric_buckets[0]
    for metric in METRIC_COLUMNS:
        if metric in ("revenue_d3", "revenue_d30"):
            assert (
                vector.values[metric] is None
                and vector.availability[metric] == "UNSUPPORTED"
            )
        else:
            assert vector.values[metric] == (Decimal(0) if complete else None)
    assert vector.values["roas_d0"] is None
    assert result[0].coverage["status"] == ("COMPLETE" if complete else "MISSING")


def test_drama_row_partial_when_member_account_incomplete(session, sync_seed):
    # 账户 A 覆盖完整有消耗，账户 B 未同步且无事实：剧行显示 A 的已同步部分并标 PARTIAL。
    for adv, campaign in (("acc-1", "c1"), ("acc-2", "c9")):
        session.add(
            AdObject(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                kind="campaign",
                remote_id=campaign,
                ad_type="SMART_PLUS",
                name="嘉书-同剧",
                observed_at=datetime.now(UTC),
                published_version=1,
            )
        )
        session.add(
            CampaignNameProjection(
                tenant_id=sync_seed.tenant_id,
                advertiser_id=adv,
                campaign_remote_id=campaign,
                name_revision=1,
                raw_name="嘉书-同剧",
                provider_label="嘉书",
                drama_name="同剧",
                status="VALID",
                parser_revision=1,
                grouping_revision=1,
            )
        )
    publish_ad_day(
        session,
        tenant_id=sync_seed.tenant_id,
        job_id=uuid4(),
        accounts={"acc-1": AccountMeta("USD", "UTC")},
        start=D,
        end=D,
        rows=[ad_row("a", D, campaign="c1", spend="10", d0="5")],
    )
    result = build_rows(
        session,
        context=sync_seed.context,
        bc_id="bc-a",
        filters=ReportingFilter(
            dimension="drama",
            start_date=D,
            end_date=D,
            advertiser_ids=("acc-1", "acc-2"),
        ),
    )
    assert len(result) == 1
    (vector,) = result[0].metric_buckets
    assert vector.values["spend"] == Decimal("10")
    assert vector.availability["spend"] == "PARTIAL"
    assert vector.values["roas_d0"] == Decimal("0.5")
    assert vector.availability["roas_d0"] == "PARTIAL"
    assert result[0].coverage["status"] != "COMPLETE"
    assert result[0].coverage["incomplete_advertiser_ids"] == ["acc-2"]
