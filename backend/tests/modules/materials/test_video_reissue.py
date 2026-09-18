"""显式接受重复风险仅接替指定视频操作，旧未知证据不可改写。"""

from uuid import UUID, uuid4

import pytest
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep
from app.modules.materials.models import MaterialAssetOperation, MaterialDistribution
from tests.modules.builds.test_definite_material_recovery import (
    executable as executable,
)
from tests.modules.builds.test_definite_material_recovery import (
    rejected_material as rejected_material,
)
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def unknown_video(executable, rejected_material):
    from app.modules.tenants.models import TenantMembership

    database, context, _ = executable
    step_id, submission_id, distribution_id, _, _ = rejected_material
    with Session(database) as db, db.begin():
        db.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "tenant_admin"
        dist = db.get(MaterialDistribution, distribution_id)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        dist.status = op.status = "result_unknown"
        op.remote_response = {
            **{
                k: v for k, v in op.remote_response.items() if k != "definite_no_effect"
            },
            "send_armed": True,
            "reconciliation_stopped": True,
        }
        step = db.get(ExecutionStep, step_id)
        step.status, step.error_code = "UNKNOWN", "material_result_unknown"
        before = dict(op.remote_response)
    return step_id, submission_id, distribution_id, before


def replace(db, executable, unknown_video):
    from app.modules.materials.reissue import (
        MaterialReissueInput,
        authorize_material_reissue,
    )
    from app.modules.materials.reissue_models import MaterialReissueAuthorization

    _, context, _ = executable
    receipt = authorize_material_reissue(
        db,
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        submission_id=unknown_video[1],
        body=MaterialReissueInput(
            request_id=uuid4(),
            kind="VIDEO",
            old_id=unknown_video[2],
            accepted_duplicate_materials=True,
        ),
    )
    return db.get(MaterialReissueAuthorization, receipt.authorization_id).details


def test_authorized_unknown_replacement_preserves_evidence_and_original_ad(
    executable, unknown_video
):
    database, _, ids = executable
    with Session(database) as db, db.begin():
        ad = db.get(ExecutionStep, ids["AD"][0])
        ad.status, ad.phase = "UNKNOWN", "DONE"
        ad_before = ad.model_dump()
        result = replace(db, executable, unknown_video)
    with Session(database) as db:
        old = db.get(MaterialDistribution, unknown_video[2])
        old_op = db.get(MaterialAssetOperation, old.operation_id)
        new = db.get(MaterialDistribution, UUID(result["new_distribution_id"]))
        op = db.get(MaterialAssetOperation, new.operation_id)
        assert old.status == old_op.status == "result_unknown"
        assert old_op.remote_response == unknown_video[3]
        assert old.superseded_by_id == new.id
        assert old_op.superseded_by_id == op.id
        assert (new.target_route, new.source_route, new.source_asset_id) == (
            old.target_route,
            old.source_route,
            old.source_asset_id,
        )
        assert op.status == "pending" and not op.remote_response.get("send_armed")
        assert "share_receipt" not in op.remote_response
        assert db.get(ExecutionStep, unknown_video[0]).distribution_id == new.id
        assert db.get(ExecutionStep, ids["AD"][0]).model_dump() == ad_before


def test_duplicate_replacement_reuses_exact_new_operation(executable, unknown_video):
    database, _, _ = executable
    with Session(database) as db, db.begin():
        first = replace(db, executable, unknown_video)
    with Session(database) as db, db.begin():
        second = replace(db, executable, unknown_video)
        assert second["new_operation_id"] == first["new_operation_id"]
        assert len(db.exec(select(MaterialDistribution)).all()) == 2


def test_known_video_refuses_duplicate_risk_replacement(executable, unknown_video):
    database, _, _ = executable
    with Session(database) as db, db.begin():
        dist = db.get(MaterialDistribution, unknown_video[2])
        op = db.get(MaterialAssetOperation, dist.operation_id)
        op.remote_response = {**op.remote_response, "upload_video_id": "known-vid"}
    with Session(database) as db, db.begin(), pytest.raises(DomainError):
        replace(db, executable, unknown_video)


@pytest.mark.parametrize("kind", ["prepare", "verify"])
def test_old_worker_permission_error_after_reissue_preserves_unknown(
    executable, unknown_video, redis_client, kind
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.modules.accounts.models import BCAccountAccess
    from app.modules.materials.distribution import run_distribution

    database, context, _ = executable
    paused, resume = Event(), Event()
    worker_id = []
    with Session(database) as db, db.begin():
        old = db.get(MaterialDistribution, unknown_video[2])
        op = db.get(MaterialAssetOperation, old.operation_id)
        op.remote_response = {
            key: value
            for key, value in op.remote_response.items()
            if key != "reconciliation_stopped"
        }
        before = dict(op.remote_response)

    def pause_before_authorization(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            worker_id
            and get_ident() == worker_id[0]
            and "FROM tenant_membership" in statement
            and not paused.is_set()
        ):
            paused.set()
            assert resume.wait(15)

    def run_old():
        worker_id.append(get_ident())
        run_distribution(
            database_engine=database,
            redis_client=redis_client,
            context=context,
            distribution_id=unknown_video[2],
            kind=kind,
        )

    event.listen(database, "before_cursor_execute", pause_before_authorization)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_old)
            try:
                assert paused.wait(15)
                with Session(database) as db, db.begin():
                    replace(db, executable, unknown_video)
                with Session(database) as db, db.begin():
                    grant = db.exec(
                        select(BCAccountAccess).where(
                            BCAccountAccess.tenant_id == context.tenant_id,
                            BCAccountAccess.advertiser_id == "account-A",
                        )
                    ).one()
                    grant.can_build = False
            finally:
                resume.set()
            future.result(timeout=15)
    finally:
        event.remove(database, "before_cursor_execute", pause_before_authorization)
    with Session(database) as db:
        old = db.get(MaterialDistribution, unknown_video[2])
        op = db.get(MaterialAssetOperation, old.operation_id)
        assert old.status == op.status == "result_unknown"
        assert op.remote_response == before


def test_old_delivery_and_publish_cannot_replace_new_mapping(executable, unknown_video):
    from app.modules.materials.distribution import _delivery_matches, _publish_mapping
    from app.modules.materials.models import AccountMaterial

    database, context, _ = executable
    with Session(database) as db, db.begin():
        result = replace(db, executable, unknown_video)
        new = db.get(MaterialDistribution, UUID(result["new_distribution_id"]))
        _publish_mapping(db, context, new, {"video_id": "replacement-vid"})
        old = db.get(MaterialDistribution, unknown_video[2])
        op = db.get(MaterialAssetOperation, old.operation_id)
        delivery = MaterialAssetOperation(**{**op.model_dump(), "remote_response": {}})
        assert not _delivery_matches(
            delivery, operation_id=op.id, revision=None, recovery_claim_id=None
        )
        _publish_mapping(db, context, old, {"video_id": "late-old-vid"})
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.material_id == new.material_id,
                AccountMaterial.advertiser_id == new.advertiser_id,
            )
        ).one()
        assert mapping.video_id == "replacement-vid"


def test_replacement_runs_native_share_and_continues_original_material_step(
    executable, unknown_video, redis_client, monkeypatch
):
    import json

    from urllib3.response import HTTPResponse

    from app.modules.builds.material_execution import recover_material_results
    from app.modules.materials.distribution import run_distribution
    from app.modules.materials.models import AccountMaterial, MaterialFile

    database, context, _ = executable
    with Session(database) as db, db.begin():
        result = replace(db, executable, unknown_video)
        new_id = UUID(result["new_distribution_id"])
        new = db.get(MaterialDistribution, new_id)
        material_id = new.material_id
        filename = db.get(MaterialFile, new.material_id).file_name
    calls = []

    def transport(_pool, method, url, **_kwargs):
        calls.append((method, url))
        assert "/upload/" not in url
        data = (
            {}
            if "/share/" in url
            else {
                "list": [
                    {
                        "video_id": "source-video"
                        if "/info/" in url
                        else "verified-target",
                        "material_id": "source-mid",
                        "file_name": filename,
                        "signature": "a" * 32,
                        "displayable": True,
                    }
                ],
                "page_info": {"page": 1, "page_size": 100, "total_page": 1},
            }
        )
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": data}).encode(), status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    for kind in ("prepare", "verify"):
        run_distribution(
            database_engine=database,
            redis_client=redis_client,
            context=context,
            distribution_id=new_id,
            kind=kind,
        )
    recover_material_results(database_engine=database)
    for kind in ("prepare", "verify"):
        run_distribution(
            database_engine=database,
            redis_client=redis_client,
            context=context,
            distribution_id=unknown_video[2],
            kind=kind,
        )
    with Session(database) as db:
        assert db.get(MaterialDistribution, new_id).status == "ready", db.get(
            MaterialDistribution, new_id
        ).reason_code
        step = db.get(ExecutionStep, unknown_video[0])
        assert step.status == "SUCCEEDED"
        assert step.resolved["mapping"]["video_id"] == "verified-target"
        assert (
            db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == material_id,
                    AccountMaterial.advertiser_id == "account-A",
                )
            )
            .one()
            .video_id
            == "verified-target"
        )
    assert [method for method, _ in calls] == ["GET", "POST", "GET", "GET"]


@pytest.mark.parametrize("executable", [2], indirect=True)
@pytest.mark.parametrize("reissue_consumer", [False, True])
def test_cross_bc_seed_replacement_rebinds_only_original_waiters_and_shares(
    executable, redis_client, monkeypatch, reissue_consumer
):
    import json
    from datetime import UTC, datetime
    from urllib.parse import parse_qs, urlsplit

    from urllib3.response import HTTPResponse

    from app.core.config import settings
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TenantBC
    from app.modules.builds.preview_models import BuildUnit
    from app.modules.builds.routes import load_preview_route
    from app.modules.materials.distribution import ensure_target_asset, run_distribution
    from app.modules.materials.models import AccountMaterial, MaterialFile
    from app.modules.materials.seed_models import MaterialBCSeed
    from app.modules.tenants.models import TenantMembership
    from tests.modules.builds.test_drafts import account
    from tests.modules.materials.test_readiness import target

    database, context, ids = executable
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.vetted.example"})
    )
    monkeypatch.setattr(
        settings,
        "TIKTOK_CALL_POLICIES",
        {
            "base": {**settings.TIKTOK_CALL_POLICIES["base"], "lease_ms": 970000},
        },
    )
    with Session(database) as db, db.begin():
        db.get(
            TenantMembership, (context.tenant_id, context.actor_id)
        ).role = "tenant_admin"
        anchor = db.get(ExecutionStep, ids["MATERIAL"][0])
        material = db.get(MaterialFile, anchor.material_id)
        material.storage_state, material.video_md5, material.byte_size = (
            "unavailable",
            "a" * 32,
            100,
        )
        filename, material_id, submission_id = (
            material.file_name,
            material.id,
            anchor.submission_id,
        )
        for mapping in db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material.id)
        ).all():
            db.delete(mapping)
        account(db, context, "seed-primary")
        db.get(
            TenantBC, (context.tenant_id, "bc-draft")
        ).material_advertiser_id = "seed-primary"
        route = load_preview_route(db, context=context, preview_id=anchor.preview_id)
        db.add(TenantBC(tenant_id=context.tenant_id, bc_id="source-foreign"))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id="source-foreign",
                connection_id=route.connection_id,
                kind="OFFICIAL_API",
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id,
                bc_id="source-foreign",
                connection_id=route.connection_id,
            )
        )
        target(
            db,
            {
                "context": context,
                "bc_id": "source-foreign",
                "connection_id": route.connection_id,
            },
            advertiser_id="foreign-source",
        )
        db.add(
            AccountMaterial(
                tenant_id=context.tenant_id,
                bc_id="source-foreign",
                material_id=material.id,
                advertiser_id="foreign-source",
                connection_id=route.connection_id,
                video_id="foreign-vid",
                mid="foreign-mid",
                status="available",
                verified_at=datetime.now(UTC),
            )
        )
        db.flush()
        steps = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == submission_id,
                ExecutionStep.kind == "MATERIAL",
                ExecutionStep.material_id == material.id,
            )
        ).all()
        assert len(steps) == 2
        consumer_ids = []
        for step in steps:
            unit = db.get(BuildUnit, step.unit_id)
            result = ensure_target_asset(
                db,
                context=context,
                bc_id="bc-draft",
                material_id=material.id,
                advertiser_id=unit.advertiser_id,
                task_key=f"build:{step.id}",
                route=route,
            )
            step.distribution_id = result.task_id
            step.status, step.phase, step.error_code, step.dispatch_id = (
                "UNKNOWN",
                "DONE",
                "material_result_unknown",
                None,
            )
            consumer_ids.append(result.task_id)
        seed = db.exec(select(MaterialBCSeed)).one()
        owner = db.get(MaterialDistribution, seed.distribution_id)
        op = db.get(MaterialAssetOperation, owner.operation_id)
        owner.status = op.status = "result_unknown"
        op.remote_response = {
            **op.remote_response,
            "send_armed": True,
            "reconciliation_stopped": True,
        }
        old_id, old_seed_id, old_response = owner.id, seed.id, dict(op.remote_response)
        # 无原提交引用的历史消费者必须仍指向旧代。
        account(db, context, "other-batch-target")
        outsider = ensure_target_asset(
            db,
            context=context,
            bc_id="bc-draft",
            material_id=material.id,
            advertiser_id="other-batch-target",
            task_key="other-batch",
            route=route,
        ).task_id
    with Session(database) as db, db.begin():
        result = replace(db, executable, (None, submission_id, old_id, old_response))
        new_id = UUID(result["new_distribution_id"])
        assert set(result["rebound_consumer_ids"]) == {
            str(value) for value in consumer_ids
        }
        assert db.get(MaterialDistribution, outsider).seed_id == old_seed_id
        from app.modules.materials.bc_seeding import seed_dependency_settled

        assert (
            db.exec(
                select(MaterialDistribution.id)
                .join(
                    MaterialAssetOperation,
                    MaterialAssetOperation.id == MaterialDistribution.operation_id,
                )
                .where(MaterialDistribution.id == outsider, seed_dependency_settled())
            ).first()
            is None
        )
    calls = []
    fail_share = [reissue_consumer]
    target_reads = []

    def transport(_pool, method, url, **kwargs):
        calls.append((method, url))
        query = parse_qs(urlsplit(url).query)
        advertiser = query.get("advertiser_id", [None])[0] or dict(
            kwargs.get("fields", [])
        ).get("advertiser_id")
        if "/upload/" in url:
            fields = dict(kwargs["fields"])
            assert fields["advertiser_id"] == "seed-primary"
            assert fields["upload_type"] == "UPLOAD_BY_URL"
            data = [{"video_id": "primary-vid", "material_id": "primary-mid"}]
        elif "/share/" in url:
            if fail_share[0]:
                from urllib3.exceptions import ReadTimeoutError

                fail_share[0] = False
                raise ReadTimeoutError(
                    None, url, "fixture: native share result unknown"
                )
            data = {}
        else:
            video = (
                "foreign-vid"
                if advertiser == "foreign-source"
                else "primary-vid"
                if advertiser == "seed-primary"
                else f"target-{advertiser}"
            )
            data = {
                "list": [
                    {
                        "video_id": video,
                        "material_id": "foreign-mid"
                        if advertiser == "foreign-source"
                        else "primary-mid",
                        "file_name": filename,
                        "signature": "a" * 32,
                        "size": 100,
                        "displayable": True,
                        "width": 1080,
                        "height": 1920,
                        "duration": 4.5,
                        "format": "mp4",
                        "preview_url": "https://media.vetted.example/video",
                    }
                ],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            }
            # 详情接口只返回请求的 VID；目标已有另一 VID 时先报候选未找到，
            # 再让真实列表发现原目标映射，不能用无关 VID 伪造详情命中。
            requested_videos = json.loads(
                query.get(
                    "video_ids", [dict(kwargs.get("fields", [])).get("video_ids", "[]")]
                )[0]
            )
            target_reads.append((advertiser, requested_videos, "/search/" in url))
            if requested_videos and video not in requested_videos:
                data["list"] = []
        return HTTPResponse(
            body=json.dumps({"code": 0, "data": data}).encode(), status=200
        )

    monkeypatch.setattr("urllib3.PoolManager.request", transport)
    run_distribution(
        database_engine=database,
        redis_client=redis_client,
        context=context,
        distribution_id=new_id,
        kind="prepare",
    )
    unknown_consumer = None
    if reissue_consumer:
        unknown_consumer = consumer_ids[0]
        run_distribution(
            database_engine=database,
            redis_client=redis_client,
            context=context,
            distribution_id=unknown_consumer,
            kind="prepare",
        )
        with Session(database) as db, db.begin():
            old_consumer = db.get(MaterialDistribution, unknown_consumer)
            old_consumer_op = db.get(MaterialAssetOperation, old_consumer.operation_id)
            assert old_consumer.status == old_consumer_op.status == "result_unknown"
            retained_seed_id = old_consumer.seed_id
            consumer_before = dict(old_consumer_op.remote_response)
            # 来源失效、内容身份改变和未绑定等待者都不能获得此独立共享补发。
            for invalid in (
                "owner_unready",
                "source_video_changed",
                "seed_content_changed",
                "unbound",
            ):
                with pytest.raises(DomainError), db.begin_nested():
                    retained = db.get(MaterialBCSeed, retained_seed_id)
                    if invalid == "owner_unready":
                        db.get(
                            MaterialDistribution, retained.distribution_id
                        ).status = "verifying"
                    elif invalid == "source_video_changed":
                        db.get(
                            AccountMaterial, old_consumer.source_asset_id
                        ).video_id = "changed-primary-video"
                    elif invalid == "seed_content_changed":
                        retained.content_key = "changed-content-key"
                    else:
                        old_consumer_op.remote_response = {}
                        old_consumer.source_asset_id = None
                    db.flush()
                    replace(
                        db,
                        executable,
                        (None, submission_id, unknown_consumer, consumer_before),
                    )
            consumer_result = replace(
                db, executable, (None, submission_id, unknown_consumer, consumer_before)
            )
            replacement_id = UUID(consumer_result["new_distribution_id"])
            replacement = db.get(MaterialDistribution, replacement_id)
            replacement_op = db.get(MaterialAssetOperation, replacement.operation_id)
            assert replacement.seed_id == retained_seed_id
            assert (
                consumer_result["old_seed_id"]
                == consumer_result["new_seed_id"]
                == str(retained_seed_id)
            )
            assert replacement_op.remote_response["transport"] == "native_share"
            assert replacement_op.remote_response["source_video_id"] == "primary-vid"
            assert db.exec(
                select(MaterialBCSeed.generation).order_by(MaterialBCSeed.generation)
            ).all() == [1, 2]
        consumer_ids[0] = replacement_id
    for consumer_id in consumer_ids:
        for kind in ("prepare", "verify"):
            run_distribution(
                database_engine=database,
                redis_client=redis_client,
                context=context,
                distribution_id=consumer_id,
                kind=kind,
            )
    # 旧 URL 网关的迟到成功回调仍可发生，不能写入已接替的旧操作业务回执。
    from app.modules.materials.distribution import _relay_receipt

    _relay_receipt(
        database,
        context=context,
        distribution_id=old_id,
        operation_id=UUID(result["old_operation_id"]),
        claim=uuid4(),
        evidence={"video_id": "late-old-url-video", "mid": "late-old-mid"},
    )
    with Session(database) as db:
        assert db.get(MaterialDistribution, new_id).status == "ready", (
            db.get(MaterialDistribution, new_id).reason_code,
            calls,
        )
        assert all(
            db.get(MaterialDistribution, value).status == "ready"
            for value in consumer_ids
        ), [
            (
                db.get(MaterialDistribution, value).status,
                db.get(MaterialDistribution, value).reason_code,
            )
            for value in consumer_ids
        ]
        if unknown_consumer:
            original_consumer = db.get(MaterialDistribution, unknown_consumer)
            assert original_consumer.status == "result_unknown"
            assert (
                db.get(
                    MaterialAssetOperation, original_consumer.operation_id
                ).remote_response
                == consumer_before
            )
        assert db.get(MaterialDistribution, outsider).seed_id == old_seed_id
        old = db.get(MaterialDistribution, old_id)
        assert old.status == "result_unknown"
        assert (
            db.get(MaterialAssetOperation, old.operation_id).remote_response
            == old_response
        )
        assert db.exec(
            select(MaterialBCSeed.generation).order_by(MaterialBCSeed.generation)
        ).all() == [1, 2]
        assert (
            db.exec(
                select(AccountMaterial).where(
                    AccountMaterial.material_id == material_id,
                    AccountMaterial.advertiser_id == "account-A",
                )
            )
            .one()
            .video_id
            == "target-account-A"
        )
    assert sum("/upload/" in url for _, url in calls) == 1
    assert ("account-A", ["primary-vid"], False) in target_reads
    assert ("account-A", [], True) in target_reads
