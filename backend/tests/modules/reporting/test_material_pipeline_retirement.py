"""旧素材链路退休：拒绝新需求，旧消息不能再访问上游。"""

from datetime import UTC, datetime

import pytest

from app.core.errors import DomainError
from app.modules.reporting.jobs import enqueue_job, execute_job
from app.modules.reporting.store_models import ReportSyncJob
from tests.modules.reporting.test_jobs import D, _route


@pytest.mark.parametrize("kind", ["material_day", "material_links"])
def test_retired_kind_refuses_new_demand(session, sync_seed, kind):
    with pytest.raises(DomainError, match="退休|停用"):
        enqueue_job(
            session,
            tenant_id=sync_seed.tenant_id,
            bc_id="bc-a",
            kind=kind,
            tier="history",
            advertiser_ids=["acc-1"],
            start=D,
            end=D,
            frozen_route=_route(sync_seed),
            actor_id=sync_seed.actor_id,
        )


def test_retired_running_message_is_fenced_without_reader(committed_sync_seed):
    from uuid import uuid4

    seed, factory = committed_sync_seed
    token = uuid4()
    with factory() as s:
        job = ReportSyncJob(
            tenant_id=seed.tenant_id,
            bc_id="bc-a",
            kind="material_links",
            priority=2,
            advertiser_ids=["acc-1"],
            start_date=D,
            end_date=D,
            frozen_route=_route(seed),
            actor_id=seed.actor_id,
            job_key=str(uuid4()),
            status="RUNNING",
            claim_token=token,
            claimed_until=datetime.now(UTC),
            due_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        job_id = job.id

    class Reader:
        def __getattr__(self, name):
            raise AssertionError("retired job must not access provider")

    assert (
        execute_job(factory, job_id=job_id, claim_token=token, reader=Reader())
        == "CANCELLED"
    )


def test_retirement_keeps_evidence_and_invalidates_only_legacy_work(session, sync_seed):
    import importlib
    from uuid import uuid4

    from sqlalchemy import text

    common = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "priority": 4,
        "advertiser_ids": ["acc-1"],
        "start_date": D,
        "end_date": D,
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "due_at": datetime.now(UTC),
    }
    old = ReportSyncJob(**common, kind="material_links", job_key=str(uuid4()))
    session.add(old)
    new_id = enqueue_job(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="ad_day",
        tier="history",
        advertiser_ids=["acc-1"],
        start=D,
        end=D,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
    )
    session.flush()
    importlib.import_module(
        "app.alembic.versions.0114_retire_material_pipeline"
    ).retire(session.connection())
    session.expire_all()
    assert session.get(ReportSyncJob, old.id).status == "CANCELLED"
    assert session.get(ReportSyncJob, old.id).error_code == "report_kind_retired"
    assert session.get(ReportSyncJob, new_id).status == "QUEUED"
    # 新需求、冻结子桶和调度仍保留；不把已取消旧工作伪造为平台已发布。
    assert (
        session.connection()
        .execute(
            text(
                "SELECT count(*) FROM sync_execution_scope WHERE report_job_id=:job AND status='PENDING'"
            ),
            {"job": new_id},
        )
        .scalar_one()
        > 0
    )


def test_retirement_downgrade_does_not_revive_cancelled_jobs(session, sync_seed):
    import importlib
    from uuid import uuid4

    module = importlib.import_module(
        "app.alembic.versions.0114_retire_material_pipeline"
    )
    old = ReportSyncJob(
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        kind="material_day",
        priority=4,
        advertiser_ids=["acc-1"],
        start_date=D,
        end_date=D,
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        due_at=datetime.now(UTC),
        job_key=str(uuid4()),
    )
    session.add(old)
    session.flush()
    module.retire(session.connection())
    session.refresh(old)
    retired = (old.status, old.claim_generation, old.finished_at)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(session.connection())):
        module.downgrade()
    session.refresh(old)
    assert retired == (old.status, old.claim_generation, old.finished_at)
    assert old.status == "CANCELLED" and old.error_code == "report_kind_retired"
