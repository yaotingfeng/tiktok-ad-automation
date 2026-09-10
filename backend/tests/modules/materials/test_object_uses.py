from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import DomainError
from app.modules.materials.ingest_models import ObjectCleanup
from app.modules.materials.object_uses import acquire_original_use, release_object_uses
from tests.modules.materials.test_object_budget import budget_fixture


def verified_object(session, context):
    _, obj = budget_fixture(session, context)
    obj.status = "verified"
    obj.actual_bytes = obj.expected_bytes
    obj.sha256, obj.video_md5 = "a" * 64, "b" * 32
    obj.digest_verified_at = datetime.now(UTC)
    session.flush()
    return obj


def test_unknown_remote_use_does_not_expire_or_change_identity(session, context):
    obj = verified_object(session, context)
    operation_id = uuid4()
    use = acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="ingest",
        operation_id=operation_id,
        lifetime_seconds=7200,
    )
    use.expires_at = datetime.now(UTC) - timedelta(days=2)
    session.flush()
    replay = acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="ingest",
        operation_id=operation_id,
        lifetime_seconds=7200,
    )
    assert replay.id == use.id
    assert replay.status == "active"
    assert (
        release_object_uses(
            session, object_id=obj.id, purpose="ingest", operation_id=operation_id
        )
        == 1
    )
    assert (
        release_object_uses(
            session, object_id=obj.id, purpose="ingest", operation_id=operation_id
        )
        == 0
    )


def test_cleanup_closes_new_permission_but_preserves_existing_unknown_use(
    session, context
):
    obj = verified_object(session, context)
    use = acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="ingest",
        operation_id=uuid4(),
        lifetime_seconds=7200,
    )
    session.add(
        ObjectCleanup(
            tenant_id=obj.tenant_id,
            bc_id=obj.bc_id,
            material_id=obj.material_id,
            generation=obj.generation,
            reason="verified_source",
        )
    )
    session.flush()
    with pytest.raises(DomainError):
        acquire_original_use(
            session,
            context=context,
            object_id=obj.id,
            purpose="preview",
            lifetime_seconds=300,
        )
    session.refresh(use)
    assert use.status == "active"


def test_permission_scope_and_preview_lifetime_are_bounded(
    session, context, other_context
):
    obj = verified_object(session, context)
    with pytest.raises(DomainError):
        acquire_original_use(
            session,
            context=other_context,
            object_id=obj.id,
            purpose="preview",
            lifetime_seconds=300,
        )
    with pytest.raises(DomainError):
        acquire_original_use(
            session,
            context=context,
            object_id=obj.id,
            purpose="preview",
            lifetime_seconds=301,
        )
    use = acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="preview",
        lifetime_seconds=300,
    )
    assert use.expires_at <= datetime.now(UTC) + timedelta(seconds=300)
    with pytest.raises(DomainError):
        release_object_uses(session, object_id=obj.id, purpose="ingest")


def test_put_requires_reservation_and_receive_state(session, context):
    _, obj = budget_fixture(session, context)
    with pytest.raises(DomainError):
        acquire_original_use(
            session,
            context=context,
            object_id=obj.id,
            purpose="part_put",
            lifetime_seconds=60,
        )
    obj.status = "receiving"
    obj.reserved_bytes = obj.expected_bytes
    session.flush()
    use = acquire_original_use(
        session,
        context=context,
        object_id=obj.id,
        purpose="part_put",
        lifetime_seconds=60,
    )
    assert use.status == "active"
    assert release_object_uses(session, object_id=obj.id, purpose="part_put") == 1
