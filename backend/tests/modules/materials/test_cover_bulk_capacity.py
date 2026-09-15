"""本地容量：真实 PG 候选分页及 SDK HTTP 边界；不冒充完整任务或平台吞吐。"""

import json
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

import pytest
from sqlalchemy import event, text, update
from sqlmodel import Session, col, select

from app.integrations.tiktok.contracts.materials import RemoteCallBudget
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.jobs.admission import admission_policy
from app.jobs.models import PendingDispatch
from app.modules.materials import covers
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.models import AccountMaterial, MaterialFile
from tests.modules.materials.test_channel_covers import (
    app_config as app_config,
)
from tests.modules.materials.test_channel_covers import (
    cover_env as cover_env,
)
from tests.modules.materials.test_channel_covers import (
    database_engine as database_engine,
)
from tests.modules.materials.test_channel_covers import (
    gateway_case as gateway_case,
)
from tests.modules.materials.test_channel_covers import (
    gateway_wire as gateway_wire,
)
from tests.modules.materials.test_channel_covers import (
    policy as policy,
)


def seed_cover_queue(env, engine, count):
    """批量准备相同授权下的有效记录，素材与VID映射均各自独立。"""
    common = {"tenant_id": env["context"].tenant_id, "bc_id": env["route"].bc_id}
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        materials = [
            MaterialFile(
                **common,
                file_name=f"capacity-{i}.mp4",
                object_key=f"offline/{uuid4()}",
                byte_size=120,
                video_md5="a" * 32,
                sha256="b" * 64,
                storage_state="unavailable",
            )
            for i in range(count)
        ]
        db.add_all(materials)
        db.flush()
        assets = [
            AccountMaterial(
                **common,
                material_id=material.id,
                advertiser_id=env["advertiser"],
                connection_id=env["route"].connection_id,
                video_id=f"capacity-video-{i}",
                status="available",
                verified_at=now,
            )
            for i, material in enumerate(materials)
        ]
        db.add_all(assets)
        db.flush()
        jobs, dispatches = [], []
        for i, asset in enumerate(assets):
            identity, dispatch_id = uuid4(), uuid4()
            dispatches.append(
                PendingDispatch(
                    id=dispatch_id,
                    tenant_id=common["tenant_id"],
                    actor_id=env["context"].actor_id,
                    task_name="materials.verify_cover",
                    task_key=f"cover:{identity}:1",
                    payload={"job_id": str(identity), "revision": 1},
                    available_at=now,
                )
            )
            jobs.append(
                MaterialCoverJob(
                    id=identity,
                    **common,
                    material_id=asset.material_id,
                    asset_id=asset.id,
                    advertiser_id=env["advertiser"],
                    connection_id=env["route"].connection_id,
                    actor_id=env["context"].actor_id,
                    frozen_route=env["route"].model_dump(mode="json"),
                    video_id=asset.video_id,
                    video_md5="a" * 32,
                    known_image_id=f"capacity-image-{i}",
                    remote_name=f"capacity-{i}.jpg",
                    signature="c" * 32,
                    width=360,
                    height=640,
                    request_armed_at=now,
                    status="VERIFYING",
                    dispatch_id=dispatch_id,
                    revision=1,
                )
            )
        db.add_all(dispatches)
        db.flush()
        db.add_all(jobs)
        db.flush()
        return {job.id for job in jobs}


@pytest.mark.parametrize("count", [100, 1000, 10000])
@pytest.mark.parametrize("gateway_case", ["OFFICIAL_API"], indirect=True)
def test_indexed_cover_groups_and_actual_sdk_arrays_scale_linearly(
    cover_env,
    gateway_wire,
    database_engine,
    redis_client,
    count,
):
    identities = seed_cover_queue(cover_env, database_engine, count)
    seen, sizes = set(), []
    planning_seconds = transport_seconds = 0.0
    plans = []
    with Session(database_engine) as db:
        db.execute(text("ANALYZE material_cover_job"))
        db.execute(text("ANALYZE pending_dispatch"))
        db.commit()
        while True:
            started = perf_counter()
            first = db.exec(
                select(MaterialCoverJob)
                .where(
                    MaterialCoverJob.tenant_id == cover_env["context"].tenant_id,
                    MaterialCoverJob.status == "VERIFYING",
                )
                .order_by(col(MaterialCoverJob.id))
                .limit(1)
            ).first()
            if first is None:
                break
            connection = db.connection()
            captured = []

            def capture(
                _connection, _cursor, statement, parameters, *_, captured=captured
            ):
                captured.append((statement, parameters))

            inspect_plan = len(sizes) in {0, count // 100, count // 50 - 1}
            if inspect_plan:
                event.listen(connection, "before_cursor_execute", capture)
            try:
                group = [first, *db.exec(covers._known_cover_candidates(first)).all()]
            finally:
                if inspect_plan:
                    event.remove(connection, "before_cursor_execute", capture)
            if captured:
                statement, parameters = captured[0]
                plans.append(
                    connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement,
                        parameters,
                    ).scalar_one()[0]["Plan"]
                )
            group_ids = {job.id for job in group}
            assert not seen & group_ids
            seen.update(group_ids)
            sizes.append(len(group))
            video_ids = tuple(job.video_id for job in group)
            image_ids = tuple(job.known_image_id for job in group)
            # 仅让下一轮生产候选SQL看到这一组已出队，未调用业务发布或伪造运行回执。
            db.execute(
                update(MaterialCoverJob)
                .where(col(MaterialCoverJob.id).in_(group_ids))
                .values(status="READY")
            )
            db.commit()
            planning_seconds += perf_counter() - started
            deadline = datetime.now(UTC) + timedelta(seconds=40)
            started = perf_counter()
            with open_tiktok_gateway(
                database_engine=database_engine,
                redis_client=redis_client,
                context=cover_env["context"],
                route=cover_env["route"],
                task_deadline=deadline,
            ) as gateway:
                for kind, requested in (("video", video_ids), ("image", image_ids)):
                    gateway_wire["sdk_data"]["data"] = {
                        "list": [{f"{kind}_id": value} for value in requested]
                    }
                    budget = RemoteCallBudget(
                        deadline,
                        45,
                        admission_policy(f"materials.get_{kind}s").lease_ms,
                    )
                    actual = getattr(gateway.materials, f"read_{kind}s")(
                        advertiser_id=cover_env["advertiser"],
                        **{f"{kind}_ids": requested},
                        budget=budget,
                    )
                    assert (
                        tuple(getattr(row, f"{kind}_id") for row in actual) == requested
                    )
            transport_seconds += perf_counter() - started
    assert seen == identities
    assert sizes == [50] * (count // 50)
    assert len(gateway_wire["sdk_calls"]) == count // 25

    def nodes(plan):
        yield plan
        for child in plan.get("Plans", []):
            yield from nodes(child)

    shape = [node for plan in plans for node in nodes(plan)]
    # 小表顺序扫描属于合理成本选择；10,000条时必须使用限定索引并免去全组排序。
    if count == 10000:
        assert not any(
            node["Node Type"] == "Sort" and node["Actual Loops"] for node in shape
        ), plans
        scanned = [
            node for node in shape if node.get("Relation Name") == "material_cover_job"
        ]
        assert all(
            node["Actual Rows"] + node.get("Rows Removed by Filter", 0) <= 55
            for node in scanned
        ), scanned
    print(  # noqa: T201
        "CAPACITY "
        + json.dumps(
            {
                "jobs": count,
                "groups": len(sizes),
                "sdk_http_calls": len(gateway_wire["sdk_calls"]),
                "pg_grouping_seconds": round(planning_seconds, 4),
                "local_sdk_seconds": round(transport_seconds, 4),
                "scope": "PG candidate partition and local HTTP, not full cover jobs or remote throughput",
            }
        )
    )
