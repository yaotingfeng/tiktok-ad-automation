"""真实 PostgreSQL 验证创建入目录与原同步发布的衔接，不发送平台请求。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.modules.ads.directory import current_ad_materials
from app.modules.ads.models import AdMaterialReference, AdObject
from app.modules.ads.sync import _publish_entities, _publish_materials
from app.modules.ads.sync_models import AdDirectoryRun
from app.modules.builds.created_directory import publish_created_object
from app.modules.builds.execution_models import ExecutionStep
from app.modules.builds.routes import load_preview_route
from app.modules.reporting.dimension_query import build_rows
from app.modules.reporting.schemas import ReportingFilter
from app.modules.reporting.store_models import ReportSyncState
from tests.modules.builds.test_execution import executable as executable


@pytest.fixture(autouse=True)
def complete_offline_identity_catalog(monkeypatch):
    """回执专项沿用执行夹具，补齐当前目录要求的完整页计数，不访问外部接口。"""
    from app.modules.builds.scene_job_models import SceneJob
    from tests.modules.builds import test_execution

    seed = test_execution.seed_targeting_directory

    def complete(session, context, advertisers, **kwargs):
        seed(session, context, advertisers, **kwargs)
        for job in session.exec(
            select(SceneJob).where(SceneJob.tenant_id == context.tenant_id)
        ).all():
            identity = job.facts.get("identity")
            if identity and identity.get("options"):
                job.facts = {
                    **job.facts,
                    "identity": {
                        **identity,
                        "seen": len(identity["options"]),
                        "total_number": len(identity["options"]),
                        "total_page": 1,
                    },
                }
                session.add(job)
        session.flush()

    monkeypatch.setattr(test_execution, "seed_targeting_directory", complete)


@pytest.fixture
def created_step(executable, request):
    engine, context, ids = executable
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, ids["AD"][0])
        route = load_preview_route(session, context=context, preview_id=step.preview_id)
        step.status, step.phase, step.remote_id = "SUCCEEDED", "DONE", "created-ad"
        step.operation_status = None
        step.request_body = {
            "advertiser_id": "account-A",
            "adgroup_id": "created-group",
            "ad_name": "创建后的广告",
            "operation_status": "ENABLE",
            "creative_list": [
                {
                    "creative_info": {
                        "ad_format": "SINGLE_VIDEO",
                        "video_info": {"video_id": "target-0"},
                        "image_info": [{"web_uri": "cover-0"}],
                    }
                }
            ],
        }
        if getattr(request, "param", None) == "native":
            step.request_body = {
                **step.request_body,
                "creative_list": [
                    {
                        **step.request_body["creative_list"][0],
                        "smart_plus_creative_id": "created-native",
                        "ad_material_id": "created-mid",
                    }
                ],
            }
        identity = step.id
    return engine, context, identity, route


def test_new_ad_visible_without_report_and_unknown_status_is_not_invented(created_step):
    engine, context, identity, route = created_step
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        publish_created_object(session, step=step, route=route)
        obj = session.exec(select(AdObject)).one()
        assert (
            obj.operation_status is None and "operation_status" not in obj.configuration
        )
        uses = current_ad_materials(session, obj.ref)
        assert len(uses) == 1 and uses[0].platform_material_id == "target-0"
        assert uses[0].ad_material_id is None
        # 视频封面不能作为独立图片主素材。
        assert uses[0].material_type == "VIDEO"
        filters = ReportingFilter(
            dimension="ad",
            start_date=datetime.now(UTC).date(),
            end_date=datetime.now(UTC).date(),
            include_empty=True,
        )
        rows = build_rows(session, context=context, bc_id=route.bc_id, filters=filters)
        assert len(rows) == 1 and rows[0].display["name"] == "创建后的广告"
        assert not build_rows(
            session,
            context=context,
            bc_id=route.bc_id,
            filters=filters.model_copy(update={"include_empty": False}),
        )
        assert not session.exec(select(ReportSyncState)).all()
        assert not session.exec(select(AdDirectoryRun)).all()


def test_existing_sync_updates_created_directory_and_repeat_receipt_preserves_it(
    created_step,
):
    engine, context, identity, route = created_step
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        publish_created_object(session, step=step, route=route)
        obj = session.exec(select(AdObject)).one()
        run = AdDirectoryRun(
            tenant_id=context.tenant_id,
            advertiser_id="account-A",
            bc_id=route.bc_id,
            actor_id=context.actor_id,
            connection_id=route.connection_id,
            channel=route.channel,
            frozen_route=route.model_dump(mode="json"),
            kind="ad",
            ad_type="SMART_PLUS",
            partition_key="test",
            query={},
        )
        from app.integrations.tiktok.contracts.ads import (
            AdEntity,
            AdMaterialUsage,
            MaterialUseRef,
        )
        from app.modules.ads.sync import (
            _entity_json,
            _material_json,
            _next_directory_version,
        )

        version = _next_directory_version(session)
        incoming = AdEntity(
            obj.ref,
            obj.parent_ref,
            "SMART_PLUS",
            "平台更新的名称",
            {},
            "DISABLE",
            "APPROVED",
            "NOT_DELIVERING",
            datetime.now(UTC) + timedelta(seconds=1),
        )
        _publish_entities(
            session,
            [_entity_json(incoming)],
            run=run,
            version=version,
            fallback=incoming.observed_at,
        )
        usage = AdMaterialUsage(
            MaterialUseRef(obj.ref, "target-0", "actual-material-id", "VIDEO"),
            None,
            "DISABLE",
            True,
            "平台素材名",
            ("actual-creative-id",),
        )
        _publish_materials(session, [_material_json(usage)], run=run, version=version)
        publish_created_object(session, step=step, route=route)
        session.expire_all()
        obj = session.exec(select(AdObject)).one()
        assert obj.name == "平台更新的名称" and obj.operation_status == "DISABLE"
        assert obj.published_version == version
        uses = current_ad_materials(session, obj.ref)
        assert len(uses) == 1 and uses[0].ad_material_id == "actual-material-id"
        assert "creative_ids" not in AdMaterialReference.model_fields


@pytest.mark.parametrize(
    "invalid", ["unknown", "mismatch", "tenant", "connection", "account"]
)
def test_unconfirmed_or_wrong_scope_does_not_enter_directory(created_step, invalid):
    engine, _, identity, route = created_step
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        if invalid == "unknown":
            step.status = "UNKNOWN"
        elif invalid == "mismatch":
            step.mismatch = True
        elif invalid == "account":
            session.expunge(step)
            step.request_body = {
                **step.request_body,
                "advertiser_id": "another-account",
            }
        else:
            route = route.model_copy(update={f"{invalid}_id": uuid4()})
        publish_created_object(session, step=step, route=route)
        assert not session.exec(select(AdObject)).all()
        assert not session.exec(select(AdMaterialReference)).all()


def test_concurrent_receipts_insert_one_object_and_one_usage(created_step):
    engine, _, identity, route = created_step

    def insert_receipt():
        with Session(engine) as session, session.begin():
            publish_created_object(
                session, step=session.get(ExecutionStep, identity), route=route
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: insert_receipt(), range(2)))
    with Session(engine) as session:
        assert len(session.exec(select(AdObject)).all()) == 1
        assert len(session.exec(select(AdMaterialReference)).all()) == 1


def test_receipt_transaction_rollback_removes_directory_and_materials(created_step):
    engine, _, identity, route = created_step
    with Session(engine) as session:
        publish_created_object(
            session, step=session.get(ExecutionStep, identity), route=route
        )
        assert len(session.exec(select(AdObject)).all()) == 1
        session.rollback()
    with Session(engine) as session:
        assert not session.exec(select(AdObject)).all()
        assert not session.exec(select(AdMaterialReference)).all()


def test_carousel_records_images_and_music_without_invented_creative_id(created_step):
    engine, _, identity, route = created_step
    with Session(engine) as session, session.begin():
        step = session.get(ExecutionStep, identity)
        session.expunge(step)
        step.request_body = {
            **step.request_body,
            "creative_list": [
                {
                    "creative_info": {
                        "ad_format": "CAROUSEL_ADS",
                        "image_info": [{"web_uri": "image-1"}, {"web_uri": "image-2"}],
                        "music_info": {"music_id": "music-1"},
                    }
                }
            ],
        }
        publish_created_object(session, step=step, route=route)
        obj = session.exec(select(AdObject)).one()
        uses = current_ad_materials(session, obj.ref)
        assert {u.platform_material_id for u in uses} == {"image-1", "image-2"}
        assert all(
            u.material_type == "IMAGE" and u.ad_material_id is None for u in uses
        )
        from app.modules.ads.directory import directory_carousels

        carousel = directory_carousels(obj)
        assert carousel.identities[0].ordered_image_ids == ("image-1", "image-2")
        assert carousel.identities[0].music_id == "music-1"


@pytest.mark.parametrize("created_step", ["native"], indirect=True)
def test_created_native_creative_uses_current_identity_table_and_account_lock(
    created_step,
):
    from sqlalchemy import event

    from app.modules.ads.creative_models import AdCreative

    engine, _, identity, route = created_step
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with Session(engine) as session, session.begin():
            step = session.get(ExecutionStep, identity)
            publish_created_object(session, step=step, route=route)
            creative = session.get(
                AdCreative, (step.tenant_id, "account-A", "created-native")
            )
            assert creative.platform_material_id == "target-0"
            assert creative.ad_material_id == "created-mid"
            lock = next(
                i
                for i, sql in enumerate(statements)
                if "pg_advisory_xact_lock(hashtextextended" in sql
            )
            write = next(
                i
                for i, sql in enumerate(statements)
                if sql.startswith("INSERT INTO ad_object")
            )
            assert lock < write
            assert "creative_ids" not in AdMaterialReference.__table__.columns
    finally:
        event.remove(engine, "before_cursor_execute", observe)
