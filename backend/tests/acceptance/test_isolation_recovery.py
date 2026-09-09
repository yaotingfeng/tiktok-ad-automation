"""Cross-module replay and isolation: real database, real tasks, offline wire."""

from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.api.deps import get_db
from app.main import app
from app.modules.builds import submissions
from app.modules.builds.execution_models import ExecutionStep, SubmissionRequest
from app.modules.tenants.models import TenantMembership
from tests.acceptance.scenario import PASSWORD


def test_cross_tenant_http_reads_and_frozen_cursor_are_isolated(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    scenario.submit()

    def database_session():
        with Session(scenario.database_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database_session
    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/login/access-token",
                data={"username": scenario.scope.email, "password": PASSWORD},
            )
            assert login.status_code == 200
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            own = f"/api/tenants/{scenario.scope.context.tenant_id}"
            foreign = f"/api/tenants/{scenario.other.context.tenant_id}"
            before = deepcopy(scenario.runtime.wire.calls)
            from app.jobs.models import PendingDispatch

            with Session(scenario.database_engine) as session:
                dispatch_before = session.exec(
                    select(func.count()).select_from(PendingDispatch)
                ).one()
            assert (
                client.get(
                    f"{foreign}/build-previews/{scenario.preview_id}", headers=headers
                ).status_code
                == 403
            )
            assert (
                client.get(
                    f"{own}/materials/{scenario.other.material_ids[0]}?bc_id={scenario.scope.bc_id}",
                    headers=headers,
                ).status_code
                == 404
            )
            assert (
                client.get(f"{own}/submissions/{uuid4()}", headers=headers).status_code
                == 404
            )
            other_login = client.post(
                "/api/login/access-token",
                data={"username": scenario.other.email, "password": PASSWORD},
            )
            assert other_login.status_code == 200
            other_headers = {
                "Authorization": "Bearer " + other_login.json()["access_token"]
            }
            from app.modules.providers.models import PromotionLink

            with Session(scenario.database_engine) as session:
                link_id = session.exec(
                    select(PromotionLink.id).where(
                        PromotionLink.tenant_id == scenario.scope.context.tenant_id
                    )
                ).first()
            for suffix in [
                f"/build-previews/{scenario.preview_id}",
                f"/submissions/{scenario.submission_id}",
                f"/providers/links/{link_id}",
            ]:
                assert (
                    client.get(foreign + suffix, headers=other_headers).status_code
                    == 404
                )
                assert (
                    client.get(own + suffix, headers=other_headers).status_code == 403
                )
            conflict = client.get(
                f"{foreign}/accounts",
                params={"bc_id": scenario.other.bc_id},
                headers=other_headers,
            )
            assert conflict.status_code == 200
            assert any(
                row["advertiser_id"] == scenario.scope.accounts[0]
                for row in conflict.json()["items"]
            )
            first = client.get(
                f"{own}/build-previews/{scenario.preview_id}/units?limit=2",
                headers=headers,
            )
            assert first.status_code == 200
            cursor = first.json()["next_cursor"]
            assert cursor
            second = client.get(
                f"{own}/build-previews/{scenario.preview_id}/units",
                params={"limit": 2, "cursor": cursor},
                headers=headers,
            )
            assert second.status_code == 200
            assert {r["unit_id"] for r in first.json()["items"]}.isdisjoint(
                {r["unit_id"] for r in second.json()["items"]}
            )
            tamper = client.get(
                f"{own}/build-previews/{scenario.preview_id}/units",
                params={"limit": 2, "cursor": cursor, "readiness": "BLOCKED"},
                headers=headers,
            )
            assert tamper.status_code == 422
            material_url = f"{own}/materials/{scenario.scope.material_ids[0]}"
            assert (
                client.get(
                    material_url,
                    params={"bc_id": scenario.scope.bc_id},
                    headers=headers,
                ).status_code
                == 200
            )
            signed = client.get(
                material_url + "/preview",
                params={"bc_id": scenario.scope.bc_id},
                headers=headers,
            )
            assert (
                signed.status_code == 200
                and signed.headers["Cache-Control"] == "no-store"
            )
            assert signed.json()["expires_in"] == 300
            from urllib.parse import parse_qs, urlsplit

            parsed = urlsplit(signed.json()["url"])
            assert parsed.path.endswith(
                f"/tenants/{scenario.scope.context.tenant_id}/materials/{scenario.scope.material_ids[0]}/original"
            )
            assert parse_qs(parsed.query)["response-content-type"] == ["video/mp4"]
            foreign_signed = client.get(
                f"{own}/materials/{scenario.other.material_ids[0]}/preview",
                params={"bc_id": scenario.scope.bc_id},
                headers=headers,
            )
            assert (
                foreign_signed.status_code == 404 and "url" not in foreign_signed.json()
            )
            assert scenario.runtime.wire.calls == before
            with Session(scenario.database_engine) as session:
                assert (
                    session.exec(
                        select(func.count()).select_from(PendingDispatch)
                    ).one()
                    == dispatch_before
                )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("acceptance_scenario", [{"material_count": 1}], indirect=True)
@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("kind", ["campaign", "ad"])
def test_remote_commit_lost_reply_uses_get_without_recreating(
    acceptance_scenario, ambiguous, kind
):
    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    wire = scenario.runtime.wire
    wire.lose_response_kind = kind
    wire.ambiguous_kind = kind if ambiguous else None
    expected_creates = 6 if kind == "campaign" else 12
    create_endpoint = f"/open_api/v1.3/smart_plus/{kind}/create/"
    get_endpoint = f"/open_api/v1.3/smart_plus/{kind}/get/"
    scenario.submit()

    def settled():
        # NEEDS_REVIEW has deliberate precedence while unrelated units still run.
        # Wait for their actual readbacks and the ambiguous create's GET, rather
        # than interpreting the aggregate label as a worker completion signal.
        if not ambiguous:
            return scenario.view().status == "COMPLETED"
        if wire.calls[get_endpoint] < expected_creates:
            return False
        with Session(scenario.database_engine) as session:
            verified = session.exec(
                select(func.count())
                .select_from(ExecutionStep)
                .where(
                    ExecutionStep.submission_id == scenario.submission_id,
                    ExecutionStep.kind == "READBACK",
                    ExecutionStep.status == "SUCCEEDED",
                )
            ).one()
        return verified == (20 if kind == "campaign" else 23)

    scenario.runtime.drive_until(settled, timeout=420)
    view = scenario.view()
    assert wire.calls[create_endpoint] == expected_creates, (
        scenario.runtime.diagnostics()
    )
    assert wire.calls[get_endpoint] >= expected_creates
    if ambiguous:
        assert view.status == "NEEDS_REVIEW"
        with Session(scenario.database_engine) as session:
            unknown = session.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == scenario.submission_id,
                    ExecutionStep.kind == kind.upper(),
                    ExecutionStep.status == "UNKNOWN",
                )
            ).all()
            assert len(unknown) == 1 and unknown[0].remote_id is None
        assert len(wire.smart.store[kind]) == expected_creates + 1
        from app.modules.builds.recovery import (
            get_recovery,
            get_request,
            request_recovery,
        )

        with Session(scenario.database_engine) as session, session.begin():
            request_id = uuid4()
            receipt = request_recovery(
                session,
                context=scenario.scope.context,
                submission_id=scenario.submission_id,
                request_id=request_id,
                kind="RECONCILE",
            )

        def recovery_done():
            with Session(scenario.database_engine) as session:
                return (
                    get_recovery(
                        session,
                        context=scenario.scope.context,
                        recovery_id=receipt.recovery_id,
                    ).state
                    == "COMPLETED"
                )

        scenario.runtime.drive_until(recovery_done)
        scenario.runtime.pump_jobs()
        assert scenario.view().status == "NEEDS_REVIEW"
        with Session(scenario.database_engine) as session:
            assert (
                get_request(
                    session, context=scenario.scope.context, request_id=request_id
                )
                == receipt
            )
            assert receipt.state == "QUEUED" and receipt.scheduled_count == 0
        assert wire.calls[create_endpoint] == expected_creates

    else:
        assert view.status == "COMPLETED", scenario.runtime.diagnostics()
        assert len(wire.smart.store[kind]) == expected_creates
        assert view.succeeded.campaign_count == 6
    creates = wire.calls[create_endpoint]
    for message in list(scenario.runtime.delivered):
        if message["name"] in {"builds.execute_step", "builds.reconcile_step"}:
            scenario.runtime.deliver(message)
    scenario.runtime.pump_jobs()
    assert wire.calls[create_endpoint] == creates


@pytest.mark.parametrize("acceptance_scenario", [{"material_count": 1}], indirect=True)
def test_submission_aliases_and_duplicate_delivery_do_not_repeat_post(
    acceptance_scenario,
):
    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    first, alias = uuid4(), uuid4()
    submission_id = scenario.submit(first)
    assert scenario.submit(first) == scenario.submit(alias) == submission_id
    with Session(scenario.database_engine) as session:
        assert (
            session.exec(
                select(func.count())
                .select_from(SubmissionRequest)
                .where(SubmissionRequest.tenant_id == scenario.scope.context.tenant_id)
            ).one()
            == 2
        )
    scenario.runtime.drive_until(
        lambda: scenario.view().status not in {"QUEUED", "RUNNING"}
    )
    assert scenario.view().status == "COMPLETED", scenario.runtime.diagnostics()
    before = len(
        [c for c in scenario.runtime.wire.smart.calls if c["method"] == "POST"]
    )
    for message in list(scenario.runtime.delivered):
        scenario.runtime.deliver(message)
    scenario.runtime.pump_jobs()
    assert scenario.submit(first) == scenario.submit(alias) == submission_id
    assert (
        len([c for c in scenario.runtime.wire.smart.calls if c["method"] == "POST"])
        == before
        == 24
    )


@pytest.mark.parametrize("acceptance_scenario", [{"material_count": 1}], indirect=True)
def test_revocation_after_first_ad_preserves_enable_and_stops_remaining_creatives(
    acceptance_scenario,
):
    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    wire = scenario.runtime.wire

    def revoke(kind, _body):
        if kind == "ad":
            wire.after_create = None
            with Session(scenario.database_engine) as session, session.begin():
                member = session.get(
                    TenantMembership,
                    (scenario.scope.context.tenant_id, scenario.scope.context.actor_id),
                )
                member.role = "viewer"
                session.add(member)

    wire.after_create = revoke
    scenario.submit()
    scenario.runtime.drive_until(
        lambda: scenario.view().status not in {"QUEUED", "RUNNING"}
    )
    assert len(wire.smart.store["ad"]) == 1, scenario.runtime.diagnostics()
    assert next(iter(wire.smart.store["ad"].values()))["operation_status"] == "ENABLE"
    assert wire.smart.store["campaign"] and wire.smart.store["adgroup"]
    assert all(c["path"].endswith(("/create/", "/get/")) for c in wire.smart.calls)
    with Session(scenario.database_engine) as session:
        known = session.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == scenario.submission_id,
                ExecutionStep.kind == "AD",
                ExecutionStep.remote_id.is_not(None),
            )
        ).all()
        assert len(known) == 1 and known[0].status == "SUCCEEDED"


@pytest.mark.parametrize("acceptance_scenario", [{"material_count": 1}], indirect=True)
def test_expired_armed_attempt_late_receipt_cannot_replace_reader_lease(
    acceptance_scenario,
):
    """Separate committed sessions; late SDK receipt arrives during the new GET."""
    import time
    from contextlib import ExitStack

    from redis import Redis

    from app.core.config import settings
    from app.integrations.tiktok.sdk import sdk_client
    from app.modules.builds.dispatch import queue_step
    from app.modules.builds.execution import prepare_request
    from app.modules.builds.execution_admission import admitted_build_call
    from app.modules.builds.execution_models import (
        StepEvidence,
        Submission,
        SubmissionUnit,
    )
    from app.modules.builds.execution_state import (
        arm_request,
        expire_attempt,
        preserve_created_receipt,
        record_created,
    )
    from app.modules.builds.sdk_requests import PORTFOLIO_ENDPOINT, invoke_portfolio

    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    scenario.submit()
    identity = None
    for _ in range(20):
        scenario.runtime.step_job()
        with Session(scenario.database_engine) as session:
            row = session.exec(
                select(ExecutionStep)
                .join(
                    SubmissionUnit,
                    (SubmissionUnit.tenant_id == ExecutionStep.tenant_id)
                    & (SubmissionUnit.unit_id == ExecutionStep.unit_id),
                )
                .where(
                    ExecutionStep.submission_id == scenario.submission_id,
                    ExecutionStep.kind == "CTA",
                    col(SubmissionUnit.expanded).is_(True),
                )
            ).first()
            if row:
                identity = row.id
                break
    assert identity
    context = scenario.scope.context
    with Session(scenario.database_engine) as session, session.begin():
        claim = submissions.claim_step(
            session, context=context, step_id=identity, owner=uuid4(), lease_seconds=2
        )
        assert claim
        frozen = submissions.load_execution_unit(
            session,
            context=context,
            submission_id=scenario.submission_id,
            unit_id=claim.unit_id,
        ).frozen
        body = prepare_request(session, context=context, claim=claim, frozen=frozen)
        arm_request(session, context=context, claim=claim, body=body)
    with Redis.from_url(settings.REDIS_URL, decode_responses=True) as redis_client:
        with (
            admitted_build_call(
                redis_client,
                context=context,
                endpoint=PORTFOLIO_ENDPOINT,
                advertiser_id=claim.advertiser_id,
            ),
            ExitStack() as stack,
        ):
            with Session(scenario.database_engine) as session:
                client = stack.enter_context(
                    sdk_client(
                        session,
                        context=context,
                        connection_id=scenario.scope.connection_id,
                    )
                )
            result = invoke_portfolio(client, body=body)
    # The remote effect happened; its reply has not been committed locally.
    time.sleep(2.1)
    with Session(scenario.database_engine) as session, session.begin():
        step = session.exec(
            select(ExecutionStep).where(ExecutionStep.id == identity).with_for_update()
        ).one()
        assert expire_attempt(session, step=step) == "RECONCILE"
        assert (
            submissions.claim_step(
                session, context=context, step_id=identity, owner=uuid4()
            )
            is None
        )
        preserve_created_receipt(session, claim=claim, result=result)
        submission = session.get(Submission, scenario.submission_id)
        queue_step(session, step=step, submission=submission, reconcile=True)
    observed = []

    def late_receipt(path):
        if not path.endswith("/creative/portfolio/get/"):
            return
        scenario.runtime.wire.before_get = None
        with Session(scenario.database_engine) as session, session.begin():
            current = session.get(ExecutionStep, identity)
            token, attempt, expiry = (
                current.lease_token,
                current.attempt,
                current.lease_expires_at,
            )
            assert token and token != claim.lease_token
            record_created(session, claim=claim, result=result)
            session.refresh(current)
            assert (current.lease_token, current.attempt, current.lease_expires_at) == (
                token,
                attempt,
                expiry,
            )
            assert current.remote_id is None
            observed.append(token)

    scenario.runtime.wire.before_get = late_receipt
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        scenario.runtime.step_job()
        with Session(scenario.database_engine) as session:
            current = session.get(ExecutionStep, identity)
            if current.status == "SUCCEEDED":
                assert current.remote_id == result.remote_id
                assert session.exec(
                    select(StepEvidence).where(
                        StepEvidence.step_id == identity,
                        StepEvidence.conclusion == "LATE_CREATED",
                    )
                ).all()
                break
        time.sleep(0.02)
    else:
        pytest.fail(scenario.runtime.diagnostics())
    assert len(observed) == 1
    assert (
        len(
            [
                row
                for row in scenario.runtime.wire.portfolios.values()
                if row["advertiser_id"] == claim.advertiser_id
            ]
        )
        <= 2
    )
    assert (
        sum(
            row["creative_portfolio_id"] == result.remote_id
            for row in scenario.runtime.wire.portfolios.values()
        )
        == 1
    )


def test_platform_delegation_records_actual_actor_and_target_tenant(
    acceptance_scenario,
):
    from app.jobs.models import PendingDispatch
    from app.modules.builds.models import BuildDraft, DraftPreparation

    scenario = acceptance_scenario

    def database_session():
        with Session(scenario.database_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database_session
    try:
        with TestClient(app) as client:
            token = client.post(
                "/api/login/access-token",
                data={"username": scenario.scope.platform_email, "password": PASSWORD},
            )
            assert token.status_code == 200
            headers = {"Authorization": "Bearer " + token.json()["access_token"]}
            tenant_id = scenario.other.context.tenant_id
            created = client.post(
                f"/api/tenants/{tenant_id}/build-drafts",
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "bc_id": scenario.other.bc_id,
                    "strategy_version_id": str(scenario.other.version_id),
                    "provider_connection_id": str(scenario.other.provider_id),
                    "application_id": scenario.other.application_id,
                    "drama_lines": ["The Bond"],
                    "account_lines": list(scenario.other.accounts),
                    "link_config": {"episode": 1},
                },
            )
            assert created.status_code == 201
            draft_id = created.json()["draft_id"]
            prepared = client.post(
                f"/api/tenants/{tenant_id}/build-drafts/{draft_id}/prepare",
                headers=headers,
                json={"request_id": str(uuid4())},
            )
            assert prepared.status_code == 202
            with Session(scenario.database_engine) as session:
                from uuid import UUID

                row = session.get(BuildDraft, UUID(draft_id))
                assert (
                    row.tenant_id == tenant_id
                    and row.created_by == scenario.scope.platform_id
                )
                prep = session.get(DraftPreparation, UUID(prepared.json()["task_id"]))
                assert prep.actor_id == scenario.scope.platform_id
                dispatched = session.exec(
                    select(PendingDispatch).where(
                        PendingDispatch.tenant_id == tenant_id
                    )
                ).all()
                assert dispatched and all(
                    message.actor_id == scenario.scope.platform_id
                    for message in dispatched
                )
            assert not scenario.runtime.wire.calls
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
