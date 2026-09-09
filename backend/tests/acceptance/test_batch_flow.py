from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.modules.builds import previews
from app.modules.builds.models import DraftPreparation
from app.modules.materials.models import AccountMaterial
from app.modules.providers.models import LinkPreparationItem


@pytest.mark.parametrize("acceptance_scenario", ["jiashu", "wangyan"], indirect=True)
def test_real_preparation_freeze_submission_and_official_sdk(acceptance_scenario):
    scenario = acceptance_scenario
    prep_id = scenario.prepare()
    with Session(scenario.database_engine) as session:
        prep = session.get(DraftPreparation, prep_id)
        if (
            scenario.scope.provider_id
            and scenario.runtime.wire.calls["provider:/api/distribute_admin/drama/list"]
        ):
            items = session.exec(
                select(LinkPreparationItem).where(
                    LinkPreparationItem.tenant_id == scenario.scope.context.tenant_id
                )
            ).all()
            assert items and all(
                item.resolved.get("error_code") == "lookup_incomplete" for item in items
            )
            assert not scenario.runtime.wire.smart.calls
            assert prep.status in {"READY", "BLOCKED"}
            return
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        preview = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        assert preview.status == "FROZEN"
        assert (preview.campaign_count, preview.adgroup_count, preview.ad_count) == (
            6,
            18,
            36,
        )
        assert preview.daily_budget_sum == Decimal("600")
        assert not scenario.runtime.wire.smart.calls
    scenario.submit()
    scenario.runtime.drive_until(
        lambda: scenario.view().status not in {"QUEUED", "RUNNING"}
    )
    view = scenario.view()
    assert view.succeeded.model_dump() == {
        "campaign_count": 6,
        "adgroup_count": 18,
        "ad_count": 36,
    }, scenario.runtime.diagnostics()
    assert view.daily_budget_sum == "600.00"
    wire = scenario.runtime.wire
    assert len([call for call in wire.smart.calls if call["method"] == "POST"]) == 60
    assert all(
        row["operation_status"] == "ENABLE"
        for rows in wire.smart.store.values()
        for row in rows.values()
    )
    assert wire.calls["/open_api/v1.3/file/video/ad/upload/"] == 138
    with Session(scenario.database_engine) as session:
        targets = session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == scenario.scope.context.tenant_id,
                AccountMaterial.advertiser_id.in_(scenario.scope.accounts),
            )
        ).all()
        assert len(targets) == 138
        assert all(
            row.video_id.startswith("target-vid-")
            and row.mid.startswith("target-mid-")
            and row.image_id
            for row in targets
        )


def test_preparation_and_freeze_are_real_and_have_no_tiktok_writes(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        summary = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        assert summary.status == "FROZEN"
        assert (summary.campaign_count, summary.adgroup_count, summary.ad_count) == (
            6,
            18,
            36,
        )
        assert summary.daily_budget_sum == Decimal("600")
        assert summary.blocked_count == 0
        assert summary.preparing_count == 6
        assert (
            session.exec(
                select(AccountMaterial).where(
                    AccountMaterial.tenant_id == scenario.scope.context.tenant_id,
                    AccountMaterial.advertiser_id.in_(scenario.scope.accounts),
                )
            ).all()
            == []
        )
    wire = scenario.runtime.wire
    for endpoint in [
        "identity/get",
        "minis/get",
        "creative/cta/recommend",
        "tool/vbo_status",
        "tool/region",
    ]:
        assert wire.calls[f"/open_api/v1.3/{endpoint}/"] == 3
    assert wire.calls["/open_api/v1.3/bc/asset/get/"] == 1
    assert not wire.smart.calls
    assert not wire.videos and not wire.portfolios
