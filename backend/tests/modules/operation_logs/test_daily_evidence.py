from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlmodel import select

from app.modules.accounts.models import TikTokConnection
from app.modules.ads.models import AdObject
from app.modules.operation_logs.evidence import build_evidence, frozen_sources
from tests.modules.operation_logs.test_parser import export
from tests.modules.operation_logs.test_store import publish


def anchors(session, case, observed_at=datetime(2026, 11, 2, tzinfo=UTC)):
    for kind, remote, parent, config in [
        (
            "campaign",
            "campaign-test",
            None,
            {
                "ad_type": "SMART_PLUS",
                "app_promotion_type": "MINIS",
                "budget_optimize_on": True,
            },
        ),
        (
            "adgroup",
            "group-test",
            "campaign-test",
            {
                "promotion_type": "MINI_APP",
                "optimization_goal": "VALUE",
                "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                "vbo_window": "ZERO_DAY",
                "deep_bid_type": "VO_MIN_ROAS",
                "roas_bid": "1.2",
            },
        ),
    ]:
        connection = session.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == case.context.tenant_id
            )
        ).first()
        session.add(
            AdObject(
                tenant_id=case.context.tenant_id,
                advertiser_id="report-account",
                kind=kind,
                remote_id=remote,
                parent_kind="campaign" if parent else None,
                parent_remote_id=parent,
                ad_type="SMART_PLUS",
                name="synthetic",
                observed_at=observed_at,
                configuration=config,
                published_version=1,
                source_connection_id=connection.id,
                source_channel="OFFICIAL_API",
            )
        )
    session.flush()  # 真实目录写入触发不可变观察，无人工插入观察表。


def calculate(session, case, frozen, *, timezone="UTC", day=date(2026, 11, 1)):
    return build_evidence(
        session,
        tenant_id=case.context.tenant_id,
        advertiser_id="report-account",
        campaign_id="campaign-test",
        day=day,
        timezone=timezone,
        frozen=frozen,
        spend=Decimal(100),
        revenue=Decimal(60),
        events=Decimal(300),
    )


def test_empty_logs_with_real_anchor_can_estimate_and_freeze(session, report_case):
    publish(session, report_case, export())
    anchors(session, report_case)
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    result = calculate(session, report_case, frozen)
    assert result.data["amount"] == "50"
    assert result.data["status"] == "ESTIMATED_PENDING"
    assert calculate(session, report_case, frozen).id == result.id
    # 同内容重抓不会制造修订；冻结ID仍能回读原文和事件。
    publish(session, report_case, export())
    again = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    assert again["signature"] == frozen["signature"]
    assert calculate(session, report_case, frozen).data == result.data


def test_pause_then_resume_is_excluded_and_old_evidence_unchanged(session, report_case):
    publish(session, report_case, export())
    anchors(session, report_case)
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    prior = calculate(session, report_case, frozen)
    changes = [
        {
            "action": "Change ",
            "name": "Campaign",
            "before_after": [{"before": "Enabled", "after": "Paused"}],
        },
        {
            "action": "Change ",
            "name": "Campaign",
            "before_after": [{"before": "Paused", "after": "Enabled"}],
        },
    ]
    publish(session, report_case, export(changes))
    current = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    after = calculate(session, report_case, current)
    assert after.data["amount"] == "0"
    assert "CAMPAIGN_PAUSED" in after.data["reasons"]
    assert prior.data["amount"] == "50"
    assert calculate(session, report_case, frozen).data["amount"] == "50"


def test_missing_anchor_does_not_use_default_bid(session, report_case):
    publish(session, report_case, export())
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    result = calculate(session, report_case, frozen)
    assert result.data["amount"] is None
    assert "CONFIGURATION_ANCHOR_MISSING" in result.data["reasons"]


def test_future_group_does_not_reduce_old_day_bid(session, report_case):
    case = report_case
    content = export(
        records=[
            (
                "2026-11-02 08:02",
                "Ad group",
                "group-future",
                [{"action": "Create", "name": "Ad group. Status: Paused"}],
            )
        ]
    )
    publish(
        session, case, content, datetime(2026, 11, 4, tzinfo=UTC), end=date(2026, 11, 3)
    )
    anchors(session, case, datetime(2026, 11, 4, tzinfo=UTC))
    group = session.get(
        AdObject, (case.context.tenant_id, "report-account", "adgroup", "group-test")
    )
    session.add(
        AdObject(
            **{
                **group.model_dump(),
                "remote_id": "group-future",
                "configuration": {**group.configuration, "roas_bid": "0.8"},
            }
        )
    )
    session.flush()
    frozen = frozen_sources(
        session,
        case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 3),
    )
    assert calculate(session, case, frozen).data["amount"] == "50"
    assert (
        calculate(session, case, frozen, day=date(2026, 11, 2)).data["amount"] == "25"
    )


@pytest.mark.parametrize(
    "detail,reason",
    [
        ({"action": "Change", "name": "Campaign"}, "STATUS_VALUE_UNKNOWN"),
        (
            {
                "action": "Change",
                "name": "Campaign",
                "before_after": [{"before": "Enabled", "after": "new-unknown-status"}],
            },
            "STATUS_VALUE_UNKNOWN",
        ),
        ({"action": "Change", "name": "Budget"}, "BUDGET_VALUE_UNKNOWN"),
    ],
)
def test_corrupt_known_fields_cannot_produce_positive_amount(
    session, report_case, detail, reason
):
    publish(session, report_case, export([detail]))
    anchors(session, report_case)
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    result = calculate(session, report_case, frozen)
    assert result.data["amount"] is None
    assert reason in result.data["reasons"]


def test_other_day_budget_does_not_contaminate_target_day(session, report_case):
    content = export(
        records=[
            (
                "2026-11-02 08:02",
                "Campaign",
                "campaign-test",
                [
                    {
                        "action": "Change",
                        "name": "Daily budget",
                        "before_after": [{"before": "100 USD", "after": "200 USD"}],
                    }
                ],
            )
        ]
    )
    publish(
        session,
        report_case,
        content,
        datetime(2026, 11, 3, tzinfo=UTC),
        end=date(2026, 11, 2),
    )
    anchors(session, report_case, datetime(2026, 11, 3, tzinfo=UTC))
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 2),
    )
    assert calculate(session, report_case, frozen).data["amount"] == "50"


def test_western_local_day_can_use_bidirectional_anchor(session, report_case):
    publish(
        session,
        report_case,
        export(),
        datetime(2026, 11, 3, tzinfo=UTC),
        timezone="America/Los_Angeles",
    )
    anchors(session, report_case)
    frozen = frozen_sources(
        session,
        report_case.context.tenant_id,
        ["report-account"],
        date(2026, 11, 1),
        date(2026, 11, 1),
    )
    assert (
        calculate(session, report_case, frozen, timezone="America/Los_Angeles").data[
            "amount"
        ]
        == "50"
    )


def test_empty_tail_extension_changes_evidence_only_until_anchor_covered(
    session, report_case
):
    case = report_case
    publish(session, case, export())
    anchors(session, case, datetime(2026, 11, 2, 0, 1, tzinfo=UTC))

    def snapshot():
        return frozen_sources(
            session,
            case.context.tenant_id,
            ["report-account"],
            date(2026, 11, 1),
            date(2026, 11, 1),
        )

    before = snapshot()
    assert calculate(session, case, before).data["amount"] is None
    publish(
        session,
        case,
        export(),
        datetime(2026, 11, 2, 12, 1, tzinfo=UTC),
        tail_end=datetime(2026, 11, 2, 0, 2, tzinfo=UTC),
    )
    after = snapshot()
    assert after["signature"] != before["signature"]
    assert calculate(session, case, after).data["amount"] == "50"
    publish(
        session,
        case,
        export(),
        datetime(2026, 11, 2, 12, 2, tzinfo=UTC),
        tail_end=datetime(2026, 11, 2, 0, 3, tzinfo=UTC),
    )
    assert snapshot()["signature"] == after["signature"]
