from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlmodel import select

from app.modules.ai_workbench.attribution import (
    build_attribution_pack,
    resolve_mappings,
)
from app.modules.ai_workbench.models import AIOptimizer, BuildOccurrence
from app.modules.ai_workbench.plans import AIDailyPlan, AIDailyPlanRow
from app.modules.ai_workbench.review_data import account_window
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.models import BuildDraft
from app.modules.builds.preview_models import BuildPreview, BuildUnit
from tests.modules.builds.test_drafts import intent as intent
from tests.modules.builds.test_previews import prepared as prepared
from tests.modules.builds.test_submission_queries import expanded
from tests.modules.builds.test_submissions import frozen as frozen


def mapping_case(session, context, frozen):
    submission = expanded(session, context, frozen)
    preview = session.get(BuildPreview, frozen)
    draft = session.get(BuildDraft, preview.draft_id)
    optimizer = AIOptimizer(
        tenant_id=context.tenant_id, name="evidence", created_by=context.actor_id
    )
    session.add(optimizer)
    session.flush()
    now = datetime.now(UTC)
    plan = AIDailyPlan(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        business_date=now.date(),
        created_by=context.actor_id,
        updated_by=context.actor_id,
    )
    session.add(plan)
    session.flush()
    row = AIDailyPlanRow(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        plan_id=plan.id,
        business_date=now.date(),
        scheduled_time=now.time().replace(tzinfo=None),
        position=0,
        title="Moon",
        provider_connection_id=draft.provider_connection_id,
        application_id=draft.application_id,
    )
    session.add(row)
    session.flush()
    occurrence = BuildOccurrence(
        tenant_id=context.tenant_id,
        optimizer_id=optimizer.id,
        plan_row_id=row.id,
        request_id=uuid4(),
        request_digest="a" * 64,
        strategy_version_id=draft.strategy_version_id,
        bc_id="bc-draft",
        advertiser_ids=["A"],
        provider_connection_id=draft.provider_connection_id,
        application_id=draft.application_id,
        title="Moon",
        scheduled_at=now,
        submission_id=submission,
        status="COMPLETE",
    )
    session.add(occurrence)
    session.flush()
    step = session.exec(
        select(ExecutionStep)
        .join(
            BuildUnit,
            (BuildUnit.id == ExecutionStep.unit_id)
            & (BuildUnit.tenant_id == ExecutionStep.tenant_id),
        )
        .where(
            ExecutionStep.submission_id == submission,
            ExecutionStep.kind == "CAMPAIGN",
            BuildUnit.advertiser_id == "A",
        )
    ).first()
    step.status = "SUCCEEDED"
    step.remote_id = "actual-created-campaign"
    session.flush()
    version = SimpleNamespace(bc_id="bc-draft", advertiser_ids=["A"])
    return optimizer, occurrence, version


def test_strategy_mapping_uses_frozen_execution_ids_and_scoped_report(
    session, context, frozen
):
    from app.modules.reporting.publish import AccountMeta, publish_ad_day
    from tests.modules.reporting.sync_conftest import ad_row

    optimizer, occurrence, version = mapping_case(session, context, frozen)
    now = datetime.now(UTC)
    day = now.date() - timedelta(days=1)
    publish_ad_day(
        session,
        tenant_id=context.tenant_id,
        job_id=uuid4(),
        accounts={"A": AccountMeta("USD", "UTC")},
        start=day,
        end=day,
        rows=[
            ad_row(
                "actual-ad",
                day,
                campaign="actual-created-campaign",
                adv="A",
                spend="10",
            ),
            ad_row("manual-ad", day, campaign="manual-campaign", adv="A", spend="50"),
        ],
    )
    window = {**account_window("UTC", now), "advertiser_id": "A"}
    pack = build_attribution_pack(
        session,
        context=context,
        optimizer_id=optimizer.id,
        version=version,
        cutoff=datetime.now(UTC),
        account_windows=[window],
    )
    row = pack["rows"][0]
    assert row["remote_ids"] == ["actual-created-campaign"]
    assert row["version_id"] == str(occurrence.strategy_version_id)
    assert row["reports"][0]["rows"][0]["metric_buckets"][0]["values"]["spend"] == "10"
    assert all(
        ref["remote_id"] != "manual-campaign"
        for report in row["reports"]
        for item in report["rows"]
        for ref in item["refs"]
    )
    assert not resolve_mappings(
        session, context=context, optimizer_id=uuid4(), version=version, cutoff=now
    )["rows"]
    # 冻结副本不随之后的数据刷新变化。
    occurrence.title = "renamed"
    assert row["remote_ids"] == ["actual-created-campaign"]
