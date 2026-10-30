"""临时读取超时可恢复；共享授权读锁不串行化不同任务。"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.modules.accounts.models import TikTokConnection
from app.modules.builds.scene_job_models import SceneJob
from app.modules.builds.scene_jobs import _lock_job
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
@pytest.mark.parametrize("status", ["BLOCKED", "FAILED"])
def test_explicit_prepare_recovers_timeout_at_original_page(
    database_engine, redis_client, scene_case, gateway_wire, status
):
    receipt = ensure(database_engine, scene_case)
    with Session(database_engine) as db, db.begin():
        job = db.get(SceneJob, receipt.job_id)
        job.status = status
        job.resource = "identity"
        job.failure_count = 5
        job.error_code = "tiktok_call_deadline_exceeded"
    recovered = ensure(database_engine, scene_case)
    assert recovered.state == "queued"
    assert recovered.job_id == receipt.job_id
    with Session(database_engine) as db:
        job = db.get(SceneJob, receipt.job_id)
        assert job.resource == "identity"
        assert job.next_page == 1
        assert job.failure_count == 0
    enqueue(gateway_wire, "identity", scene_responses(scene_case)["identity"])
    result = run(database_engine, redis_client, scene_case, receipt.job_id)
    assert result.status == "PENDING"
    assert result.resource == "minis"


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_different_scene_readers_share_connection_but_fence_authorization_writer(
    database_engine, scene_case
):
    receipt = ensure(database_engine, scene_case)
    with Session(database_engine) as db, db.begin():
        original = db.get(SceneJob, receipt.job_id)
        other = SceneJob.model_validate(
            {**original.model_dump(), "id": uuid4(), "scope_basis": "f" * 64}
        )
        db.add(other)
        other_id = other.id
    try:
        with Session(database_engine) as first, first.begin():
            _lock_job(
                first,
                scene_case["context"].tenant_id,
                receipt.job_id,
                shared_connection=True,
            )
            with Session(database_engine) as second, second.begin():
                second.connection().exec_driver_sql("SET LOCAL lock_timeout='100ms'")
                assert (
                    _lock_job(
                        second,
                        scene_case["context"].tenant_id,
                        other_id,
                        shared_connection=True,
                    )
                    is not None
                )
            with Session(database_engine) as writer, writer.begin():
                with pytest.raises(OperationalError):
                    writer.exec(
                        select(TikTokConnection)
                        .where(TikTokConnection.id == scene_case["route"].connection_id)
                        .with_for_update(nowait=True)
                    ).one()
    finally:
        with Session(database_engine) as db, db.begin():
            db.delete(db.get(SceneJob, other_id))


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_read_timeout_retries_then_stops_at_bounded_limit(
    database_engine, redis_client, scene_case, gateway_wire
):
    from app.core.errors import DomainError

    receipt = ensure(database_engine, scene_case)

    def timeout():
        raise DomainError("tiktok_call_deadline_exceeded", "synthetic timeout")

    gateway_wire["before"]["callback"] = timeout
    for failure in range(1, 6):
        result = run(database_engine, redis_client, scene_case, receipt.job_id)
        assert result.failure_count == failure
        assert result.status == ("FAILED" if failure == 5 else "PENDING")
        assert result.resource == "identity"
        assert result.next_page == 1
    assert ensure(database_engine, scene_case).state == "queued"
