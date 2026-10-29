"""本地准入拒绝不应把没有发送的 IMAGE 共享变成永久只读核查。"""

from contextlib import contextmanager

from sqlmodel import Session, select

from app.core.db import engine
from app.integrations.tiktok import gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.materials.cover_models import MaterialCoverShareBatch
from tests.modules.materials.test_cover_throughput import drive, image, matrix, page
from tests.modules.materials.test_covers import job_state, run
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_local_share_admission_deferral_resumes_write_without_unknown_scan(
    source_env, redis_client, wire, monkeypatch, caplog
):
    import logging

    from app.core import diagnostics

    monkeypatch.setattr(diagnostics.logger, "disabled", False)
    caplog.set_level(logging.INFO, logger=diagnostics.logger.name)
    identity = matrix(source_env, 1, 1)[0]
    original = gateway.admit_tiktok_call

    @contextmanager
    def deferred(*args, **kwargs):
        if kwargs["operation"] == "materials.share_assets":
            raise AccountAdmissionDeferred(2000, reason="account_concurrency")
        with original(*args, **kwargs):
            yield

    wire[1].extend([{"list": [image(0)]}, page([])])
    with monkeypatch.context() as patch:
        patch.setattr(gateway, "admit_tiktok_call", deferred)
        run(source_env, redis_client, identity)
    job = job_state(identity)
    assert not any(call[0] == "POST" for call in wire[0])
    assert job.request_armed_at is None
    with Session(engine) as db:
        batch = db.exec(
            select(MaterialCoverShareBatch).where(
                MaterialCoverShareBatch.tenant_id == source_env["context"].tenant_id
            )
        ).one()
        assert job.share_batch_id is None
        assert batch.armed_at is not None
        assert batch.status == "BLOCKED"
        assert batch.error_code == "cover_share_not_sent"
        assert batch.request_id is None
    wire[1].extend([page([]), {"failed_infos": {}}, page([image(0, target_id=True)])])
    drive(source_env, redis_client, identity)
    drive(source_env, redis_client, identity, read=True)
    assert job_state(identity).status == "READY"
    assert sum(call[0] == "POST" for call in wire[0]) == 1


def test_unknown_share_transport_is_never_rearmed(source_env, redis_client, wire):
    identity = matrix(source_env, 1, 1)[0]
    wire[1].extend([{"list": [image(0)]}, page([]), TimeoutError("private-url")])
    run(source_env, redis_client, identity)
    assert job_state(identity).request_armed_at is not None
    assert job_state(identity).known_image_id is None
    wire[1].extend([page([]), page([])])
    drive(source_env, redis_client, identity, read=True)
    assert sum(call[0] == "POST" for call in wire[0]) == 1
