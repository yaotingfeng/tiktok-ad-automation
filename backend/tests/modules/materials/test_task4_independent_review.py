"""Independent Task4 review: production successors and strict preview boundaries."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.models import PendingDispatch
from app.modules.materials.distribution import (
    repair_material_dispatches,
    run_distribution,
)
from app.modules.materials.models import MaterialAssetOperation
from app.modules.materials.readiness import get_material_readiness
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_distribution import queue, run, state
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_readiness import runtime_config as runtime_config
from tests.modules.materials.test_source_uploads import info
from tests.modules.materials.test_source_uploads import original_s3 as original_s3
from tests.modules.materials.test_source_uploads import run as source_run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_actual_upload_successor_survives_revocation_then_repairs_same_identity(
    source_env, redis_client, wire, original_s3
):
    ctx = source_env["context"]
    wire[1].append([{"video_id": "actual-upload-receipt"}])
    source_run(source_env, redis_client, kind="upload", s3=original_s3[0])
    with Session(engine) as session, session.begin():
        op = session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == ctx.tenant_id
            )
        ).one()
        assert op.status == "verifying"
        op_id, revision = op.id, op.remote_response["revision"]
        successor = session.exec(
            select(PendingDispatch).where(
                PendingDispatch.tenant_id == ctx.tenant_id,
                PendingDispatch.task_name == "materials.verify_original",
            )
        ).one()
        dispatch_id, payload = successor.id, dict(successor.payload)
        assert UUID(payload["operation_id"]) == op_id
        assert payload["revision"] == revision and "claim_id" not in payload
        successor.published_at = successor.available_at = datetime.now(UTC) - timedelta(
            minutes=3
        )
        session.get(TenantMembership, (ctx.tenant_id, ctx.actor_id)).role = "viewer"
    with pytest.raises(DomainError):
        source_run(source_env, redis_client, operation_id=op_id, revision=revision)
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        assert op.status == "verifying" and op.remote_response["revision"] == revision
        session.get(TenantMembership, (ctx.tenant_id, ctx.actor_id)).role = "operator"
    with Session(engine) as session, session.begin():
        assert repair_material_dispatches(session) == 1
        successor = session.get(PendingDispatch, dispatch_id)
        assert successor.published_at is None and successor.payload == payload
    wire[1].append(info(vid="readback-actual"))
    source_run(source_env, redis_client, operation_id=op_id, revision=revision)
    source_run(source_env, redis_client, operation_id=op_id, revision=revision)
    with Session(engine) as session:
        assert session.get(MaterialAssetOperation, op_id).status == "succeeded"
    assert [call[0] for call in wire[0]] == ["POST", "GET"]


def test_all_preview_paths_are_sql_read_only_with_external_boundaries_disabled(
    source_env, wire, monkeypatch
):
    with Session(engine) as session, session.begin():
        accounts = [
            target(session, source_env, advertiser_id=f"review-{i}") for i in range(3)
        ]
        asset(session, source_env, accounts[0])
        asset(session, source_env, accounts[1], seconds_old=1000)
    statements = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.split()[0].upper())

    def forbidden(*_args, **_kwargs):
        pytest.fail("readiness crossed an external or outbox boundary")

    with monkeypatch.context() as m:
        m.setattr("redis.Redis.execute_command", forbidden)
        m.setattr("boto3.client", forbidden)
        m.setattr("app.modules.materials.sdk_assets.FileApi", forbidden)
        m.setattr("app.modules.materials.distribution.enqueue_after_commit", forbidden)
        m.setattr("app.jobs.outbox.enqueue_after_commit", forbidden)
        event.listen(engine, "before_cursor_execute", observe)
        try:
            with Session(engine) as session, session.begin():
                session.execute(text("SET TRANSACTION READ ONLY"))
                result = [
                    get_material_readiness(
                        session,
                        context=source_env["context"],
                        bc_id=source_env["bc_id"],
                        material_id=source_env["material_id"],
                        advertiser_id=account,
                    )
                    for account in accounts
                ]
                assert [(r.state, r.path) for r in result] == [
                    ("ready", "existing_target"),
                    ("preparable", "existing_target"),
                    ("preparable", "upload_original"),
                ]
                assert not session.dirty and not session.new and not session.deleted
        finally:
            event.remove(engine, "before_cursor_execute", observe)
    assert set(statements) <= {"SELECT", "SET"} and wire[0] == []


def test_target_old_revision_and_wrong_actor_cannot_claim_current_work(
    source_env, redis_client, wire
):
    with Session(engine) as session, session.begin():
        account = target(session, source_env)
        asset(session, source_env, account, seconds_old=1000)
    dist_id = queue(source_env, account).task_id
    op_id = state(dist_id)[1].id
    with Session(engine) as session, session.begin():
        op = session.get(MaterialAssetOperation, op_id)
        op.remote_response = {**op.remote_response, "revision": 3}
    run(source_env, redis_client, dist_id, operation_id=op_id, revision=2)
    ctx = source_env["context"]
    with pytest.raises(DomainError, match="任务操作人"):
        run_distribution(
            database_engine=engine,
            redis_client=redis_client,
            context=TenantContext(
                tenant_id=ctx.tenant_id, actor_id=uuid4(), role="operator"
            ),
            distribution_id=dist_id,
            operation_id=op_id,
            revision=3,
            kind="verify",
        )
    assert state(dist_id)[1].attempt_token is None and wire[0] == []
    wire[1].append(info())
    run(source_env, redis_client, dist_id, operation_id=op_id, revision=3)
    assert state(dist_id)[0].status == "ready" and len(wire[0]) == 1
