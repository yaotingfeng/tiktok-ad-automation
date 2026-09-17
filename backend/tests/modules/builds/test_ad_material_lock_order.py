"""真实 PG：广告排列与批量核验锁序不同，不能形成素材行锁环。"""

from threading import Thread
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.modules.builds.execution import prepare_request
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.submissions import claim_step, load_execution_unit
from app.modules.materials.models import MaterialFile
from tests.modules.builds.test_execution import executable as executable


@pytest.fixture
def descending_executable(monkeypatch, request):
    from tests.modules.materials import test_tenant_materials

    # 在生成冻结预览之前固定两份测试素材身份；不修改已冻结的顺序。
    identities = iter([UUID(int=2), UUID(int=1)])
    monkeypatch.setattr(test_tenant_materials, "uuid4", lambda: next(identities))
    return request.getfixturevalue("executable")


def test_ad_preparation_waits_in_material_id_order_without_reordering_creatives(
    descending_executable,
):
    database, context, ids = descending_executable
    with Session(database) as db, db.begin():
        for kind in ("MATERIAL", "CTA", "CAMPAIGN", "ADGROUP"):
            for identity in ids[kind]:
                step = db.get(ExecutionStep, identity)
                step.status, step.phase = "SUCCEEDED", "DONE"
                if kind != "MATERIAL":
                    step.remote_id = f"existing-{kind}"
        db.flush()
        claim = claim_step(db, context=context, step_id=ids["AD"][0], owner=uuid4())
        assert claim is not None
        frozen = load_execution_unit(
            db,
            context=context,
            submission_id=claim.submission_id,
            unit_id=claim.unit_id,
        ).frozen

    application = f"ad-lock-order-{uuid4().hex}"
    result, failures = [], []

    def prepare():
        try:
            with Session(database) as db, db.begin():
                db.execute(text("SET LOCAL statement_timeout='8s'"))
                db.execute(
                    text("SELECT set_config('application_name',:name,true)"),
                    {"name": application},
                )
                result.append(
                    prepare_request(db, context=context, claim=claim, frozen=frozen)
                )
        except Exception as error:
            failures.append(error)

    contender = Thread(target=prepare)
    high_locked = False
    waiting = False
    try:
        with Session(database) as holder, holder.begin():
            # 模拟素材批读持有低 ID，再准备读取高 ID 的固定锁序。
            holder.exec(
                select(MaterialFile)
                .where(MaterialFile.id == UUID(int=1))
                .with_for_update()
            ).one()
            contender.start()
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with database.connect() as check:
                    waiting = bool(
                        check.execute(
                            text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name=:name AND wait_event_type='Lock')"
                            ),
                            {"name": application},
                        ).scalar_one()
                    )
                if waiting or failures:
                    break
                sleep(0.01)
            with Session(database) as check:
                try:
                    check.exec(
                        select(MaterialFile)
                        .where(MaterialFile.id == UUID(int=2))
                        .with_for_update(nowait=True)
                    ).one()
                except OperationalError as error:
                    assert error.orig.sqlstate == "55P03"
                    high_locked = True
                finally:
                    check.rollback()
    finally:
        contender.join(timeout=10)
    assert waiting, failures
    assert not contender.is_alive()
    assert not high_locked, "广告先锁住高 ID 再等低 ID，会与批量核验形成死锁"
    assert not failures
    assert [
        item["creative_info"]["video_info"]["video_id"]
        for item in result[0]["creative_list"]
    ] == ["target-0", "target-1"]
