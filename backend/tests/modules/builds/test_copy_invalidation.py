"""复制父草稿修订及来源CTA核验的真实数据库回归。"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.builds import submissions
from app.modules.builds.copy_source import load_copy_source
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.preview_models import BuildPreview
from app.modules.campaign_copies import service, tasks
from app.modules.campaign_copies.models import CampaignCopyDraft
from app.modules.campaign_copies.schemas import (
    CopyPatchRequest,
    CopyRevisionRequest,
    CopySubmitRequest,
)
from tests.modules.builds.test_campaign_copy_flow import (
    complete_offline_catalog as complete_offline_catalog,
)
from tests.modules.builds.test_campaign_copy_flow import (
    copy_source as copy_source,
)
from tests.modules.builds.test_campaign_copy_flow import (
    executable as executable,
)


def start_batch(copy_source, monkeypatch, copies=1):
    engine, context, _, _ = copy_source
    monkeypatch.setattr(
        service,
        "get_frozen_selection",
        lambda *_a, **_kw: SimpleNamespace(
            refs=(
                EntityRef(
                    context.tenant_id, "account-A", "campaign", "source-CAMPAIGN-0"
                ),
            ),
            material_uses=(),
        ),
    )
    with Session(engine) as session, session.begin():
        draft = service.create(
            session,
            context=context,
            bc_id="bc-draft",
            selection_id=uuid4(),
            request_id=uuid4(),
        )
        identity = draft.id
        if copies != 1:
            service.patch(
                session,
                context=context,
                draft_id=identity,
                body=CopyPatchRequest(
                    request_id=uuid4(),
                    expected_revision=draft.revision,
                    rows=[
                        {
                            "row_id": draft.rows[0]["row_id"],
                            "configuration": dict(
                                draft.rows[0]["configuration"], copies=copies
                            ),
                        }
                    ],
                ),
            )
        service.preview(
            session,
            context=context,
            draft_id=identity,
            body=CopyRevisionRequest(
                request_id=uuid4(), expected_revision=draft.revision
            ),
        )
        payload = {"draft_id": str(identity), "generation": draft.generation}
    return engine, context, identity, payload


@pytest.fixture
def ready_batch(copy_source, monkeypatch):
    engine, context, identity, payload = start_batch(copy_source, monkeypatch)
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as session:
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.status == "READY", draft.error_code
        child = UUID(draft.children[0]["preview_id"])
    return engine, context, identity, child


def change_budget(session, context, identity):
    draft = session.get(CampaignCopyDraft, identity)
    return service.patch(
        session,
        context=context,
        draft_id=identity,
        body=CopyPatchRequest(
            request_id=uuid4(),
            expected_revision=draft.revision,
            rows=[
                {
                    "row_id": draft.rows[0]["row_id"],
                    "configuration": dict(draft.rows[0]["configuration"], budget="250"),
                }
            ],
        ),
    )


def test_parent_revision_obsoletes_child_and_rejects_old_submit(ready_batch):
    engine, context, identity, child = ready_batch
    with Session(engine) as session, session.begin():
        changed = change_budget(session, context, identity)
        assert changed.status == "DRAFT" and changed.children == []
    with Session(engine) as session, session.begin():
        assert session.get(BuildPreview, child).status == "OBSOLETE"
        with pytest.raises(DomainError) as error:
            submissions.submit_preview(
                session, context=context, preview_id=child, request_id=uuid4()
            )
        assert error.value.code in {"preview_not_frozen", "draft_revision_conflict"}
    # 废弃旧副本后，正常新预览仍应创建新身份并使用新预算。
    with Session(engine) as session, session.begin():
        draft = session.get(CampaignCopyDraft, identity)
        service.preview(
            session,
            context=context,
            draft_id=identity,
            body=CopyRevisionRequest(
                request_id=uuid4(), expected_revision=draft.revision
            ),
        )
        payload = {"draft_id": str(identity), "generation": draft.generation}
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as session, session.begin():
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.status == "READY", draft.error_code
        current_child = UUID(draft.children[0]["preview_id"])
        assert current_child != child
        assert session.get(BuildPreview, current_child).budget == 250


def test_direct_submitted_child_blocks_parent_edit_but_replay_stays_valid(ready_batch):
    engine, context, identity, child = ready_batch
    request_id = uuid4()
    with Session(engine) as session, session.begin():
        original = submissions.submit_preview(
            session, context=context, preview_id=child, request_id=request_id
        )
        submission_id = original.submission_id
    with Session(engine) as session, session.begin():
        with pytest.raises(DomainError) as error:
            change_budget(session, context, identity)
        assert error.value.code == "copy_already_submitted"
        assert session.get(CampaignCopyDraft, identity).status == "READY"
        assert session.get(BuildPreview, child).status == "FROZEN"
        replay = submissions.submit_preview(
            session, context=context, preview_id=child, request_id=request_id
        )
        assert replay.submission_id == submission_id


def test_history_mismatch_does_not_override_current_cta(copy_source):
    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        cta = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "CTA",
            )
        ).one()
        cta.mismatch, cta.error_code = True, "readback_mismatch"
        session.add(cta)
        session.flush()
        source = load_copy_source(
            session,
            context=context,
            bc_id="bc-draft",
            advertiser_id="account-A",
            campaign_id="source-CAMPAIGN-0",
        )
        assert source["blockers"] == []
        assert source["snapshot"]["cta_portfolio_id"] == "source-CTA-0"


@pytest.mark.parametrize("winner", ["edit", "submit"])
def test_parent_edit_and_direct_submit_serialize_without_double_intent(
    ready_batch, winner
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import text

    from app.modules.builds.execution_models import Submission
    from app.modules.builds.models import BuildDraft

    engine, context, identity, child = ready_batch
    with Session(engine) as session:
        child_draft_id = session.get(BuildPreview, child).draft_id
    barrier = Barrier(2)

    def work(kind):
        with Session(engine) as session:
            # 用真实行锁控制谁先取得创建/编辑边界；超时或死锁必须让测试失败。
            session.execute(text("SET LOCAL lock_timeout='5s'"))
            if kind == winner:
                if kind == "edit":
                    service.get_draft(
                        session, context=context, draft_id=identity, write=True
                    )
                session.exec(
                    select(BuildDraft)
                    .where(BuildDraft.id == child_draft_id)
                    .with_for_update()
                ).one()
            barrier.wait(timeout=10)
            try:
                if kind == "edit":
                    change_budget(session, context, identity)
                    result = "edited"
                else:
                    submissions.submit_preview(
                        session, context=context, preview_id=child, request_id=uuid4()
                    )
                    result = "submitted"
                session.commit()
                return result
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = set(pool.map(work, ["edit", "submit"]))
    assert outcomes == (
        {"edited", "preview_not_frozen"}
        if winner == "edit"
        else {"copy_already_submitted", "submitted"}
    )
    with Session(engine) as session:
        accepted = session.exec(
            select(Submission).where(Submission.preview_id == child)
        ).all()
        assert len(accepted) == (0 if winner == "edit" else 1)
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.revision == (2 if winner == "edit" else 1)


@pytest.mark.parametrize("retry_recovers", [False, True])
@pytest.mark.parametrize("submitted_first", [False, True])
def test_failed_retry_keeps_prepared_children_for_later_invalidation(
    copy_source, monkeypatch, retry_recovers, submitted_first
):
    from app.modules.builds import copying

    engine, context, identity, payload = start_batch(copy_source, monkeypatch, copies=2)
    original = copying.create_copy_preview
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as session:
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.status == "PREPARING"
        old_child = UUID(draft.children[0]["preview_id"])
        payload["generation"] = draft.generation

    direct_request = uuid4()
    if submitted_first:
        with Session(engine) as session, session.begin():
            direct = submissions.submit_preview(
                session,
                context=context,
                preview_id=old_child,
                request_id=direct_request,
            )
            direct_id = direct.submission_id

    def unavailable(*_a, **_kw):
        raise DomainError("copy_scene_unavailable", "临时无法核验目标场景")

    monkeypatch.setattr(copying, "create_copy_preview", unavailable)
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as session, session.begin():
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.status == "FAILED"
        service.preview(
            session,
            context=context,
            draft_id=identity,
            body=CopyRevisionRequest(
                request_id=uuid4(), expected_revision=draft.revision
            ),
        )
        assert draft.children[0]["preview_id"] == str(old_child)
        assert draft.children[0]["status"] == "READY"
        payload["generation"] = draft.generation
    if retry_recovers:
        monkeypatch.setattr(copying, "create_copy_preview", original)
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    monkeypatch.setattr(copying, "create_copy_preview", original)
    with Session(engine) as session, session.begin():
        draft = session.get(CampaignCopyDraft, identity)
        assert draft.status == ("READY" if retry_recovers else "FAILED")
        assert draft.children[0]["preview_id"] == str(old_child)
        if retry_recovers:
            assert len({child["preview_id"] for child in draft.children}) == 2
        if submitted_first:
            with pytest.raises(DomainError) as error:
                change_budget(session, context, identity)
            assert error.value.code == "copy_already_submitted"
            replay = submissions.submit_preview(
                session,
                context=context,
                preview_id=old_child,
                request_id=direct_request,
            )
            assert replay.submission_id == direct_id
        else:
            change_budget(session, context, identity)
    if submitted_first:
        return
    with Session(engine) as session, session.begin():
        assert session.get(BuildPreview, old_child).status == "OBSOLETE"
        with pytest.raises(DomainError) as error:
            submissions.submit_preview(
                session, context=context, preview_id=old_child, request_id=uuid4()
            )
        assert error.value.code == "preview_not_frozen"


def test_region_display_enrichment_does_not_invalidate_copy_source(
    copy_source, monkeypatch
):
    from app.modules.builds.scene_job_models import SceneJob

    engine, context, _, _ = copy_source
    with Session(engine) as session, session.begin():
        for job in session.exec(select(SceneJob)).all():
            job.facts = {**job.facts, "regions": {}}
            session.add(job)
    engine, context, identity, payload = start_batch(copy_source, monkeypatch)
    with Session(engine) as session, session.begin():
        row = session.get(CampaignCopyDraft, identity)
        assert row.rows[0]["source"]["targeting"] is None
        for job in session.exec(select(SceneJob)).all():
            job.facts = {
                **job.facts,
                "regions": {
                    "locations": [
                        {"region_code": "US", "location_id": "fixture-location"}
                    ]
                },
            }
            session.add(job)
    tasks.process(
        database_engine=engine,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    with Session(engine) as session, session.begin():
        row = session.get(CampaignCopyDraft, identity)
        assert row.status == "READY", row.error_code
        service.submit(
            session,
            context=context,
            draft_id=identity,
            body=CopySubmitRequest(
                request_id=uuid4(),
                expected_revision=row.revision,
                preview_digest=row.preview_digest,
            ),
        )


def test_refresh_sources_upgrades_legacy_draft_and_preserves_saved_settings(
    copy_source, monkeypatch
):
    from copy import deepcopy

    engine, context, identity, _ = start_batch(copy_source, monkeypatch)
    with Session(engine) as session, session.begin():
        row = session.get(CampaignCopyDraft, identity)
        items = deepcopy(row.rows)
        items[0]["source"]["snapshot"]["version"] = 1
        items[0]["configuration"]["budget"] = "250"
        row.rows = items
        row.status = "FAILED"
        session.add(row)
    request = CopyRevisionRequest(request_id=uuid4(), expected_revision=1)
    with Session(engine) as session, session.begin():
        row = service.refresh_sources(
            session, context=context, draft_id=identity, body=request
        )
        assert row.status == "DRAFT" and row.revision == 2
        assert row.rows[0]["source"]["snapshot"]["version"] == 2
        assert row.rows[0]["configuration"]["budget"] == "250"
        assert row.children == [] and row.preview_digest is None
        assert (
            service.refresh_sources(
                session, context=context, draft_id=identity, body=request
            ).revision
            == 2
        )


def test_legacy_frozen_copy_requires_explicit_refresh_without_rewriting_it():
    from app.modules.builds.copying import validate_copy_submission

    with pytest.raises(DomainError) as error:
        validate_copy_submission(
            None,
            context=None,
            preview=SimpleNamespace(config={"campaign_copy_version": 1}),
        )
    assert error.value.code == "copy_source_contract_obsolete"
