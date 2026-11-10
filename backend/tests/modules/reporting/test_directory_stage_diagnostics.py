"""目录子阶段旁观原返回、调用数和异常。"""

from uuid import uuid4

import pytest

from app.modules.ads.directory_requests import request_directory
from app.modules.reporting import diagnostics
from tests.modules.ads.test_directory_requests import _route
from tests.modules.reporting.test_reporting_stage_diagnostics import capture as capture
from tests.modules.reporting.test_reporting_stage_diagnostics import records


def test_directory_instrumentation_preserves_creation_and_idempotency(
    session, sync_seed, monkeypatch, capture
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)

    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        kwargs = {
            "tenant_id": sync_seed.tenant_id,
            "bc_id": "bc-a",
            "frozen_route": _route(sync_seed),
            "actor_id": sync_seed.actor_id,
            "advertiser_ids": ["acc-1"],
            "scope": "directory",
        }
        assert request_directory(session, **kwargs) == 10
        assert request_directory(session, **kwargs) == 0
        return "WAITING"

    step(job_id=uuid4())
    (row,) = records(capture)
    assert row["stages"]["directory_verify_routes"]["calls"] == 2
    assert row["stages"]["directory_slot_lock"]["calls"] == 2
    assert row["stages"]["directory_existing"]["calls"] == 20
    assert row["stages"]["directory_flush"]["calls"] == 2
    assert not row["job_commit_confirmed"]


@pytest.mark.parametrize("immediate", [True, False])
def test_targeted_outbox_single_call_is_not_commit(
    session, sync_seed, monkeypatch, capture, immediate
):
    from sqlmodel import select

    from app.core.config import settings
    from app.integrations.tiktok.contracts.ads import EntityRef
    from app.jobs.models import PendingDispatch

    monkeypatch.setattr(settings, "ADS_SYNC_ENABLED", True)

    @diagnostics.summarize_job
    def step(*, job_id):
        assert job_id is not None
        assert (
            request_directory(
                session,
                tenant_id=sync_seed.tenant_id,
                bc_id="bc-a",
                frozen_route=_route(sync_seed),
                actor_id=sync_seed.actor_id,
                advertiser_ids=["acc-1"],
                scope="targeted",
                refs=[EntityRef(sync_seed.tenant_id, "acc-1", "ad", "changed-ad")],
                ad_types={"acc-1": {"SMART_PLUS": ("ad",)}},
                dispatch_immediately=immediate,
            )
            == 1
        )
        return "WAITING"

    step(job_id=uuid4())
    rows = session.exec(
        select(PendingDispatch).where(PendingDispatch.tenant_id == sync_seed.tenant_id)
    ).all()
    assert len(rows) == 1
    # 两种请求都先唤醒canonical调度；只有真实领取配额和head后才发业务步骤。
    assert rows[0].task_name == "reporting.plan_due"
    assert rows[0].payload == {"dispatch_only": True}
    assert rows[0].dispatch_contract_version == 2
    (row,) = records(capture)
    assert row["stages"]["directory_flush"]["calls"] == 2
    assert row["stages"]["directory_outbox"]["calls"] == 1
    assert not row["job_commit_confirmed"]
