"""真实 PG：切片等待不轮询投递，前片落定后自动登记下一片。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, col, select

from app.jobs.models import PendingDispatch
from app.modules.builds.dependency_waits import wake_material_dependencies
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.material_execution import (
    plan_material_slice,
    recover_material_results,
)
from app.modules.materials.models import MaterialDistribution
from tests.modules.builds.test_material_matrix_planning import (
    executable as executable,
)
from tests.modules.builds.test_material_matrix_planning import (
    expanded_inventory as expanded_inventory,
)
from tests.modules.builds.test_material_matrix_planning import (
    isolated_strategy_database as isolated_strategy_database,
)
from tests.modules.builds.test_material_matrix_planning import (
    material_count as material_count,
)
from tests.modules.builds.test_material_matrix_planning import (
    matrix as matrix,
)
from tests.modules.builds.test_material_matrix_planning import (
    skip_one as skip_one,
)
from tests.modules.builds.test_material_matrix_planning import (
    skipped_inventory as skipped_inventory,
)


def park_future(matrix):
    database, context, unit_ids, _ = matrix
    plan_material_slice(database_engine=database, context=context, unit_id=unit_ids[0])
    with Session(database) as db, db.begin():
        future = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "MATERIAL",
                col(ExecutionStep.distribution_id).is_(None),
            )
        ).all()
        for step in future:
            step.error_code = "execution_window_wait"
            step.updated_at = datetime.now(UTC) - timedelta(days=1)
        assert len(future) == 20
        return {step.id for step in future}


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_active_slice_waiters_do_not_publish_repeated_step_messages(matrix):
    database, context, _, _ = matrix
    future_ids = park_future(matrix)
    with Session(database) as db:
        before = set(
            db.exec(
                select(PendingDispatch.id).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                )
            ).all()
        )
    assert wake_material_dependencies(database_engine=database) == 0
    assert wake_material_dependencies(database_engine=database) == 0
    with Session(database) as db:
        assert (
            set(
                db.exec(
                    select(PendingDispatch.id).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                    )
                ).all()
            )
            == before
        )
        assert all(
            db.get(ExecutionStep, identity).dispatch_id is None
            for identity in future_ids
        )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_settled_slice_registers_next_slice_before_any_future_step_delivery(matrix):
    database, context, _, _ = matrix
    future_ids = park_future(matrix)
    with Session(database) as db, db.begin():
        # 这里模拟既有分发状态机的明确终态，未补写远端成功或目标VID。
        for distribution in db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == context.tenant_id,
            )
        ).all():
            distribution.status = "blocked"
            distribution.reason_code = "fixture_definite_no_effect"
    wake_material_dependencies(database_engine=database)
    with Session(database) as db:
        future = [db.get(ExecutionStep, identity) for identity in future_ids]
        assert all(step.distribution_id is not None for step in future)
        assert all(
            step.error_code == "material_pending" and step.dispatch_id is None
            for step in future
        )
        assert (
            len(
                db.exec(
                    select(MaterialDistribution.id).where(
                        MaterialDistribution.tenant_id == context.tenant_id,
                    )
                ).all()
            )
            == 220
        )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_active_slice_waiters_do_not_starve_direct_settlement_at_page_limit(matrix):
    database, context, _, _ = matrix
    park_future(matrix)
    with Session(database) as db, db.begin():
        step = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                col(ExecutionStep.distribution_id).is_not(None),
            )
        ).first()
        identity = step.id
        distribution = db.get(MaterialDistribution, step.distribution_id)
        distribution.status = "blocked"
        distribution.reason_code = "fixture_definite_no_effect"
    # 明确终态在通用等待分页之前由恢复器领取，不再排一条 Builds 消息。
    assert recover_material_results(database_engine=database, limit=1) == 1
    assert wake_material_dependencies(database_engine=database, limit=1) == 0
    with Session(database) as db:
        assert db.get(ExecutionStep, identity).status == "FAILED"
        assert db.get(ExecutionStep, identity).dispatch_id is None


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_slow_material_does_not_block_refilling_free_material_slots(matrix):
    database, context, _, _ = matrix
    future_ids = park_future(matrix)
    with Session(database) as db, db.begin():
        distributions = db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == context.tenant_id,
            )
        ).all()
        slow_material = min(row.material_id for row in distributions)
        slow_ids = {row.id for row in distributions if row.material_id == slow_material}
        assert len(slow_ids) == 10
        for distribution in distributions:
            if distribution.material_id != slow_material:
                # 模拟其余十九条素材的明确终态，不伪造成功回执或目标副本。
                distribution.status = "blocked"
                distribution.reason_code = "fixture_definite_no_effect"
    wake_material_dependencies(database_engine=database)
    with Session(database) as db:
        future = [db.get(ExecutionStep, identity) for identity in future_ids]
        assert all(step.distribution_id is not None for step in future)
        assert all(
            step.error_code == "material_pending" and step.dispatch_id is None
            for step in future
        )
        active = db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == context.tenant_id,
                col(MaterialDistribution.status).in_(
                    ["queued", "preparing", "verifying"]
                ),
            )
        ).all()
        active_materials = {row.material_id for row in active}
        assert active_materials == {slow_material} | {
            step.material_id for step in future
        }
        assert len(active_materials) == 3
        assert len(active_materials) <= 20
        assert len(active) == 30
        assert slow_ids <= {row.id for row in active}
        scheduled = set(
            db.exec(
                select(col(PendingDispatch.payload)["distribution_id"].astext).where(
                    PendingDispatch.tenant_id == context.tenant_id,
                    PendingDispatch.task_name == "materials.prepare_target",
                )
            ).all()
        )
        assert {str(step.distribution_id) for step in future} <= scheduled
