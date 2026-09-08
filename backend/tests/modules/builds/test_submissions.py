from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlmodel import select

from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.builds import previews, submissions
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionRequest,
)
from tests.modules.builds.test_previews import drain
from tests.modules.builds.test_previews import prepared as prepared


@pytest.fixture
def frozen(session, context, prepared):
    identity = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, identity)
    return identity


def test_duplicate_alias_is_permanent_and_submission_is_one(session, context, frozen):
    keys = [uuid4(), uuid4()]
    first = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=keys[0]
    )
    second = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=keys[1]
    )
    assert first.submission_id == second.submission_id
    assert session.exec(select(func.count()).select_from(Submission)).one() == 1
    assert session.exec(select(func.count()).select_from(SubmissionRequest)).one() == 2
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == 0
    assert (
        session.exec(
            select(func.count())
            .select_from(PendingDispatch)
            .where(PendingDispatch.task_name == "builds.expand_submission")
        ).one()
        == 1
    )
    with pytest.raises(DomainError, match="幂等"):
        submissions.submit_preview(
            session, context=context, preview_id=uuid4(), request_id=keys[1]
        )


def test_expansion_is_bounded_idempotent_and_snapshot_scoped(session, context, frozen):
    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    assert not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id, limit=1
    )
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() <= 1
    for _ in range(1000):
        if submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id, limit=100
        ):
            break
    else:
        raise AssertionError("expansion did not finish")
    before = session.exec(select(func.count()).select_from(ExecutionStep)).one()
    assert submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == before
    counts = dict(
        session.exec(
            select(ExecutionStep.kind, func.count()).group_by(ExecutionStep.kind)
        ).all()
    )
    assert counts == {
        "MATERIAL": 138,
        "CTA": 6,
        "CAMPAIGN": 6,
        "ADGROUP": 18,
        "AD": 36,
        "READBACK": 60,
    }


def test_obsolete_alias_recovers_original_without_reinterpreting_draft(
    session, context, frozen
):
    from app.modules.builds.models import BuildDraft
    from app.modules.builds.preview_models import BuildPreview

    key = uuid4()
    first = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=key
    )
    preview = session.get(BuildPreview, frozen)
    draft = session.get(BuildDraft, preview.draft_id)
    draft.revision += 1
    session.add(draft)
    session.flush()
    assert (
        session.get(BuildPreview, frozen, populate_existing=True).status == "OBSOLETE"
    )
    for request in (key, uuid4()):
        replay = submissions.submit_preview(
            session, context=context, preview_id=frozen, request_id=request
        )
        assert replay.submission_id == first.submission_id
    while not submissions.expand_submission(
        session, context=context, submission_id=first.submission_id
    ):
        pass
    assert (
        submissions.get_submission(
            session, context=context, submission_id=first.submission_id
        ).submitted.ad_count
        == 36
    )


def test_later_preview_cannot_claim_earlier_scope_even_if_it_expands_first(
    session, context, prepared, monkeypatch
):
    from dataclasses import replace

    from app.modules.builds.execution_models import DraftUnitReservation
    from app.modules.builds.models import BuildDraft

    original = previews.read_scene_context
    monkeypatch.setattr(
        previews,
        "read_scene_context",
        lambda *a, **kw: (
            replace(
                original(*a, **kw), supported=False, reason_codes=("unsupported_scene",)
            )
            if kw["advertiser_id"] == "C"
            else original(*a, **kw)
        ),
    )
    first_preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, first_preview)
    first = submissions.submit_preview(
        session, context=context, preview_id=first_preview, request_id=uuid4()
    )
    draft = session.get(BuildDraft, prepared)
    draft.revision += 1
    session.add(draft)
    session.flush()
    monkeypatch.setattr(previews, "read_scene_context", original)
    second_preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=2
    )
    drain(session, context, second_preview)
    second = submissions.submit_preview(
        session, context=context, preview_id=second_preview, request_id=uuid4()
    )
    before = submissions.get_submission(
        session, context=context, submission_id=second.submission_id
    )
    assert before.submitted.campaign_count == 2 and before.excluded.campaign_count == 4
    while not submissions.expand_submission(
        session, context=context, submission_id=second.submission_id
    ):
        pass
    while not submissions.expand_submission(
        session, context=context, submission_id=first.submission_id
    ):
        pass
    reservations = session.exec(select(DraftUnitReservation)).all()
    assert len(reservations) == 6
    assert all(
        r.submission_id
        == (second.submission_id if r.advertiser_id == "C" else first.submission_id)
        for r in reservations
    )
    excluded = submissions.get_submission_units(
        session, context=context, submission_id=second.submission_id, excluded_only=True
    )
    assert len(excluded.items) == 4 and all(
        x.reason_codes == ["previous_submission"] for x in excluded.items
    )


def test_actor_revocation_blocks_submission_and_worker_then_resumes_current_generation(
    session, context, frozen
):
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.submission_tasks import (
        process_submission,
        repair_submissions,
    )
    from app.modules.tenants.models import TenantMembership

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    row = session.get(Submission, receipt.submission_id)
    payload = {"submission_id": str(row.id), "revision": row.dispatch_revision}
    dispatch = session.get(PendingDispatch, row.dispatch_id)
    dispatch.published_at = datetime.now(UTC)
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "viewer"
    session.add_all([member, dispatch])
    session.flush()
    with pytest.raises(DomainError):
        submissions.submit_preview(
            session, context=context, preview_id=frozen, request_id=uuid4()
        )
    process_submission(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    session.expire_all()
    row = session.get(Submission, receipt.submission_id)
    assert row.error_code == "action_forbidden" and row.dispatch_revision == 0
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() == 0
    member = session.get(TenantMembership, (context.tenant_id, context.actor_id))
    member.role = "operator"
    row.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    session.add_all([member, row])
    session.flush()
    assert repair_submissions(database_engine=session.connection()) == 1
    session.expire_all()
    assert session.get(PendingDispatch, row.dispatch_id).published_at is None
    process_submission(
        database_engine=session.connection(),
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        payload=payload,
    )
    session.expire_all()
    assert session.exec(select(func.count()).select_from(ExecutionStep)).one() > 0


def test_repair_preserves_unpublished_transport_backoff(session, context, frozen):
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.submission_tasks import repair_submissions

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    row = session.get(Submission, receipt.submission_id)
    row.repair_after = datetime.now(UTC) - timedelta(seconds=1)
    dispatch = session.get(PendingDispatch, row.dispatch_id)
    future = datetime.now(UTC) + timedelta(minutes=8)
    dispatch.available_at = future
    session.add_all([row, dispatch])
    session.flush()
    assert repair_submissions(database_engine=session.connection()) == 1
    session.expire_all()
    assert session.get(Submission, row.id).dispatch_id == dispatch.id
    assert session.get(PendingDispatch, dispatch.id).available_at == future


@pytest.mark.parametrize(
    "mode",
    [
        "same_request",
        "different_request",
        "different_preview",
        "rollback",
        "duplicate_expand",
    ],
)
def test_real_connections_serialize_submission_and_expansion(
    isolated_strategy_database, monkeypatch, mode
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        draft = prepared.__wrapped__(
            session, context, create_intent(session, context), monkeypatch
        )
        preview = previews.generate_preview(
            session, context=context, draft_id=draft, expected_revision=1
        )
        drain(session, context, preview)
        other = uuid4()
        if mode == "different_preview":
            from app.modules.builds.drafts import create_draft, prepare_draft
            from app.modules.builds.models import BuildDraft
            from tests.modules.builds.test_drafts import finish, ready_links

            old = session.get(BuildDraft, draft)
            values = {
                "bc_id": old.bc_id,
                "strategy_version_id": old.strategy_version_id,
                "provider_connection_id": old.provider_connection_id,
                "application_id": old.application_id,
                "link_config": old.link_config,
                "drama_lines": ["Moon", "Short Drama"],
                "account_lines": ["A", "B", "C"],
            }
            from app.modules.providers.models import (
                ProviderApplication,
                ProviderConnection,
            )

            provider = ProviderConnection(
                tenant_id=context.tenant_id,
                kind="jiashu",
                display_name="second",
                encrypted_credentials="unused",
                status="active",
            )
            session.add(provider)
            session.flush()
            session.add(
                ProviderApplication(
                    tenant_id=context.tenant_id,
                    connection_id=provider.id,
                    external_id="app-draft",
                    name="second",
                )
            )
            session.flush()
            values["provider_connection_id"] = provider.id
            other_draft = create_draft(session, context=context, **values)
            task = prepare_draft(
                session, context=context, draft_id=other_draft, request_id=uuid4()
            )
            ready_links(session, context, task, values)
            finish(session, context, task)
            other = previews.generate_preview(
                session, context=context, draft_id=other_draft, expected_revision=1
            )
            drain(session, context, other)
        if mode == "duplicate_expand":
            receipt = submissions.submit_preview(
                session, context=context, preview_id=preview, request_id=uuid4()
            )
        session.commit()
    keys = [uuid4(), uuid4()]
    if mode in {"same_request", "different_preview"}:
        keys[1] = keys[0]
    barrier = Barrier(2)

    def worker(index):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                if mode == "duplicate_expand":
                    value = submissions.expand_submission(
                        session,
                        context=context,
                        submission_id=receipt.submission_id,
                        limit=100,
                    )
                else:
                    value = submissions.submit_preview(
                        session,
                        context=context,
                        preview_id=preview
                        if index == 0 or mode != "different_preview"
                        else other,
                        request_id=keys[index],
                    )
                if mode == "rollback" and index == 0:
                    session.rollback()
                    return None
                session.commit()
                return value
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    with Session(engine) as session:
        if mode == "different_preview":
            assert "idempotency_conflict" in results
        else:
            assert session.exec(select(func.count()).select_from(Submission)).one() == 1
            if mode == "duplicate_expand":
                assert (
                    session.exec(select(func.count()).select_from(ExecutionStep)).one()
                    <= 200
                )
            else:
                assert (
                    session.exec(
                        select(func.count())
                        .select_from(PendingDispatch)
                        .where(PendingDispatch.task_name == "builds.expand_submission")
                    ).one()
                    == 1
                )


def test_step_claim_rechecks_permission_and_never_replays_armed_request(
    session, context, frozen
):
    from datetime import UTC, datetime, timedelta

    from app.modules.accounts.models import BCAccountAccess

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    step = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "MATERIAL")
    ).first()
    owner = uuid4()
    claim = submissions.claim_step(
        session, context=context, step_id=step.id, owner=owner
    )
    assert claim.lease_token == owner and claim.attempt == 1
    assert (
        submissions.claim_step(session, context=context, step_id=step.id, owner=uuid4())
        is None
    )
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(step)
    session.flush()
    second = submissions.claim_step(
        session, context=context, step_id=step.id, owner=uuid4()
    )
    assert second.attempt == 2 and second.lease_token != owner
    step.request_body = {"advertiser_id": "A", "operation_status": "ENABLE"}
    step.request_body_digest = "d" * 64
    step.phase = "REQUEST_ARMED"
    step.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(step)
    session.flush()
    assert (
        submissions.claim_step(session, context=context, step_id=step.id, owner=uuid4())
        is None
    )
    other = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "MATERIAL", ExecutionStep.id != step.id
        )
    ).first()
    unit = submissions.load_execution_unit(
        session,
        context=context,
        submission_id=receipt.submission_id,
        unit_id=other.unit_id,
    )
    grant = session.get(
        BCAccountAccess,
        (
            context.tenant_id,
            unit.frozen.bc_id,
            unit.frozen.advertiser_id,
            unit.frozen.connection_id,
        ),
    )
    grant.authorized = False
    session.add(grant)
    session.flush()
    with pytest.raises(DomainError):
        submissions.claim_step(
            session, context=context, step_id=other.id, owner=uuid4()
        )
    campaign = session.exec(
        select(ExecutionStep).where(ExecutionStep.kind == "CAMPAIGN")
    ).first()
    assert (
        submissions.claim_step(
            session, context=context, step_id=campaign.id, owner=uuid4()
        )
        is None
    )


def test_database_rejects_scope_changes_and_mutating_evidence(session, context, frozen):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.modules.builds.execution_models import StepEvidence

    receipt = submissions.submit_preview(
        session, context=context, preview_id=frozen, request_id=uuid4()
    )
    while not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id
    ):
        pass
    step = session.exec(select(ExecutionStep).where(ExecutionStep.kind == "AD")).first()
    evidence = StepEvidence(
        tenant_id=context.tenant_id,
        submission_id=receipt.submission_id,
        step_id=step.id,
        attempt=0,
        conclusion="fixture",
    )
    session.add(evidence)
    session.flush()
    for query, values in [
        (
            "UPDATE step_evidence SET conclusion='rewritten' WHERE id=:id",
            {"id": evidence.id},
        ),
        ("DELETE FROM step_evidence WHERE id=:id", {"id": evidence.id}),
        (
            "DELETE FROM submission_request WHERE submission_id=:id",
            {"id": receipt.submission_id},
        ),
        (
            "UPDATE execution_step SET unit_id=:new WHERE id=:id",
            {"id": step.id, "new": uuid4()},
        ),
        (
            "UPDATE build_submission SET actor_id=:new WHERE id=:id",
            {"id": receipt.submission_id, "new": uuid4()},
        ),
    ]:
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(query), values)
    step.request_body = {"operation_status": "ENABLE"}
    step.request_body_digest = "f" * 64
    session.add(step)
    session.flush()
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE execution_step SET request_body='{}'::jsonb WHERE id=:id"),
            {"id": step.id},
        )
    other = session.exec(
        select(ExecutionStep).where(
            ExecutionStep.kind == "AD", ExecutionStep.unit_id != step.unit_id
        )
    ).first()
    with pytest.raises(DBAPIError), session.begin_nested():
        session.add(
            ExecutionStep(
                tenant_id=context.tenant_id,
                submission_id=receipt.submission_id,
                preview_id=frozen,
                bc_id=step.bc_id,
                unit_id=step.unit_id,
                kind="AD",
                step_key="bad-group",
                group_id=other.group_id,
                planned_ad_id=other.planned_ad_id,
            )
        )
        session.flush()


def test_http_submission_scope_and_read_routes(session, context, other_context, frozen):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.core.errors import domain_error_handler

    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = lambda: session
    from app.modules.builds.submission_api import router
    from tests.modules.strategies.test_api import headers

    path = "/api/tenants/{tenant_id}/build-previews/{preview_id}/submit"
    if not any(getattr(route, "path", None) == path for route in app.routes):
        app.include_router(router, prefix="/api")
    client = TestClient(app)
    base = f"/api/tenants/{context.tenant_id}"
    request = {"request_id": str(uuid4())}
    submitted = client.post(
        f"{base}/build-previews/{frozen}/submit", headers=headers(context), json=request
    )
    assert submitted.status_code == 202
    identity = submitted.json()["submission_id"]
    assert (
        client.post(
            f"{base}/build-previews/{frozen}/submit",
            headers=headers(context),
            json=request,
        ).json()
        == submitted.json()
    )
    assert client.get(
        f"{base}/submissions/{identity}", headers=headers(context)
    ).json()["submitted"] == {"campaign_count": 6, "adgroup_count": 18, "ad_count": 36}
    assert (
        client.get(
            f"{base}/submissions/{identity}/units", headers=headers(context)
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"{base}/submissions/{identity}/steps?limit=101", headers=headers(context)
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/tenants/{other_context.tenant_id}/submissions/{identity}",
            headers=headers(other_context),
        ).status_code
        == 404
    )


def test_503_accounts_submit_only_header_and_expand_at_most_100_facts(
    session, context, prepared
):
    from sqlalchemy import event

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.builds.execution_models import SubmissionUnit
    from app.modules.builds.models import DraftAccount
    from tests.modules.builds.test_drafts import account

    for index in range(500):
        identity = f"capacity-{index:04}"
        account(session, context, identity)
        grant = session.exec(
            select(BCAccountAccess).where(BCAccountAccess.advertiser_id == identity)
        ).one()
        session.add(
            DraftAccount(
                tenant_id=context.tenant_id,
                draft_id=prepared,
                advertiser_id=identity,
                bc_id="bc-draft",
                connection_id=grant.connection_id,
                currency="USD",
                timezone="UTC",
                first_line=index + 4,
            )
        )
    session.flush()
    preview = previews.generate_preview(
        session, context=context, draft_id=prepared, expected_revision=1
    )
    drain(session, context, preview)
    session.expunge_all()
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    connection = session.connection()
    event.listen(connection, "before_cursor_execute", observe)
    try:
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview, request_id=uuid4()
        )
    finally:
        event.remove(connection, "before_cursor_execute", observe)
    assert not any(
        "INSERT INTO submission_unit" in s or "INSERT INTO execution_step" in s
        for s in statements
    )
    assert len(session.identity_map) < 20
    summary = submissions.get_submission(
        session, context=context, submission_id=receipt.submission_id
    )
    assert (
        summary.submitted.campaign_count == 1006 and summary.submitted.ad_count == 6036
    )
    assert not submissions.expand_submission(
        session, context=context, submission_id=receipt.submission_id, limit=100
    )
    assert (
        session.exec(select(func.count()).select_from(ExecutionStep)).one()
        + session.exec(select(func.count()).select_from(SubmissionUnit)).one()
        <= 100
    )
    assert len(session.identity_map) < 120


def test_two_workers_only_one_claims_a_step(isolated_strategy_database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from tests.modules.builds.test_drafts import create_intent

    engine, context, _ = isolated_strategy_database
    with Session(engine) as session:
        draft = prepared.__wrapped__(
            session, context, create_intent(session, context), monkeypatch
        )
        preview = previews.generate_preview(
            session, context=context, draft_id=draft, expected_revision=1
        )
        drain(session, context, preview)
        receipt = submissions.submit_preview(
            session, context=context, preview_id=preview, request_id=uuid4()
        )
        while not submissions.expand_submission(
            session, context=context, submission_id=receipt.submission_id
        ):
            pass
        identity = session.exec(
            select(ExecutionStep.id).where(ExecutionStep.kind == "MATERIAL")
        ).first()
        session.commit()
    barrier = Barrier(2)

    def claim(_index):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            result = submissions.claim_step(
                session, context=context, step_id=identity, owner=uuid4()
            )
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result is not None for result in results) == 1
    with Session(engine) as session:
        assert session.get(ExecutionStep, identity).attempt == 1
