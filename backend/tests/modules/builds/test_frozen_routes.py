"""冻结父路由与稳定 attempt 身份；锁和持久化使用真实 PostgreSQL。"""

from uuid import uuid4

import pytest
from sqlmodel import select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.connection_models import (
    BCConnectionBinding,
    BCDefaultRoute,
    ConnectionAuthorization,
)
from app.modules.accounts.models import TikTokConnection
from app.modules.builds.execution_state import arm_request
from app.modules.builds.routes import (
    inherit_route,
    load_preview_route,
    save_preview_route,
    stable_attempt_id,
)
from tests.modules.builds.test_execution_state import body


@pytest.fixture
def attempt(session, context):
    """纯数据库冻结事实；不替换 scene、gateway 或任何业务调用。"""
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.execution_schemas import StepClaim
    from tests.modules.builds.test_route_migration import historical_rows

    _, preview, step = historical_rows(session, context=context, current=True)
    step.status, step.phase = "RUNNING", "CLAIMED"
    step.lease_token = uuid4()
    step.lease_expires_at = datetime.now(UTC) + timedelta(seconds=60)
    session.flush()
    claim = StepClaim(
        step_id=step.id,
        tenant_id=context.tenant_id,
        submission_id=step.submission_id,
        preview_id=preview.id,
        unit_id=step.unit_id,
        actor_id=context.actor_id,
        bc_id=preview.bc_id,
        advertiser_id="account-A",
        kind="CAMPAIGN",
        group_id=None,
        planned_ad_id=None,
        material_id=None,
        parent_step_id=None,
        lease_token=step.lease_token,
        lease_expires_at=step.lease_expires_at,
        attempt=step.attempt,
        attempt_id=step.attempt_id,
        dispatch_revision=0,
        route=load_preview_route(session, context=context, preview_id=preview.id),
    )
    return step, claim


def test_child_keeps_route_and_rejects_cross_bc():
    route = FrozenTikTokRoute(
        tenant_id=uuid4(),
        bc_id="bc-a",
        connection_id=uuid4(),
        channel="OFFICIAL_MCP",
        authorization_revision=3,
        adapter_contract_revision="build-v1",
    )
    assert inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-a") == route
    changed_default = route.model_copy(update={"connection_id": uuid4()})
    assert (
        inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-a") != changed_default
    )
    with pytest.raises(DomainError):
        inherit_route(route, tenant_id=route.tenant_id, bc_id="bc-b")


def replacement_default(session, context, route):
    replacement = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
    session.add(replacement)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            connection_id=replacement.id,
            kind="OFFICIAL_API",
        )
    )
    session.flush()
    session.get(
        BCDefaultRoute, (context.tenant_id, route.bc_id)
    ).connection_id = replacement.id
    session.flush()
    return replacement


def test_parent_route_is_idempotent_immutable_and_independent_of_current_default(
    session, context, attempt
):
    _, claim = attempt
    original = load_preview_route(session, context=context, preview_id=claim.preview_id)
    assert original == claim.route
    replacement = replacement_default(session, context, original)
    save_preview_route(
        session, context=context, preview_id=claim.preview_id, route=original
    )
    assert (
        load_preview_route(session, context=context, preview_id=claim.preview_id)
        == original
    )
    with pytest.raises(DomainError) as error:
        save_preview_route(
            session,
            context=context,
            preview_id=claim.preview_id,
            route=original.model_copy(update={"connection_id": replacement.id}),
        )
    assert error.value.code == "frozen_route_changed"
    arm_request(session, context=context, claim=claim, body=body(claim))


@pytest.mark.parametrize(
    "change", ["credential", "authorization", "contract", "read_scope"]
)
def test_arm_uses_frozen_semantic_versions_but_accepts_credential_rotation(
    session, context, attempt, change
):
    step, claim = attempt
    connection = session.get(TikTokConnection, claim.route.connection_id)
    if change == "credential":
        connection.credential_revision += 1
    elif change == "authorization":
        connection.authorization_revision += 1
    elif change == "contract":
        connection.adapter_contract_revision = "new-contract"
    else:
        auth = session.exec(
            select(ConnectionAuthorization).where(
                ConnectionAuthorization.connection_id == connection.id
            )
        ).one()
        auth.permission_summary = {**auth.permission_summary, "build_authorized": False}
    session.flush()
    if change == "credential":
        arm_request(session, context=context, claim=claim, body=body(claim))
        assert step.attempt_id == claim.attempt_id
    else:
        with pytest.raises(DomainError):
            arm_request(session, context=context, claim=claim, body=body(claim))
        assert step.request_body is None


def test_attempt_companion_keeps_original_counter_and_rejects_forged_identity(
    session, context, attempt
):
    from app.modules.builds.execution_state import record_created
    from app.modules.builds.route_models import BuildAttemptContext
    from app.modules.builds.sdk_requests import RemoteCreated

    step, claim = attempt
    saved = session.get(BuildAttemptContext, (context.tenant_id, step.id, step.attempt))
    assert (
        saved.attempt_id
        == claim.attempt_id
        == stable_attempt_id(tenant_id=context.tenant_id, step_id=step.id, attempt=1)
    )
    assert step.attempt == 1
    forged = claim.model_copy(update={"attempt_id": uuid4()})
    with pytest.raises(DomainError):
        record_created(session, claim=forged, result=RemoteCreated("v", None, "ENABLE"))


def test_route_context_database_rejects_rewrite(session, context, attempt):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    _, claim = attempt
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE build_route_context SET authorization_revision=authorization_revision+1 WHERE tenant_id=:tenant AND preview_id=:preview"
            ),
            {"tenant": context.tenant_id, "preview": claim.preview_id},
        )


@pytest.mark.parametrize("same", [True, False])
def test_independent_transactions_can_freeze_parent_only_once(
    isolated_strategy_database, same
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlmodel import Session

    from app.modules.accounts.routing import freeze_route
    from app.modules.builds.route_models import BuildRouteContext
    from tests.modules.builds.test_drafts import account
    from tests.modules.builds.test_route_migration import historical_rows

    engine, _, _ = isolated_strategy_database
    with Session(engine) as session, session.begin():
        context, preview, _ = historical_rows(session, connections=0)
        account(session, context)
        original = freeze_route(session, context=context, bc_id=preview.bc_id)
        replacement = replacement_default(session, context, original)
        other = (
            original
            if same
            else original.model_copy(update={"connection_id": replacement.id})
        )
        preview_id = preview.id
    barrier = Barrier(2)

    def save(route):
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                save_preview_route(
                    session, context=context, preview_id=preview_id, route=route
                )
                session.commit()
                return "saved"
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, (original, other)))
    assert sorted(outcomes) == (
        ["saved", "saved"] if same else ["frozen_route_changed", "saved"]
    )
    with Session(engine) as session:
        assert (
            len(
                session.exec(
                    select(BuildRouteContext).where(
                        BuildRouteContext.preview_id == preview_id
                    )
                ).all()
            )
            == 1
        )
        assert load_preview_route(session, context=context, preview_id=preview_id) in (
            original,
            other,
        )


def test_new_default_missing_draft_account_terminates_preview(
    session, context, attempt
):
    from app.modules.accounts.routing import freeze_route
    from app.modules.builds import previews
    from app.modules.builds.models import DraftAccount
    from app.modules.builds.preview_models import (
        BuildPreview,
        PreviewDrama,
        PreviewInput,
    )
    from app.modules.strategies.schemas import StrategyConfig
    from tests.modules.strategies.test_versions import config

    _, claim = attempt
    original = session.get(BuildPreview, claim.preview_id)
    row = BuildPreview(
        **{
            **original.model_dump(),
            "id": uuid4(),
            "draft_revision": 2,
            "status": "BUILDING",
            "batch_short_id": "NEXT",
        }
    )
    session.add(row)
    session.flush()
    replacement_default(session, context, claim.route)
    route = freeze_route(session, context=context, bc_id=row.bc_id)
    save_preview_route(session, context=context, preview_id=row.id, route=route)
    original_drama = session.exec(
        select(PreviewDrama).where(PreviewDrama.preview_id == original.id)
    ).one()
    session.add(PreviewDrama(**{**original_drama.model_dump(), "preview_id": row.id}))
    session.add(
        DraftAccount(
            tenant_id=context.tenant_id,
            draft_id=row.draft_id,
            bc_id=row.bc_id,
            advertiser_id=claim.advertiser_id,
            connection_id=claim.route.connection_id,
            currency="USD",
            timezone="UTC",
            first_line=1,
        )
    )
    session.add(
        PreviewInput(
            tenant_id=context.tenant_id,
            preview_id=row.id,
            bc_id=row.bc_id,
            kind="account",
            line_no=1,
            raw_text=claim.advertiser_id,
            status="READY",
        )
    )
    row.progress = {
        "current_drama": str(original_drama.drama_id),
        "unit_id": None,
        "account_after": "",
    }
    session.flush()
    with pytest.raises(
        DomainError, check=lambda error: error.code == "account_not_in_bc"
    ):
        previews._expand_unit(
            session, context, row, StrategyConfig.model_validate(config())
        )
    assert row.status != "FROZEN"
    assert session.exec(
        select(PreviewInput).where(
            PreviewInput.preview_id == row.id, PreviewInput.kind == "account"
        )
    ).all()


def test_attempt_database_rejects_mismatched_current_id_and_companion_rewrite(
    session, context, attempt
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    step, claim = attempt
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("UPDATE execution_step SET attempt_id=:id WHERE id=:step"),
            {"id": uuid4(), "step": step.id},
        )
        session.execute(text("SET CONSTRAINTS fk_step_current_attempt IMMEDIATE"))
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text(
                "UPDATE build_attempt_context SET attempt_id=:id WHERE tenant_id=:tenant AND step_id=:step AND attempt=:attempt"
            ),
            {
                "id": uuid4(),
                "tenant": context.tenant_id,
                "step": step.id,
                "attempt": claim.attempt,
            },
        )


def test_new_route_cannot_save_unverified_authorization_revision(session, context):
    from app.modules.accounts.routing import freeze_route
    from tests.modules.builds.test_drafts import account
    from tests.modules.builds.test_route_migration import historical_rows

    _, preview, _ = historical_rows(session, context=context, connections=0)
    account(session, context)
    route = freeze_route(session, context=context, bc_id=preview.bc_id)
    with pytest.raises(
        DomainError, check=lambda error: error.code == "route_authorization_changed"
    ):
        save_preview_route(
            session,
            context=context,
            preview_id=preview.id,
            route=route.model_copy(
                update={"authorization_revision": route.authorization_revision + 1}
            ),
        )
