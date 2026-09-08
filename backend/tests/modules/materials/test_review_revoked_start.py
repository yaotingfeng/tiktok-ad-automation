"""Independent review: a blocked initial delivery must remain recoverable."""

from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.db import engine
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.modules.materials.models import ObjectUpload
from app.modules.tenants.models import TenantMembership
from tests.modules.materials.test_retry_api import retry_client as retry_client
from tests.modules.materials.test_source_uploads import (
    run,
)
from tests.modules.materials.test_source_uploads import (
    source_env as source_env,
)
from tests.modules.materials.test_source_uploads import (
    wire as wire,
)


def test_initial_actor_revocation_leaves_recoverable_block_after_authority_restored(
    source_env,
    redis_client,
    wire,
    retry_client,
):
    context = source_env["context"]
    with Session(engine) as session, session.begin():
        dispatch_id = enqueue_after_commit(
            session,
            context=context,
            task_name="materials.upload_original",
            task_key=f"upload-original:{source_env['material_id']}",
            payload={"material_id": str(source_env["material_id"])},
        )
        # The worker received its ordinary completed-object delivery.
        session.get(PendingDispatch, dispatch_id).published_at = datetime.now(UTC)
        upload = session.exec(
            select(ObjectUpload).where(
                ObjectUpload.material_id == source_env["material_id"],
            )
        ).one()
        upload.task_id = dispatch_id
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "viewer"
    run(source_env, redis_client, kind="upload")
    assert wire[0] == []
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "operator"
    client, path = retry_client
    progress = client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()[
        "files"
    ][0]
    retry = client.post(f"{path}/{source_env['material_id']}/retry")
    assert (
        progress["status"],
        progress["error_code"],
        progress["can_retry"],
        retry.status_code,
    ) == (
        "blocked",
        "action_forbidden",
        True,
        200,
    )


def test_revoked_duplicate_cannot_turn_unknown_receipt_into_retry(
    source_env, redis_client, wire, retry_client
):
    import pytest

    from app.core.errors import DomainError
    from tests.modules.materials.test_source_uploads import seed_operation

    seed_operation(
        source_env, status="result_unknown", evidence={"video_id": "known-receipt"}
    )
    context = source_env["context"]
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "viewer"
    with pytest.raises(DomainError):
        run(source_env, redis_client, kind="upload")
    with Session(engine) as session, session.begin():
        session.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "operator"
    client, path = retry_client
    file = client.get(f"{path}/upload-batches/{source_env['batch_id']}").json()[
        "files"
    ][0]
    assert file["status"] == "result_unknown" and not file["can_retry"]
    assert client.post(f"{path}/{source_env['material_id']}/retry").status_code == 409
    assert wire[0] == []
