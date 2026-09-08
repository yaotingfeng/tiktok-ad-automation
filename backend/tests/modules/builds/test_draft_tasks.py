from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlmodel import select

from app.jobs.models import PendingDispatch
from app.modules.builds.draft_tasks import process_preparation, repair_preparations
from app.modules.builds.drafts import create_draft, prepare_draft
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.tenants.models import TenantMembership
from tests.modules.builds.test_drafts import account


def test_duplicate_delivery_only_advances_one_local_page(session, context, intent):
    account(session, context)
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    payload = {"preparation_id": str(task), "generation": 0}
    for _ in range(2):
        process_preparation(
            database_engine=session.connection(),
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            payload=payload,
        )
    session.expire_all()
    prep = session.get(DraftPreparation, task)
    assert prep.generation == 1 and prep.account_after == 4 and prep.phase == "accounts"


def test_revoked_actor_becomes_explicitly_blocked_and_can_prepare_after_restore(
    session, context, intent
):
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    member = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == context.tenant_id,
            TenantMembership.user_id == context.actor_id,
        )
    ).one()
    member.role = "viewer"
    session.add(member)
    session.flush()
    process_preparation(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload={"preparation_id": str(task), "generation": 0},
    )
    session.expire_all()
    assert session.get(DraftPreparation, task).status == "BLOCKED"
    assert session.get(DraftPreparation, task).error_code == "action_forbidden"
    assert session.get(BuildDraft, draft).status == "BLOCKED"
    member.role = "operator"
    session.add(member)
    session.flush()
    retry = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    assert retry != task
    assert (
        session.get(DraftPreparation, retry).provider_task_id
        == session.get(DraftPreparation, task).provider_task_id
    )


def test_repair_preserves_current_identity_and_unpublished_backoff(
    session, context, intent
):
    draft = create_draft(session, context=context, **intent)
    task = prepare_draft(session, context=context, draft_id=draft, request_id=uuid4())
    prep = session.get(DraftPreparation, task)
    dispatch = session.get(PendingDispatch, prep.dispatch_id)
    original = dispatch.id
    future = datetime.now(UTC) + timedelta(minutes=10)
    prep.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.available_at = future
    session.add_all([prep, dispatch])
    session.flush()
    assert repair_preparations(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(PendingDispatch, original).available_at == future
    prep = session.get(DraftPreparation, task)
    dispatch = session.get(PendingDispatch, original)
    prep.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch.published_at = datetime.now(UTC) - timedelta(minutes=5)
    session.add_all([prep, dispatch])
    session.flush()
    assert repair_preparations(database_engine=session.connection(), limit=1) == 1
    session.expire_all()
    assert session.get(DraftPreparation, task).dispatch_id == original
    assert session.get(DraftPreparation, task).generation == 0
    assert session.get(PendingDispatch, original).published_at is None
