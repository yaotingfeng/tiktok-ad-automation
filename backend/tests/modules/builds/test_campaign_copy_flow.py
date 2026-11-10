"""真实 PostgreSQL 验证复制冻结结构和原提交执行编译；不调用平台。"""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.ads.models import AdObject
from app.modules.builds import copying, execution, previews, submissions
from app.modules.builds.copy_source import load_copy_source
from app.modules.builds.created_directory import publish_created_object
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildPreview, BuildUnit, PlannedAd
from app.modules.builds.routes import load_preview_route
from app.modules.builds.scene_schemas import ScenePreparation
from tests.modules.builds.test_execution import executable as executable


@pytest.fixture(autouse=True)
def complete_offline_catalog(monkeypatch):
    # 老 execution fixture 的目录页缺少现行完整性计数；补真实缓存证据，
    # 不替换权限/目录读取逻辑，也不发送平台请求。
    from app.modules.builds.scene_job_models import SceneJob
    from tests.modules.builds import test_execution

    original = test_execution.seed_targeting_directory

    def seed(session, context, *args, **kwargs):
        original(session, context, *args, **kwargs)
        for job in session.exec(
            select(SceneJob).where(SceneJob.tenant_id == context.tenant_id)
        ).all():
            if job.status == "COMPLETE" and job.facts.get("identity", {}).get(
                "options"
            ):
                job.facts = {
                    **job.facts,
                    "identity": {
                        **job.facts["identity"],
                        "seen": 1,
                        "total_number": 1,
                        "total_page": 1,
                    },
                }
                session.add(job)
        session.flush()

    monkeypatch.setattr(test_execution, "seed_targeting_directory", seed)


@pytest.fixture
def copy_source(executable, monkeypatch):
    engine, context, ids = executable
    with Session(engine) as session, session.begin():
        root = session.get(ExecutionStep, ids["CAMPAIGN"][0])
        route = load_preview_route(session, context=context, preview_id=root.preview_id)
        frozen = previews.load_frozen_unit(
            session, context=context, unit_id=root.unit_id
        )
        scene = execution.read_scene_context(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            link_id=frozen.link_id,
            route=route,
        )
        monkeypatch.setattr(copying, "read_scene_context", lambda *a, **kw: scene)
        monkeypatch.setattr(
            copying,
            "ensure_scene_preparation",
            lambda *a, **kw: ScenePreparation(None, "ready"),
        )
        for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD"):
            for index, identity in enumerate(ids[kind]):
                step = session.get(ExecutionStep, identity)
                step.status, step.phase, step.operation_status = (
                    "SUCCEEDED",
                    "DONE",
                    "ENABLE",
                )
                step.remote_id = f"source-{kind}-{index}"
                if kind == "CTA":
                    body = {
                        "advertiser_id": "account-A",
                        "creative_portfolio_type": "CTA",
                        "portfolio_content": [
                            {"asset_ids": ["cta-1"], "asset_content": "Watch now"}
                        ],
                    }
                elif kind == "CAMPAIGN":
                    body = {
                        "advertiser_id": "account-A",
                        "campaign_name": "Source current",
                        "objective_type": "APP_PROMOTION",
                        "app_promotion_type": "MINIS",
                        "campaign_type": "REGULAR_CAMPAIGN",
                        "budget": "180",
                        "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
                        "budget_optimize_on": True,
                        "operation_status": "ENABLE",
                    }
                elif kind == "ADGROUP":
                    body = {
                        "advertiser_id": "account-A",
                        "campaign_id": "source-CAMPAIGN-0",
                        "adgroup_name": "Source group",
                        "minis_id": "minis-1",
                        "promotion_type": "MINI_APP",
                        "optimization_goal": "VALUE",
                        "optimization_event": "IMPRESSION_LEVEL_AD_REVENUE",
                        "vbo_window": "ZERO_DAY",
                        "bid_type": "BID_TYPE_NO_BID",
                        "deep_bid_type": "VO_HIGHEST_VALUE",
                        "billing_event": "OCPM",
                        "placement_type": "PLACEMENT_TYPE_NORMAL",
                        "placements": ["PLACEMENT_TIKTOK"],
                        "schedule_type": "SCHEDULE_FROM_NOW",
                        "schedule_start_time": "2026-11-10 00:00:00",
                        "targeting_spec": {
                            "location_ids": ["fixture-location"],
                            "spc_audience_age": "OVER_EIGHTEEN",
                        },
                        "targeting_optimization_mode": "AUTOMATIC",
                        "operation_status": "ENABLE",
                    }
                else:
                    body = {
                        "advertiser_id": "account-A",
                        "adgroup_id": "source-ADGROUP-0",
                        "ad_name": f"Source ad {index}",
                        "operation_status": "ENABLE",
                        "creative_list": [
                            {
                                "creative_info": {
                                    "identity_type": "BC_AUTH_TT",
                                    "identity_id": "identity-1",
                                    "identity_authorized_bc_id": "bc-draft",
                                    "ad_format": "SINGLE_VIDEO",
                                    "video_info": {"video_id": f"target-{index % 2}"},
                                    "image_info": [{"web_uri": f"cover-{index % 2}"}],
                                }
                            }
                        ],
                        "ad_text_list": [{"ad_text": f"平台实际修改文案{index}"}],
                        "landing_page_url_list": [{"landing_page_url": frozen.url}],
                        "ad_configuration": {"call_to_action_id": "source-CTA-0"},
                    }
                step.request_body = body
                session.add(step)
                session.flush()
                publish_created_object(session, step=step, route=route)
        session.flush()
        source = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert source["blockers"] == []
    configuration = {
        "target_advertiser_ids": ["account-A"],
        "copies": 2,
        "budget": None,
        "target_roas": None,
        "targeting": None,
        "delivery_config": {"schedule_mode": "IMMEDIATE"},
        "material_ids": [m["material_id"] for m in source["materials"]],
        "execution_route": route.model_dump(mode="json"),
        "execution_connection_id": str(route.connection_id),
    }
    return engine, context, source, configuration


def test_copy_uses_current_settings_and_text_and_enters_existing_execution(copy_source):
    engine, context, source, configuration = copy_source
    key = uuid4()
    with Session(engine) as session, session.begin():
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=key,
        )
        assert (
            copying.create_copy_preview(
                session,
                context=context,
                bc_id="bc-draft",
                source=source,
                configuration=configuration,
                advertiser_id="account-A",
                ordinal=1,
                request_id=key,
            )
            == preview_id
        )
        assert session.get(BuildPreview, preview_id).status == "FROZEN"
        unit = session.exec(
            select(BuildUnit).where(BuildUnit.preview_id == preview_id)
        ).one()
        ads = session.exec(
            select(PlannedAd).where(PlannedAd.preview_id == preview_id)
        ).all()
        assert {a.text for a in ads} == {
            a["body"]["ad_text_list"][0]["ad_text"]
            for g in source["snapshot"]["groups"]
            for a in g["ads"]
        }
        frozen = previews.load_frozen_unit(session, context=context, unit_id=unit.id)
        execution._current_scene(session, context, frozen)
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview_id, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        campaign = next(s for s in steps if s.kind == "CAMPAIGN")
        body = execution.prepare_request(
            session,
            context=context,
            claim=SimpleNamespace(step_id=campaign.id),
            frozen=frozen,
        )
        assert body["budget"] == "180"
        assert body["operation_status"] == "ENABLE"
        campaign.status, campaign.phase, campaign.remote_id = (
            "SUCCEEDED",
            "DONE",
            "new-parent",
        )
        session.add(campaign)
        session.flush()
        group = next(s for s in steps if s.kind == "ADGROUP")
        body = execution.prepare_request(
            session,
            context=context,
            claim=SimpleNamespace(step_id=group.id),
            frozen=frozen,
        )
        assert body["campaign_id"] == "new-parent"
        assert (
            body["targeting_spec"]
            == source["snapshot"]["groups"][0]["body"]["targeting_spec"]
        )
        assert body["deep_bid_type"] == "VO_HIGHEST_VALUE"
        assert {s.material_id for s in steps if s.kind == "MATERIAL"} == {
            __import__("uuid").UUID(x) for x in configuration["material_ids"]
        }


def test_current_missing_setting_blocks_despite_historical_frozen_source(copy_source):
    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        root = session.get(
            AdObject, (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0")
        )
        body = dict(root.configuration)
        del body["objective_type"]
        root.configuration = body
        session.add(root)
        session.flush()
        source = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert source["blockers"] and not source["snapshot"]


def test_material_filter_and_multiple_copies_do_not_merge(copy_source):
    engine, context, source, configuration = copy_source
    configuration = {
        **configuration,
        "material_ids": configuration["material_ids"][:1],
        "budget": "225",
    }
    with Session(engine) as session, session.begin():
        previews_ids = [
            copying.create_copy_preview(
                session,
                context=context,
                bc_id="bc-draft",
                source=source,
                configuration=configuration,
                advertiser_id="account-A",
                ordinal=n,
                request_id=uuid4(),
            )
            for n in (1, 2)
        ]
        assert len(set(previews_ids)) == 2
        units = session.exec(
            select(BuildUnit).where(BuildUnit.preview_id.in_(previews_ids))
        ).all()
        assert len({u.campaign_name for u in units}) == 2
        assert all(session.get(BuildPreview, u.preview_id).budget == 225 for u in units)
        expected_ads = sum(
            bool(set(ad["material_ids"]) & set(configuration["material_ids"]))
            for group in source["snapshot"]["groups"]
            for ad in group["ads"]
        )
        assert all(u.ad_count == expected_ads for u in units)


def test_source_change_invalidates_copy_and_other_account_cannot_be_injected(
    copy_source,
):
    engine, context, source, configuration = copy_source
    with Session(engine) as session, session.begin():
        with pytest.raises(DomainError) as error:
            copying.create_copy_preview(
                session,
                context=context,
                bc_id="bc-draft",
                source=source,
                configuration=configuration,
                advertiser_id="not-selected",
                ordinal=1,
                request_id=uuid4(),
            )
        assert error.value.code == "copy_scope_mismatch"
        root = session.get(
            AdObject, (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0")
        )
        root.configuration = {**root.configuration, "budget": "999"}
        session.add(root)
        session.flush()
        with pytest.raises(DomainError) as error:
            copying.create_copy_preview(
                session,
                context=context,
                bc_id="bc-draft",
                source=source,
                configuration=configuration,
                advertiser_id="account-A",
                ordinal=1,
                request_id=uuid4(),
            )
        assert error.value.code == "copy_source_changed"


def test_pending_preparation_persists_draft_and_reentry_creates_only_one_preview(
    copy_source, monkeypatch
):
    from app.modules.builds.models import BuildDraft

    engine, context, source, configuration = copy_source
    request_id = uuid4()
    monkeypatch.setattr(
        copying,
        "ensure_scene_preparation",
        lambda *a, **kw: ScenePreparation(None, "queued"),
    )
    with Session(engine) as session, session.begin():
        with pytest.raises(DomainError) as error:
            copying.create_copy_preview(
                session,
                context=context,
                bc_id="bc-draft",
                source=source,
                configuration=configuration,
                advertiser_id="account-A",
                ordinal=1,
                request_id=request_id,
            )
        assert error.value.code == "copy_preparing"
    with Session(engine) as session, session.begin():
        draft = session.exec(
            select(BuildDraft).where(BuildDraft.request_id == request_id)
        ).one()
        assert draft.status == "PREPARING"
        assert not session.exec(
            select(BuildPreview).where(BuildPreview.draft_id == draft.id)
        ).all()
        monkeypatch.setattr(
            copying,
            "ensure_scene_preparation",
            lambda *a, **kw: ScenePreparation(None, "ready"),
        )
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=request_id,
        )
        assert session.get(BuildPreview, preview_id).status == "FROZEN"
        assert (
            len(
                session.exec(
                    select(BuildPreview).where(BuildPreview.draft_id == draft.id)
                ).all()
            )
            == 1
        )


def test_cross_account_ad_uses_target_vid_and_inherited_current_copy(copy_source):
    from datetime import UTC, datetime

    from app.modules.materials.models import AccountMaterial
    from tests.modules.builds.test_drafts import account

    engine, context, source, configuration = copy_source
    with Session(engine) as session, session.begin():
        account(session, context, "account-B")
        for old in session.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.advertiser_id == "account-A",
            )
        ).all():
            session.add(
                AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id="bc-draft",
                    material_id=old.material_id,
                    advertiser_id="account-B",
                    connection_id=old.connection_id,
                    video_id="B-" + old.video_id,
                    image_id="B-" + old.image_id,
                    status="available",
                    verified_at=datetime.now(UTC),
                )
            )
        session.flush()
        configuration = {**configuration, "target_advertiser_ids": ["account-B"]}
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-B",
            ordinal=1,
            request_id=uuid4(),
        )
        unit = session.exec(
            select(BuildUnit).where(BuildUnit.preview_id == preview_id)
        ).one()
        frozen = previews.load_frozen_unit(session, context=context, unit_id=unit.id)
        route = load_preview_route(session, context=context, preview_id=preview_id)
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview_id, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        steps = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        for step in steps:
            if step.kind in {"CTA", "CAMPAIGN", "ADGROUP"}:
                step.status, step.phase, step.remote_id = (
                    "SUCCEEDED",
                    "DONE",
                    "B-" + step.kind,
                )
                session.add(step)
        session.flush()
        ad = next(step for step in steps if step.kind == "AD")
        claim = SimpleNamespace(
            step_id=ad.id, advertiser_id="account-B", bc_id="bc-draft", route=route
        )
        body = execution.prepare_request(
            session, context=context, claim=claim, frozen=frozen
        )
        assert body["advertiser_id"] == "account-B"
        assert body["adgroup_id"] == "B-ADGROUP"
        assert body["ad_text_list"][0]["ad_text"].startswith("平台实际修改文案")
        assert all(
            entry["creative_info"]["video_info"]["video_id"].startswith("B-target-")
            for entry in body["creative_list"]
        )
        summary = previews.get_preview_summary(
            session, context=context, preview_id=preview_id
        )
        assert summary.currency == "USD" and summary.daily_budget_sum == 180
        submission = submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        )
        assert submission.daily_budget_sum == "180"


@pytest.mark.parametrize("refresh_observation", [False, True])
def test_parent_copy_batch_drives_two_children_and_idempotent_submit(
    copy_source, monkeypatch, refresh_observation
):
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.modules.builds.execution_models import Submission
    from app.modules.campaign_copies import service, tasks
    from app.modules.campaign_copies.models import CampaignCopyDraft
    from app.modules.campaign_copies.schemas import (
        CopyPatchRequest,
        CopyRevisionRequest,
        CopySubmitRequest,
    )

    engine, context, source, configuration = copy_source
    monkeypatch.setattr(
        service,
        "get_frozen_selection",
        lambda *a, **kw: SimpleNamespace(
            refs=(
                EntityRef(
                    context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0"
                ),
            ),
            material_uses=(),
        ),
    )
    with Session(engine) as session, session.begin():
        original = session.get(
            AdObject, (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0")
        )
        original_body = deepcopy(original.configuration)
        draft = service.create(
            session,
            context=context,
            bc_id="bc-draft",
            selection_id=uuid4(),
            request_id=uuid4(),
        )
        draft_id = draft.id
        config = {
            k: v
            for k, v in configuration.items()
            if k not in {"execution_route", "execution_connection_id"}
        }
        draft = service.patch(
            session,
            context=context,
            draft_id=draft_id,
            body=CopyPatchRequest(
                request_id=uuid4(),
                expected_revision=draft.revision,
                rows=[{"row_id": draft.rows[0]["row_id"], "configuration": config}],
            ),
        )
        service.preview(
            session,
            context=context,
            draft_id=draft_id,
            body=CopyRevisionRequest(
                request_id=uuid4(), expected_revision=draft.revision
            ),
        )
    for _ in range(5):
        with Session(engine) as session:
            draft = session.get(CampaignCopyDraft, draft_id)
            if draft.status == "READY":
                break
            assert draft.status == "PREPARING", draft.error_code
            payload = {"draft_id": str(draft_id), "generation": draft.generation}
        tasks.process(
            database_engine=engine,
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
    with Session(engine) as session, session.begin():
        draft = session.get(CampaignCopyDraft, draft_id)
        assert draft.status == "READY", draft.error_code
        assert len({c["preview_id"] for c in draft.children}) == 2
        if refresh_observation:
            from datetime import timedelta
            from uuid import UUID

            # 模拟普通同步只推进目录水位；READY子预览和父批次都应继续可提交。
            for obj in session.exec(
                select(AdObject).where(
                    AdObject.tenant_id == context.tenant_id,
                    AdObject.advertiser_id == "account-A",
                )
            ).all():
                obj.published_version += 1
                obj.observed_at += timedelta(seconds=30)
                session.add(obj)
            session.flush()
            refreshed = load_copy_source(
                session,
                context=context,
                bc_id="bc-draft",
                advertiser_id="account-A",
                campaign_id="source-CAMPAIGN-0",
            )
            assert refreshed == source
            direct = submissions.submit_preview(
                session,
                context=context,
                preview_id=UUID(draft.children[0]["preview_id"]),
                request_id=uuid4(),
            )
            assert direct.submission_id is not None
        body = CopySubmitRequest(
            request_id=uuid4(),
            expected_revision=draft.revision,
            preview_digest=draft.preview_digest,
        )
        submitted = service.submit(
            session, context=context, draft_id=draft_id, body=body
        )
        submission_ids = {c["submission_id"] for c in submitted.children}
        assert len(submission_ids) == 2
        assert (
            service.submit(session, context=context, draft_id=draft_id, body=body).id
            == draft_id
        )
        assert (
            len(
                session.exec(
                    select(Submission).where(
                        Submission.preview_id.in_(
                            [c["preview_id"] for c in draft.children]
                        )
                    )
                ).all()
            )
            == 2
        )
        assert (
            session.get(
                AdObject,
                (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0"),
            ).configuration
            == original_body
        )


def test_independent_group_budgets_have_exact_preview_and_submission_totals(
    copy_source, monkeypatch
):
    from dataclasses import replace

    from app.modules.builds.preview_catalog import get_preview_dramas

    engine, context, source, configuration = copy_source
    with Session(engine) as session, session.begin():
        root = session.get(
            AdObject, (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0")
        )
        root.configuration = {
            **root.configuration,
            "budget": "0",
            "budget_mode": "BUDGET_MODE_INFINITE",
            "budget_optimize_on": False,
            "smart_plus_adgroup_mode": "MULTIPLE",
        }
        group = session.get(
            AdObject, (context.tenant_id, "account-A", "adgroup", "source-ADGROUP-0")
        )
        group.configuration = {
            **group.configuration,
            "budget": "60",
            "budget_mode": "BUDGET_MODE_DYNAMIC_DAILY_BUDGET",
        }
        second = AdObject(
            **{
                **group.model_dump(),
                "remote_id": "source-second-group",
                "name": "Second current group",
                "configuration": {
                    **group.configuration,
                    "adgroup_id": "source-second-group",
                    "adgroup_name": "Second current group",
                    "budget": "83",
                },
            }
        )
        session.add_all([root, group, second])
        moved_ad = session.exec(
            select(AdObject)
            .where(
                AdObject.tenant_id == context.tenant_id,
                AdObject.advertiser_id == "account-A",
                AdObject.kind == "ad",
            )
            .order_by(AdObject.remote_id)
        ).first()
        moved_ad.parent_remote_id = "source-second-group"
        moved_ad.configuration = {
            **moved_ad.configuration,
            "adgroup_id": "source-second-group",
        }
        session.add(moved_ad)
        session.flush()
        updated_source = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert updated_source["blockers"] == []
        assert updated_source["budget"] is None
        base_scene = copying.read_scene_context(session)
        scene = replace(
            base_scene,
            field_constraints={
                **base_scene.field_constraints,
                "adgroup_daily_budget": dict(
                    base_scene.field_constraints["campaign_daily_budget"]
                ),
            },
        )
        monkeypatch.setattr(copying, "read_scene_context", lambda *a, **kw: scene)
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=updated_source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=uuid4(),
        )
        summary = previews.get_preview_summary(
            session, context=context, preview_id=preview_id
        )
        assert summary.adgroup_count == 2 and summary.daily_budget_sum == 143
        drama_summary = get_preview_dramas(
            session, context=context, preview_id=preview_id
        )
        assert drama_summary.items[0].daily_budget_sum == 143
        units = previews.get_preview_units(
            session, context=context, preview_id=preview_id
        )
        assert units.items[0].budget == 143
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview_id, request_id=uuid4()
        )
        report = submissions.get_submission(
            session, context=context, submission_id=receipt.submission_id
        )
        assert report.daily_budget_sum == "143"
        unit = session.exec(
            select(BuildUnit).where(BuildUnit.preview_id == preview_id)
        ).one()
        assert {
            g["budget"] for g in unit.scene_snapshot["campaign_copy"]["groups"].values()
        } == {"60", "83"}


def test_direct_child_submit_rechecks_ready_source(copy_source):
    engine, context, source, configuration = copy_source
    with Session(engine) as session, session.begin():
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=uuid4(),
        )
        root = session.get(
            AdObject, (context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0")
        )
        root.configuration = {**root.configuration, "budget": "999"}
        session.add(root)
        session.flush()
        with pytest.raises(DomainError) as error:
            submissions.submit_preview(
                session, context=context, preview_id=preview_id, request_id=uuid4()
            )
        assert error.value.code == "copy_source_changed"


def test_direct_child_submit_rejects_revoked_source_authorization(copy_source):
    from uuid import UUID

    from app.modules.accounts.models import TikTokConnection

    engine, context, source, configuration = copy_source
    with Session(engine) as session, session.begin():
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=source,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=uuid4(),
        )
        connection = session.get(
            TikTokConnection, UUID(source["snapshot"]["source_route"]["connection_id"])
        )
        connection.authorization_revision += 1
        session.add(connection)
        session.flush()
        with pytest.raises(DomainError):
            submissions.submit_preview(
                session, context=context, preview_id=preview_id, request_id=uuid4()
            )


def test_custom_source_cover_is_blocked_instead_of_replaced(copy_source):
    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        ad = session.exec(
            select(AdObject).where(
                AdObject.tenant_id == context.tenant_id,
                AdObject.advertiser_id == "account-A",
                AdObject.kind == "ad",
            )
        ).first()
        body = deepcopy(ad.configuration)
        body["creative_list"][0]["creative_info"]["image_info"] = [
            {"web_uri": "custom-cover"}
        ]
        ad.configuration = body
        session.add(ad)
        session.flush()
        current = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert any("封面" in message for message in current["blockers"])
        assert not current["snapshot"]


def test_actual_source_targeting_and_child_group_settings_are_visible(copy_source):
    engine, context, _, configuration = copy_source
    with Session(engine) as session, session.begin():
        group = session.get(
            AdObject, (context.tenant_id, "account-A", "adgroup", "source-ADGROUP-0")
        )
        group.configuration = {
            **group.configuration,
            "targeting_spec": {
                "location_ids": ["fixture-location"],
                "languages": ["en"],
                "spc_audience_age": "OVER_TWENTY_FIVE",
            },
        }
        session.add(group)
        session.flush()
        current = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert current["blockers"] == []
        assert not current["targeting_mixed"]
        assert current["targeting"]["region_codes"] == ["US"]
        assert current["targeting"]["languages"] == ["en"]
        assert "AGE_18_24" not in current["targeting"]["age_groups"]
        preview_id = copying.create_copy_preview(
            session,
            context=context,
            bc_id="bc-draft",
            source=current,
            configuration=configuration,
            advertiser_id="account-A",
            ordinal=1,
            request_id=uuid4(),
        )
        summary = previews.get_preview_summary(
            session, context=context, preview_id=preview_id
        )
        assert summary.copy_group_settings[0]["targeting"] == current["targeting"]
        saved = session.get(BuildPreview, preview_id)
        assert (
            saved.config["copy_group_settings"][0]["targeting"] == current["targeting"]
        )
