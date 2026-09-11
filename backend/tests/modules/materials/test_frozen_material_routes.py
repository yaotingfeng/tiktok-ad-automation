"""Frozen material authority survives queue delay and never changes asset identity."""

from tests.modules.materials.route_support import second_connection
from tests.modules.materials.test_remote_only_distribution import (
    remote_env as remote_env,
)
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire
from tests.modules.materials.test_url_ingest import url_env as url_env


def test_route_loader_rejects_history_scope_and_coerced_revision():
    from uuid import uuid4

    import pytest

    from app.core.context import TenantContext
    from app.core.errors import DomainError
    from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
    from app.modules.materials.routes import load_material_route

    context = TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")
    route = FrozenTikTokRoute(
        tenant_id=context.tenant_id,
        bc_id="bc",
        connection_id=uuid4(),
        channel="OFFICIAL_API",
        authorization_revision=1,
        adapter_contract_revision="official-api-v1",
    )
    for value in (
        None,
        {},
        {**route.model_dump(mode="json"), "authorization_revision": True},
        {**route.model_dump(mode="json"), "authorization_revision": "1"},
    ):
        with pytest.raises(DomainError, match="历史素材连接信息需要核实"):
            load_material_route(value, context=context, bc_id="bc")
    with pytest.raises(DomainError) as error:
        load_material_route(
            route.model_dump(mode="json"), context=context, bc_id="other"
        )
    assert error.value.code == "frozen_route_scope_mismatch"


def test_default_switch_during_source_get_keeps_both_original_routes(
    remote_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.connection_models import BCDefaultRoute
    from tests.modules.materials.test_distribution import queue, run, state
    from tests.modules.materials.test_remote_only_distribution import info

    prepared = queue(remote_env, remote_env["target"])
    with Session(engine) as db, db.begin():
        replacement = second_connection(db, remote_env)

    def switch_default():
        with Session(engine) as db, db.begin():
            db.get(
                BCDefaultRoute, (remote_env["context"].tenant_id, remote_env["bc_id"])
            ).connection_id = replacement
        return info()

    wire[1].extend([switch_default, [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "verifying" and mapping is None
    assert dist.target_route["connection_id"] == str(remote_env["connection_id"])
    assert dist.source_route["connection_id"] == str(remote_env["connection_id"])
    assert op.remote_response["upload_connection_id"] == str(
        remote_env["connection_id"]
    )
    assert [item[0] for item in wire[0]] == ["GET", "POST"]


def test_changed_authorization_blocks_queued_target_without_http(
    remote_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.models import TikTokConnection
    from tests.modules.materials.test_distribution import queue, run, state

    prepared = queue(remote_env, remote_env["target"])
    with Session(engine) as db, db.begin():
        db.get(
            TikTokConnection, remote_env["connection_id"]
        ).authorization_revision += 1
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert (
        dist.status == "blocked" and dist.reason_code == "route_authorization_changed"
    )
    assert (
        mapping is None and not op.remote_response.get("send_armed") and wire[0] == []
    )


def test_credential_only_rotation_keeps_queued_target_usable(
    remote_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.credentials import encrypt_credentials
    from app.core.db import engine
    from app.modules.accounts.models import TikTokConnection
    from tests.modules.materials.test_distribution import queue, run, state
    from tests.modules.materials.test_remote_only_distribution import info

    prepared = queue(remote_env, remote_env["target"])
    with Session(engine) as db, db.begin():
        connection = db.get(TikTokConnection, remote_env["connection_id"])
        connection.credential_revision += 1
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=connection.tenant_id,
            value={"access_token": "rotated-offline-token"},
        )
    wire[1].extend([info(), [{"video_id": "actual-target"}]])
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    assert state(prepared.task_id)[0].status == "verifying"
    assert [item[0] for item in wire[0]] == ["GET", "POST"]


def test_cover_inherits_target_route_without_relabeling_uploaded_video(
    source_env, wire
):
    from uuid import uuid4

    import pytest
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.core.errors import DomainError
    from app.modules.accounts.routing import freeze_route
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import ensure_cover
    from app.modules.materials.models import AccountMaterial
    from tests.modules.materials.test_readiness import asset

    with Session(engine) as db, db.begin():
        actual = asset(db, source_env, "actual-account")
        identity = actual.id
        second = second_connection(db, source_env)
        route = freeze_route(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            connection_id=second,
        )
        result = ensure_cover(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id="actual-account",
            task_key=f"new:{uuid4()}",
            route=route,
        )
        assert result.state == "queued"
    with Session(engine) as db, db.begin():
        old_route = freeze_route(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            connection_id=source_env["connection_id"],
        )
        with pytest.raises(DomainError) as error:
            ensure_cover(
                db,
                context=source_env["context"],
                bc_id=source_env["bc_id"],
                material_id=source_env["material_id"],
                advertiser_id="actual-account",
                task_key=f"new:{uuid4()}",
                route=old_route,
            )
        assert error.value.code == "frozen_route_changed"
        assert (
            db.get(AccountMaterial, identity).connection_id
            == source_env["connection_id"]
        )
        jobs = db.exec(
            select(MaterialCoverJob).where(MaterialCoverJob.asset_id == identity)
        ).all()
        assert len(jobs) == 1 and jobs[0].connection_id == second
    assert wire[0] == []


def test_ingest_default_switch_before_first_delivery_uses_accepted_route(
    url_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.connection_models import BCDefaultRoute
    from tests.modules.materials.test_url_ingest import operation, run

    with Session(engine) as db, db.begin():
        replacement = second_connection(db, url_env)
        db.get(
            BCDefaultRoute, (url_env["context"].tenant_id, url_env["bc_id"])
        ).connection_id = replacement
    wire[1].append([{"video_id": "first-source-vid"}])
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "verifying"
    assert op.frozen_route["connection_id"] == str(url_env["connection_id"])
    assert op.remote_response["connection_id"] == str(url_env["connection_id"])
    assert len(wire[0]) == 1 and wire[0][0][0] == "POST"


def test_persisted_material_route_cannot_be_rebound(remote_env):
    import pytest
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialDistribution,
    )
    from tests.modules.materials.test_distribution import queue, state

    prepared = queue(remote_env, remote_env["target"])
    dist, op, _ = state(prepared.task_id)
    for model, identity, column in (
        (MaterialDistribution, dist.id, "target_route"),
        (MaterialDistribution, dist.id, "source_route"),
        (MaterialAssetOperation, op.id, "frozen_route"),
    ):
        with Session(engine) as db, pytest.raises(DBAPIError):
            db.execute(update(model).where(model.id == identity).values({column: None}))
            db.commit()
    assert state(prepared.task_id)[0].target_route == dist.target_route


def test_source_authorization_change_after_get_prevents_target_post(
    remote_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.models import TikTokConnection
    from tests.modules.materials.test_distribution import queue, run, state
    from tests.modules.materials.test_remote_only_distribution import info

    with Session(engine) as db, db.begin():
        target_connection = second_connection(db, remote_env)
    prepared = queue(
        {**remote_env, "connection_id": target_connection}, remote_env["target"]
    )

    def revoke_source():
        with Session(engine) as db, db.begin():
            db.get(
                TikTokConnection, remote_env["connection_id"]
            ).authorization_revision += 1
        return info()

    wire[1].append(revoke_source)
    run(remote_env, redis_client, prepared.task_id, kind="prepare")
    dist, op, mapping = state(prepared.task_id)
    assert dist.status == "blocked"
    assert dist.target_route["connection_id"] == str(target_connection)
    assert dist.source_route["connection_id"] == str(remote_env["connection_id"])
    assert not op.remote_response.get("send_armed") and mapping is None
    assert [call[0] for call in wire[0]] == ["GET"]


def test_source_readback_remains_read_only_when_upload_permission_is_removed(
    source_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.accounts.models import BCAccountAccess
    from tests.modules.materials.test_source_uploads import (
        info,
        run,
        seed_operation,
        snapshot,
    )

    identity = seed_operation(source_env)
    with Session(engine) as db, db.begin():
        db.get(
            BCAccountAccess,
            (
                source_env["context"].tenant_id,
                source_env["bc_id"],
                "actual-account",
                source_env["connection_id"],
            ),
        ).can_upload = False
    wire[1].append(info())
    run(source_env, redis_client, operation_id=identity)
    operation, _, mapping = snapshot(source_env, identity)
    assert operation.status == "succeeded" and mapping is not None
    assert [call[0] for call in wire[0]] == ["GET"]


def test_foreign_tenant_route_never_creates_material_dependency(remote_env, wire):
    from uuid import uuid4

    import pytest
    from sqlmodel import Session

    from app.core.db import engine
    from app.core.errors import DomainError
    from app.modules.accounts.routing import freeze_route
    from app.modules.materials.distribution import ensure_target_asset

    with Session(engine) as db, db.begin():
        route = freeze_route(
            db,
            context=remote_env["context"],
            bc_id=remote_env["bc_id"],
            connection_id=remote_env["connection_id"],
        )
        foreign = route.model_copy(update={"tenant_id": uuid4()})
        with pytest.raises(DomainError) as error:
            ensure_target_asset(
                db,
                context=remote_env["context"],
                bc_id=remote_env["bc_id"],
                material_id=remote_env["material_id"],
                advertiser_id=remote_env["target"],
                task_key="cross-tenant",
                route=foreign,
            )
        assert error.value.code == "frozen_route_scope_mismatch"
    assert wire[0] == []


def test_current_viewer_can_finish_existing_source_readback_without_upload(
    source_env, redis_client, wire
):
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.modules.tenants.models import TenantMembership
    from tests.modules.materials.test_source_uploads import (
        info,
        run,
        seed_operation,
        snapshot,
    )

    identity = seed_operation(source_env)
    with Session(engine) as db, db.begin():
        db.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == source_env["context"].tenant_id,
                TenantMembership.user_id == source_env["context"].actor_id,
            )
        ).one().role = "viewer"
    wire[1].append(info())
    run(source_env, redis_client, operation_id=identity)
    assert snapshot(source_env, identity)[0].status == "succeeded"
    assert [call[0] for call in wire[0]] == ["GET"]


def test_historical_unknown_source_route_stays_null_and_never_sends(
    source_env, redis_client, wire
):
    from sqlmodel import Session

    from app.core.db import engine
    from app.modules.materials.models import (
        MaterialAssetOperation,
        MaterialUploadAttempt,
    )
    from tests.modules.materials.test_source_uploads import run, snapshot

    with Session(engine) as db, db.begin():
        operation = MaterialAssetOperation(
            tenant_id=source_env["context"].tenant_id,
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id="actual-account",
            path="upload_original",
            status="result_unknown",
            request_digest="a" * 64,
            remote_response={"video_id": "historic-real-vid", "send_armed": True},
        )
        db.add(operation)
        db.flush()
        db.add(
            MaterialUploadAttempt(
                tenant_id=operation.tenant_id,
                bc_id=operation.bc_id,
                material_id=operation.material_id,
                advertiser_id=operation.advertiser_id,
                connection_id=source_env["connection_id"],
                operation_id=operation.id,
                status="result_unknown",
                request_digest=operation.request_digest,
            )
        )
        identity = operation.id
    run(source_env, redis_client, operation_id=identity)
    operation, attempt, mapping = snapshot(source_env, identity)
    assert operation.status == "result_unknown" and operation.frozen_route is None
    assert operation.remote_response["video_id"] == "historic-real-vid"
    assert operation.remote_response["error_code"] == "material_route_unverified"
    assert attempt.connection_id == source_env["connection_id"] and mapping is None
    assert wire[0] == []


def test_multiple_cover_operations_cannot_choose_first_and_repeat_upload(
    source_env, wire
):
    from uuid import uuid4

    import pytest
    from sqlmodel import Session

    from app.core.db import engine
    from app.core.errors import DomainError
    from app.modules.accounts.routing import freeze_route
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import ensure_cover
    from tests.modules.materials.test_readiness import asset

    with Session(engine) as db, db.begin():
        asset(db, source_env, "actual-account")
        first_route = freeze_route(
            db, context=source_env["context"], bc_id=source_env["bc_id"]
        )
        first = ensure_cover(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id="actual-account",
            task_key="first",
            route=first_route,
        )
        second = second_connection(db, source_env)
        second_route = freeze_route(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            connection_id=second,
        )
        original = db.get(MaterialCoverJob, first.task_id)
        db.add(
            MaterialCoverJob(
                **{
                    **original.model_dump(),
                    "id": uuid4(),
                    "connection_id": second,
                    "frozen_route": second_route.model_dump(mode="json"),
                    "status": "UNKNOWN",
                    "dispatch_id": None,
                }
            )
        )
    with Session(engine) as db, db.begin(), pytest.raises(DomainError):
        ensure_cover(
            db,
            context=source_env["context"],
            bc_id=source_env["bc_id"],
            material_id=source_env["material_id"],
            advertiser_id="actual-account",
            task_key="again",
            route=first_route,
        )
    assert wire[0] == []
