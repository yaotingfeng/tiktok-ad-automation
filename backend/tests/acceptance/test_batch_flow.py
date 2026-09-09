from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.modules.builds import previews
from app.modules.materials.models import AccountMaterial


@pytest.mark.parametrize("acceptance_scenario", ["jiashu", "wangyan"], indirect=True)
def test_real_preparation_freeze_submission_and_official_sdk(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
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
    assert Decimal(view.daily_budget_sum) == Decimal("600")
    wire = scenario.runtime.wire
    assert len([call for call in wire.smart.calls if call["method"] == "POST"]) == 60
    assert all(
        row["operation_status"] == "ENABLE"
        for rows in wire.smart.store.values()
        for row in rows.values()
    )
    assert wire.calls["/open_api/v1.3/file/video/ad/upload/"] == 138
    assert wire.calls["/open_api/v1.3/file/image/ad/upload/"] == 138
    assert wire.calls["/open_api/v1.3/file/image/ad/info/"] >= 138
    assert wire.calls["/open_api/v1.3/adgroup/get/"] >= 18
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
    from collections import defaultdict

    from app.modules.builds.preview_models import BuildUnit, PlannedAd, PlannedGroup
    from app.modules.materials.models import MaterialUploadAttempt

    with Session(scenario.database_engine) as session:
        rows = session.exec(
            select(BuildUnit, PlannedGroup, PlannedAd)
            .join(PlannedGroup, PlannedGroup.unit_id == BuildUnit.id)
            .join(PlannedAd, PlannedAd.group_id == PlannedGroup.id)
            .where(BuildUnit.preview_id == preview_id)
        ).all()
        copies = defaultdict(set)
        for unit, group, ad in rows:
            copies[(unit.drama_id, group.group_no, ad.creative_no)].add(
                (ad.copy_id, ad.text)
            )
        assert len(copies) == 12 and all(len(values) == 1 for values in copies.values())
        history = session.exec(
            select(MaterialUploadAttempt).where(
                MaterialUploadAttempt.tenant_id == scenario.scope.context.tenant_id
            )
        ).all()
        assert len(history) == 46
        assert {row.advertiser_id for row in history} == set(scenario.scope.sources)
        assert all(
            row.status == "succeeded"
            and row.connection_id == scenario.scope.connection_id
            for row in history
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


@pytest.mark.parametrize("variant", ["currency", "minis", "multi_drama"])
def test_partial_input_and_material_matching_use_actual_preparation(
    acceptance_scenario, variant
):
    from app.modules.accounts.models import AdvertiserAccount
    from app.modules.builds.preview_models import (
        BuildUnit,
        PreviewDrama,
        PreviewGroupMaterial,
    )
    from app.modules.materials.models import MaterialFile

    scenario = acceptance_scenario
    if variant == "currency":
        with Session(scenario.database_engine) as session, session.begin():
            row = session.get(
                AdvertiserAccount,
                (scenario.scope.context.tenant_id, scenario.scope.accounts[0]),
            )
            row.currency = "EUR"
            session.add(row)
    elif variant == "minis":
        scenario.runtime.wire.minis_unavailable_accounts.add(scenario.scope.accounts[0])
    else:
        with Session(scenario.database_engine) as session, session.begin():
            row = session.get(MaterialFile, scenario.scope.material_ids[0])
            row.file_name = "The Bond Hidden Promise.mp4"
            session.add(row)
    scenario.prepare()
    preview_id = scenario.freeze()
    with Session(scenario.database_engine) as session:
        summary = previews.get_preview_summary(
            session, context=scenario.scope.context, preview_id=preview_id
        )
        if variant in {"currency", "minis"}:
            assert (
                summary.campaign_count,
                summary.adgroup_count,
                summary.ad_count,
            ) == (4, 12, 24)
            assert summary.daily_budget_sum == Decimal("400")
            if variant == "minis":
                blocked = session.exec(
                    select(BuildUnit).where(
                        BuildUnit.preview_id == preview_id,
                        BuildUnit.readiness == "BLOCKED",
                    )
                ).all()
                assert len(blocked) == 2
                assert all(
                    row.advertiser_id == scenario.scope.accounts[0]
                    and "minis_unavailable" in row.reason_codes
                    for row in blocked
                )
        else:
            assert summary.campaign_count == 6 and summary.input_issue_count == 0
            rows = session.exec(
                select(PreviewGroupMaterial, PreviewDrama)
                .join(
                    PreviewDrama,
                    (PreviewDrama.preview_id == PreviewGroupMaterial.preview_id)
                    & (PreviewDrama.drama_id == PreviewGroupMaterial.drama_id),
                )
                .where(
                    PreviewGroupMaterial.preview_id == preview_id,
                    PreviewGroupMaterial.material_id == scenario.scope.material_ids[0],
                )
            ).all()
            assert {drama.title for _, drama in rows} == {"The Bond", "Hidden Promise"}
    scenario.submit()
    submitted = scenario.view()
    if variant in {"currency", "minis"}:
        assert submitted.submitted.model_dump() == {
            "campaign_count": 4,
            "adgroup_count": 12,
            "ad_count": 24,
        }
        assert Decimal(submitted.daily_budget_sum) == Decimal("400")
        assert submitted.excluded_unit_count == 2
        assert submitted.excluded.model_dump() == {
            "campaign_count": 2,
            "adgroup_count": 6,
            "ad_count": 12,
        }
        for name, planned_count in submitted.planned.model_dump().items():
            assert planned_count == getattr(submitted.submitted, name) + getattr(
                submitted.excluded, name
            )
    else:
        assert submitted.submitted.campaign_count == 6
    assert not scenario.runtime.wire.smart.calls
    assert not scenario.runtime.wire.videos
