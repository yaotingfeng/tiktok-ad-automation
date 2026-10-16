from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlmodel import delete, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.ads import EntityRef
from app.jobs.models import PendingDispatch
from app.modules.ads.directory_requests import request_directory
from app.modules.reporting import job_tasks
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.sync_conftest import sync_seed as sync_seed


def test_recovery_promotes_old_targeted_read_ahead_of_full_directory(
    session, sync_seed, monkeypatch
):
    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    args = {
        "tenant_id": sync_seed.tenant_id,
        "bc_id": "bc-a",
        "frozen_route": _route(sync_seed),
        "actor_id": sync_seed.actor_id,
        "advertiser_ids": ["acc-1"],
    }
    assert request_directory(session, scope="directory", **args) == 10
    assert (
        request_directory(
            session,
            scope="targeted",
            refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
            **args,
        )
        == 3
    )
    # 模拟旧版已持久化但尚未消费的回读：新派发器应先恢复它，而非十条全量任务。
    session.exec(
        delete(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    )
    monkeypatch.setattr(job_tasks, "DIRECTORY_DISPATCH_BATCH", 1)
    assert job_tasks._dispatch_directories(session, datetime.now(UTC)) == 1
    dispatch = session.exec(
        select(PendingDispatch).where(
            PendingDispatch.tenant_id == sync_seed.tenant_id,
        )
    ).one()
    assert dispatch.task_name == "ads.targeted_step"


@pytest.mark.parametrize("foreign_tenant", [False, True])
def test_targeted_entry_rejects_full_or_foreign_runs(
    session, sync_seed, monkeypatch, foreign_tenant
):
    from app.modules.ads import tasks
    from app.modules.ads.sync_models import AdDirectoryRun

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)
    request_directory(
        session,
        tenant_id=sync_seed.tenant_id,
        bc_id="bc-a",
        frozen_route=_route(sync_seed),
        actor_id=sync_seed.actor_id,
        advertiser_ids=["acc-1"],
        scope="targeted" if foreign_tenant else "directory",
        refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")]
        if foreign_tenant
        else (),
    )
    run = session.exec(
        select(AdDirectoryRun).where(AdDirectoryRun.tenant_id == sync_seed.tenant_id)
    ).first()
    monkeypatch.setattr(tasks, "engine", session.connection())
    with pytest.raises(DomainError, match="定向回读缺少对象范围"):
        tasks.targeted_step(
            tenant_id=str(uuid4() if foreign_tenant else sync_seed.tenant_id),
            actor_id=str(sync_seed.actor_id),
            payload={"run_id": str(run.id), "claim_generation": 1},
        )
