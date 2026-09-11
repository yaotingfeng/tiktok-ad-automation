"""真实 PostgreSQL claim 与 SDK HTTP 边界并发。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlmodel import Session, select

from app.modules.builds import scene
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses

BOUNDED_GUARD = scene._require_bounded_worker


@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_two_deliveries_have_one_request_and_no_open_database_transaction(
    database_engine, redis_client, scene_case, gateway_wire
):
    receipt = ensure(database_engine, scene_case)
    enqueue(gateway_wire, "identity", scene_responses(scene_case)["identity"])
    entered, release = Event(), Event()

    def remote():
        gateway_wire["before"]["callback"] = None
        with Session(database_engine) as db:
            job = db.exec(
                select(SceneJob)
                .where(SceneJob.id == receipt.job_id)
                .with_for_update(nowait=True)
            ).one()
            assert job.claim_token is not None
        entered.set()
        assert release.wait(5)

    gateway_wire["before"]["callback"] = remote
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            run, database_engine, redis_client, scene_case, receipt.job_id
        )
        try:
            assert entered.wait(5)
            duplicate = pool.submit(
                run, database_engine, redis_client, scene_case, receipt.job_id
            )
            assert duplicate.result(timeout=3).claim_token is not None
        finally:
            release.set()
        result = first.result(timeout=5)
    assert result.resource == "minis"
    assert len(gateway_wire["sdk_calls"]) == 1
    with Session(database_engine) as db:
        assert (
            len(
                db.exec(
                    select(SceneJobPage).where(SceneJobPage.job_id == receipt.job_id)
                ).all()
            )
            == 1
        )
