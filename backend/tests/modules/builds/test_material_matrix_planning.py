"""真实 PostgreSQL 验证发送前登记整片需求；不替换素材准备业务逻辑。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from time import perf_counter

import pytest
from sqlalchemy import delete, event
from sqlmodel import Session, col, select

from app.core.config import settings
from app.integrations.tiktok.admission import PROTOCOL_OPERATIONS
from app.jobs.models import PendingDispatch
from app.modules.accounts.models import AdvertiserAccount, TenantBC, TikTokConnection
from app.modules.builds import execution_window, material_execution
from app.modules.builds.dispatch import process_unit
from app.modules.builds.execution_models import (
    ExecutionStep,
    Submission,
    SubmissionUnit,
)
from app.modules.builds.preview_models import BuildUnit, PreviewSkippedMaterial
from app.modules.builds.routes import load_preview_route
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
)
from app.modules.materials.readiness import get_material_readiness
from tests.modules.builds import test_execution
from tests.modules.builds.test_drafts import account
from tests.modules.builds.test_execution import executable as executable
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def material_count(request):
    return getattr(request, "param", 2)


@pytest.fixture
def skip_one(request):
    return getattr(request, "param", False)


@pytest.fixture(autouse=True)
def skipped_inventory(skip_one, monkeypatch):
    if not skip_one:
        return
    drain_preview = test_execution.drain

    def drain_with_unavailable_target(db, context, preview_id):
        # 真实冻结前使A账户的一条副本不可用；生产准备规则会排除该缺少摘要
        # 的副本，其他账户已验证副本仍然可用，不伪造readiness返回值。
        mapping = db.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.advertiser_id == "account-A",
            )
            .order_by(col(AccountMaterial.material_id))
        ).first()
        mapping.status = "unavailable"
        db.flush()
        return drain_preview(db, context, preview_id)

    monkeypatch.setattr(test_execution, "drain", drain_with_unavailable_target)


@pytest.fixture(autouse=True)
def expanded_inventory(material_count, monkeypatch):
    if material_count == 2:
        return
    create_material = test_execution.material

    def create_with_extra_inventory(db, context, name, **kwargs):
        # 仅扩展已有测试夹具的建数辅助函数；真实预览和提交仍从完整库存生成，
        # 不绕过冻结触发器，也不替换任何生产准备/共享实现。
        if name == "Moon-0.mp4":
            connection = db.exec(
                select(TikTokConnection).where(
                    TikTokConnection.tenant_id == context.tenant_id
                )
            ).one()
            accounts = db.exec(
                select(AdvertiserAccount.advertiser_id).where(
                    AdvertiserAccount.tenant_id == context.tenant_id
                )
            ).all()
            for index in range(2, material_count):
                extra = create_material(db, context, f"Moon-{index}.mp4", **kwargs)
                for identity in accounts:
                    db.add(
                        AccountMaterial(
                            tenant_id=context.tenant_id,
                            bc_id="bc-draft",
                            material_id=extra.id,
                            advertiser_id=identity,
                            connection_id=connection.id,
                            video_id=f"extra-target-{index}",
                            image_id=f"extra-cover-{index}",
                            status="available",
                            verified_at=datetime.now(UTC),
                        )
                    )
        return create_material(db, context, name, **kwargs)

    monkeypatch.setattr(test_execution, "material", create_with_extra_inventory)


@pytest.fixture
def matrix(executable, monkeypatch, material_count):
    database, context, identities = executable
    monkeypatch.setattr(execution_window, "MAX_ACTIVE_UNITS", 10)
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            **settings.TIKTOK_CALL_POLICIES,
            "endpoints": {
                operation: {"lease_ms": 970000}
                for operation in {*PROTOCOL_OPERATIONS, "materials.share_assets"}
            },
        },
    )
    # 网络替身仅放在传输边界；规划自身不得读取或发送远端请求。
    monkeypatch.setattr(
        "urllib3.PoolManager.request",
        lambda *_a, **_kw: pytest.fail("本地素材矩阵规划不得访问远端"),
    )
    with Session(database) as db, db.begin():
        units = db.exec(
            select(BuildUnit).where(BuildUnit.tenant_id == context.tenant_id)
        ).all()
        assert len(units) == 10
        material_ids = set(
            db.exec(
                select(ExecutionStep.material_id).where(
                    ExecutionStep.tenant_id == context.tenant_id,
                    ExecutionStep.kind == "MATERIAL",
                )
            ).all()
        )
        assert len(material_ids) == material_count
        connection = db.exec(
            select(TikTokConnection).where(
                TikTokConnection.tenant_id == context.tenant_id
            )
        ).one()
        primary = "matrix-primary"
        account(db, context, primary)
        db.get(
            TenantBC, (context.tenant_id, "bc-draft")
        ).material_advertiser_id = primary
        # 保留冻结预览、步骤和证据，仅重建可变账户库存，使首次准备必须走共享。
        # 主素材账户具备真实授权和真实本地 VID/MID，所有十个目标尚无副本。
        db.execute(
            delete(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id
            )
        )
        for index, identity in enumerate(sorted(material_ids)):
            material = db.get(MaterialFile, identity)
            material.video_md5 = f"{index + 1:032x}"
            db.add(
                AccountMaterial(
                    tenant_id=context.tenant_id,
                    bc_id="bc-draft",
                    material_id=identity,
                    advertiser_id=primary,
                    connection_id=connection.id,
                    video_id=f"matrix-source-video-{index}",
                    mid=f"matrix-source-mid-{index}",
                    status="available",
                    verified_at=datetime.now(UTC),
                )
            )
        db.flush()
        route = load_preview_route(db, context=context, preview_id=units[0].preview_id)
        for unit in units:
            for identity in material_ids:
                readiness = get_material_readiness(
                    db,
                    context=context,
                    bc_id=unit.bc_id,
                    material_id=identity,
                    advertiser_id=unit.advertiser_id,
                    route=route,
                )
                assert (readiness.state, readiness.path) == (
                    "preparable",
                    "share_source",
                ), readiness
        unit_ids = [unit.id for unit in units]
    return database, context, unit_ids, material_ids


def assert_complete_matrix(matrix):
    database, context, unit_ids, material_ids = matrix
    with Session(database) as db:
        steps = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).all()
        distributions = db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == context.tenant_id
            )
        ).all()
        assert len(distributions) == 20
        by_id = {row.id: row for row in distributions}
        units = {identity: db.get(BuildUnit, identity) for identity in unit_ids}
        assert len(steps) == 20
        assert {(step.unit_id, step.material_id) for step in steps} == {
            (unit_id, material_id)
            for unit_id in unit_ids
            for material_id in material_ids
        }
        for step in steps:
            assert step.distribution_id in by_id
            distribution = by_id[step.distribution_id]
            assert (distribution.material_id, distribution.advertiser_id) == (
                step.material_id,
                units[step.unit_id].advertiser_id,
            )
            assert (step.status, step.error_code) == ("PENDING", "material_pending")
            operation = db.get(MaterialAssetOperation, distribution.operation_id)
            assert operation.status == "pending"
            assert operation.remote_response["transport"] == "native_share"
            assert operation.remote_response["source_advertiser_id"] == "matrix-primary"
            assert not operation.remote_response.get("send_armed")
            assert (
                db.exec(
                    select(PendingDispatch.id).where(
                        PendingDispatch.tenant_id == context.tenant_id,
                        PendingDispatch.task_name == "materials.prepare_target",
                        col(PendingDispatch.payload)["distribution_id"].astext
                        == str(distribution.id),
                    )
                ).first()
                is not None
            )
        return {row.id for row in distributions}


@pytest.mark.parametrize("executable", [10], indirect=True)
def test_first_planning_registers_two_materials_for_all_ten_accounts(matrix):
    database, context, unit_ids, _ = matrix
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[0]
    )
    original = assert_complete_matrix(matrix)
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[-1]
    )
    assert assert_complete_matrix(matrix) == original


@pytest.mark.parametrize("executable", [10], indirect=True)
def test_unit_dispatch_plans_all_targets_before_individual_material_workers(matrix):
    database, context, unit_ids, _ = matrix
    with Session(database) as db:
        unit = db.exec(
            select(SubmissionUnit).where(SubmissionUnit.unit_id == unit_ids[0])
        ).one()
        revision = unit.dispatch_revision
    process_unit(
        database_engine=database,
        context=context,
        payload={"unit_id": str(unit_ids[0]), "revision": revision},
    )
    assert_complete_matrix(matrix)


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("stale", ["revision", "dispatch"])
def test_stale_unit_delivery_cannot_register_a_material_slice(matrix, stale):
    database, context, unit_ids, _ = matrix
    with Session(database) as db, db.begin():
        unit = db.exec(
            select(SubmissionUnit).where(SubmissionUnit.unit_id == unit_ids[0])
        ).one()
        revision = unit.dispatch_revision
        if stale == "revision":
            unit.dispatch_revision += 1
        else:
            unit.dispatch_id = None
    assert (
        process_unit(
            database_engine=database,
            context=context,
            payload={"unit_id": str(unit_ids[0]), "revision": revision},
        )
        == 0
    )
    with Session(database) as db:
        assert not db.exec(
            select(MaterialDistribution.id).where(
                MaterialDistribution.tenant_id == context.tenant_id
            )
        ).all()


@pytest.mark.parametrize("executable", [10], indirect=True)
def test_incomplete_submission_cannot_start_an_early_single_account_slice(matrix):
    database, context, unit_ids, _ = matrix
    with Session(database) as db, db.begin():
        submission = db.exec(
            select(Submission).where(Submission.tenant_id == context.tenant_id)
        ).one()
        submission.expanded = False
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[0]
    )
    with Session(database) as db:
        assert not db.exec(
            select(MaterialDistribution.id).where(
                MaterialDistribution.tenant_id == context.tenant_id
            )
        ).all()
        assert not db.exec(
            select(ExecutionStep.id).where(
                ExecutionStep.tenant_id == context.tenant_id,
                col(ExecutionStep.distribution_id).is_not(None),
            )
        ).all()


@pytest.mark.parametrize("executable", [10], indirect=True)
def test_concurrent_unit_planners_register_one_shared_matrix(matrix):
    database, context, unit_ids, _ = matrix
    barrier = Barrier(2)

    def plan(identity):
        barrier.wait(timeout=10)
        return material_execution.plan_material_slice(
            database_engine=database, context=context, unit_id=identity
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(plan, identity) for identity in unit_ids[:2]]
        for result in results:
            result.result(timeout=30)
    assert_complete_matrix(matrix)


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("skip_one", [True], indirect=True)
def test_planning_preserves_frozen_account_specific_material_exclusion(matrix):
    database, context, unit_ids, material_ids = matrix
    with Session(database) as db:
        skipped = db.exec(
            select(PreviewSkippedMaterial).where(
                PreviewSkippedMaterial.tenant_id == context.tenant_id,
            )
        ).one()
        excluded = (skipped.unit_id, skipped.material_id)
        assert skipped.reason_code == "material_digest_missing"
        assert db.get(BuildUnit, skipped.unit_id).advertiser_id == "account-A"
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[0]
    )
    with Session(database) as db:
        steps = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).all()
        assert {(step.unit_id, step.material_id) for step in steps} == {
            (unit_id, material_id)
            for unit_id in unit_ids
            for material_id in material_ids
        } - {excluded}
        assert len(steps) == 19
        assert all(step.distribution_id is not None for step in steps)
        distributions = db.exec(
            select(MaterialDistribution).where(
                MaterialDistribution.tenant_id == context.tenant_id,
            )
        ).all()
        assert len(distributions) == 19
        assert not any(
            row.advertiser_id == "account-A" and row.material_id == excluded[1]
            for row in distributions
        )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_first_slice_caps_at_twenty_materials_without_partial_target_fanout(matrix):
    database, context, unit_ids, material_ids = matrix
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[0]
    )
    with Session(database) as db:
        rows = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "MATERIAL",
            )
        ).all()
        prepared = [row for row in rows if row.distribution_id is not None]
        waiting = [row for row in rows if row.distribution_id is None]
        assert len(prepared) == 200
        assert len(waiting) == 20
        selected_materials = {row.material_id for row in prepared}
        assert len(selected_materials) == 20
        assert selected_materials < material_ids
        assert {(row.unit_id, row.material_id) for row in prepared} == {
            (unit_id, material_id)
            for unit_id in unit_ids
            for material_id in selected_materials
        }
        assert {row.material_id for row in waiting}.isdisjoint(selected_materials)
        assert (
            len(
                db.exec(
                    select(MaterialDistribution.id).where(
                        MaterialDistribution.tenant_id == context.tenant_id
                    )
                ).all()
            )
            == 200
        )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_unit_dispatch_does_not_bypass_slice_cap_with_lazy_material_workers(matrix):
    database, context, unit_ids, _ = matrix
    for identity in unit_ids[:2]:
        with Session(database) as db:
            unit = db.exec(
                select(SubmissionUnit).where(SubmissionUnit.unit_id == identity)
            ).one()
            revision = unit.dispatch_revision
        process_unit(
            database_engine=database,
            context=context,
            payload={"unit_id": str(identity), "revision": revision},
        )
        with Session(database) as db:
            future = db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.tenant_id == context.tenant_id,
                    ExecutionStep.kind == "MATERIAL",
                    col(ExecutionStep.distribution_id).is_(None),
                )
            ).all()
            assert len(future) == 20
            assert all(
                row.dispatch_id is None and row.status == "PENDING" for row in future
            )
            assert (
                len(
                    db.exec(
                        select(MaterialDistribution.id).where(
                            MaterialDistribution.tenant_id == context.tenant_id
                        )
                    ).all()
                )
                == 200
            )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [22], indirect=True)
def test_old_direct_material_delivery_cannot_prepare_outside_active_slice(
    matrix, redis_client
):
    from app.modules.builds.execution import process_step

    database, context, unit_ids, _ = matrix
    material_execution.plan_material_slice(
        database_engine=database, context=context, unit_id=unit_ids[0]
    )
    with Session(database) as db:
        future = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.tenant_id == context.tenant_id,
                ExecutionStep.kind == "MATERIAL",
                col(ExecutionStep.distribution_id).is_(None),
            )
        ).first()
        step_id, revision = future.id, future.dispatch_revision
    process_step(
        database_engine=database,
        redis_client=redis_client,
        context=context,
        step_id=step_id,
        revision=revision,
    )
    with Session(database) as db:
        future = db.get(ExecutionStep, step_id)
        assert future.distribution_id is None
        assert future.status == "PENDING"
        assert (
            len(
                db.exec(
                    select(MaterialDistribution.id).where(
                        MaterialDistribution.tenant_id == context.tenant_id
                    )
                ).all()
            )
            == 200
        )


@pytest.mark.parametrize("executable", [10], indirect=True)
@pytest.mark.parametrize("material_count", [20], indirect=True)
def test_full_twenty_by_ten_slice_computes_execution_window_once(
    matrix, record_property
):
    database, context, unit_ids, _ = matrix
    statements = []

    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(database, "before_cursor_execute", counted)
    started = perf_counter()
    try:
        material_execution.plan_material_slice(
            database_engine=database, context=context, unit_id=unit_ids[0]
        )
    finally:
        elapsed = perf_counter() - started
        event.remove(database, "before_cursor_execute", counted)
    windows = sum("row_number() OVER" in statement for statement in statements)
    record_property("planning_sql_count", len(statements))
    record_property("planning_seconds", elapsed)
    assert windows == 1
    assert len(statements) < 5000, (
        f"20x10 planning emitted {len(statements)} SQL statements"
    )
    with Session(database) as db:
        assert (
            len(
                db.exec(
                    select(ExecutionStep.id).where(
                        ExecutionStep.tenant_id == context.tenant_id,
                        col(ExecutionStep.distribution_id).is_not(None),
                    )
                ).all()
            )
            == 200
        )
