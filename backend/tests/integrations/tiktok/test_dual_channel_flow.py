"""跨阶段真实服务链；所有平台授权和远端响应均为 SYNTHETIC 离线证据。

从生产场景任务和原件入库开始，不播种可用素材或已冻结的广告预览。
使用同一事实链完成目标分发、缺失目标ID获取、封面准备、预览和回执直接创建。
"""

import json
from hashlib import md5
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from app.core.config import settings
from app.modules.materials.ingest_models import IngestSession, OriginalUse
from app.modules.materials.models import AccountMaterial, MaterialAssetOperation
from app.modules.materials.source_uploads import run_source_upload
from tests.integrations.tiktok.flow_inputs import _prepare_preview, _store
from tests.integrations.tiktok.flow_inputs import flow_storage as flow_storage
from tests.integrations.tiktok.gateway_support import gateway_case as gateway_case
from tests.integrations.tiktok.gateway_support import gateway_wire as gateway_wire
from tests.modules.accounts.conftest import app_config as app_config
from tests.modules.accounts.conftest import policy as policy
from tests.modules.builds.scene.support import enqueue, ensure, run, scene_responses
from tests.modules.builds.scene.support import scene_case as scene_case
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database as isolated_strategy_database,
)


@pytest.fixture
def database_engine(isolated_strategy_database):
    return isolated_strategy_database[0]


@pytest.fixture
def retain_build_history(isolated_strategy_database):
    return isolated_strategy_database[0]


def _reply(wire, tool, data):
    wire["sdk_data"]["data"] = data
    wire["wire"].results[tool].clear()
    wire["wire"].results[tool].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "request_id": "synthetic-flow",
                "data": data,
            },
        }
    )


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("cross_bc", [False, True], ids=["same-bc", "cross-bc"])
def test_scene_to_enabled_ads_uses_creation_receipts_on_one_channel(
    cross_bc,
    database_engine,
    redis_client,
    scene_case,
    gateway_wire,
    flow_storage,
    monkeypatch,
):
    # 若场景绕过准入、上传提前释放原件、或源 ID/摘要由请求补出，本用例应失败。
    case, remote = scene_case, flow_storage
    context, route, advertiser = case["context"], case["route"], case["advertiser_id"]
    preparation = ensure(database_engine, case)
    for resource, data in scene_responses(case).items():
        enqueue(gateway_wire, resource, data)
        job = run(database_engine, redis_client, case, preparation.job_id)
        # 每次只投递当前资源的合成回执；若未推进就在该阶段报告，避免下一资源
        # 的回执覆盖尚未消费的数据后才以最终 PENDING 掩盖原始失败。
        assert job.error_code is None and resource in job.facts, (
            route.channel,
            resource,
            job.status,
            job.resource,
            job.next_page,
            job.error_code,
            job.failure_count,
        )
    assert job.status == "COMPLETE", job.error_code
    sdk_start = len(gateway_wire["sdk_calls"])
    mcp_start = len(gateway_wire["wire"].calls)
    source_route, source_advertiser = route, advertiser
    if cross_bc:
        source_route, source_advertiser = _add_upload_bc(
            database_engine, context, route, advertiser
        )
    parent_id, material_id, object_id, generation = _store(
        database_engine, context, source_route, remote
    )
    created = {
        "video_id": "synthetic-source-vid",
        "material_id": "synthetic-source-mid",
        "advertiser_id": source_advertiser,
    }
    _reply(
        gateway_wire,
        "file_video_ad_upload",
        [created] if route.channel == "OFFICIAL_API" else created,
    )
    args = {
        "database_engine": database_engine,
        "redis_client": redis_client,
        "context": context,
        "material_id": material_id,
        "object_id": object_id,
        "generation": generation,
        "s3": remote,
    }
    run_source_upload(**args, kind="upload")
    with Session(database_engine) as db:
        op = db.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.material_id == material_id
            )
        ).one()
        # 正常上传直接由成功回执入库；无需额外查询，也不会保留原件用途。
        assert op.status == "succeeded", op.remote_response
        assert op.frozen_route == source_route.model_dump(mode="json")
        op_id = op.id
        mapping = db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material_id)
        ).one()
        assert (mapping.advertiser_id, mapping.video_id, mapping.mid) == (
            source_advertiser,
            "synthetic-source-vid",
            "synthetic-source-mid",
        )
        assert mapping.image_id is None
        assert db.get(IngestSession, parent_id).ready_count == 1
        assert (
            db.exec(select(OriginalUse).where(OriginalUse.operation_id == op_id))
            .one()
            .released_at
            is not None
        )
    # 已完成原始投递再次到达只能读取本地结果，不能偷偷再上传一次。
    run_source_upload(**args, kind="upload")
    if route.channel == "OFFICIAL_MCP":
        assert settings.TIKTOK_APP_ID == settings.TIKTOK_APP_SECRET == ""
        calls = [
            call["params"]
            for call in gateway_wire["wire"].calls[mcp_start:]
            if call["method"] == "tools/call"
        ]
        assert [call["name"] for call in calls] == [
            "file_video_ad_upload",
        ]
        assert all(
            call["arguments"]["advertiser_id"] == source_advertiser for call in calls
        )
    else:
        calls = gateway_wire["sdk_calls"][sdk_start:]
        assert [call[0] for call in calls] == ["POST"]
        assert calls[0][1].endswith("/file/video/ad/upload/")
    _prepare_target(
        database_engine,
        redis_client,
        context,
        route,
        material_id,
        source_advertiser,
        remote,
        gateway_wire,
        monkeypatch,
        source_route=source_route,
    )
    preview_id = _prepare_preview(
        database_engine, redis_client, case, gateway_wire, monkeypatch
    )
    ids = _submit_waiting_for_cover(
        database_engine, redis_client, case, preview_id, gateway_wire, monkeypatch
    )
    _finish_cover(
        database_engine, redis_client, case, gateway_wire, remote, ids, monkeypatch
    )
    _create_from_receipts(database_engine, redis_client, case, gateway_wire, ids)
    from app.modules.materials.ingest_models import IngestSessionFile
    from app.modules.materials.models import MaterialFile

    with Session(database_engine) as db:
        assert db.get(MaterialFile, material_id).bc_id == source_route.bc_id
        parent = db.get(IngestSession, parent_id)
        assert parent.bc_id == source_route.bc_id
        assert parent.frozen_route == source_route.model_dump(mode="json")
        uploaded = db.exec(
            select(IngestSessionFile).where(
                IngestSessionFile.material_id == material_id
            )
        ).one()
        assert (uploaded.bc_id, uploaded.source_advertiser_id) == (
            source_route.bc_id,
            source_advertiser,
        )


def _add_upload_bc(database_engine, context, target_route, primary_advertiser):
    """上传入口绑定独立 A；共享连接的 B 仍固定原有主账户。"""
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
    )
    from app.modules.accounts.models import TenantBC
    from app.modules.accounts.routing import freeze_route
    from tests.modules.materials.test_readiness import target

    source_bc, source_advertiser = "1234567890123456790", "90071992547409933"
    with Session(database_engine) as db, db.begin():
        db.add(TenantBC(tenant_id=context.tenant_id, bc_id=source_bc))
        db.flush()
        db.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=source_bc,
                connection_id=target_route.connection_id,
                kind=target_route.channel,
            )
        )
        db.flush()
        db.add(
            BCDefaultRoute(
                tenant_id=context.tenant_id,
                bc_id=source_bc,
                connection_id=target_route.connection_id,
            )
        )
        target(
            db,
            {
                "context": context,
                "bc_id": source_bc,
                "connection_id": target_route.connection_id,
            },
            advertiser_id=source_advertiser,
        )
        db.get(
            TenantBC, (context.tenant_id, target_route.bc_id)
        ).material_advertiser_id = primary_advertiser
        source_route = freeze_route(db, context=context, bc_id=source_bc)
    return source_route, source_advertiser


def _submit_waiting_for_cover(
    database_engine, redis_client, case, preview_id, wire, monkeypatch
):
    """允许可准备组合提交；即使 CTA 完成，缺封面也不能创建 Campaign。"""
    from app.modules.builds import execution
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.submissions import expand_submission, submit_preview
    from tests.modules.builds.test_channel_execution import created

    context = case["context"]
    monkeypatch.setattr(execution, "_require_bounded_worker", lambda: None)
    with Session(database_engine) as db, db.begin():
        receipt = submit_preview(
            db, context=context, preview_id=preview_id, request_id=uuid4()
        )
    for _ in range(5):
        with Session(database_engine) as db, db.begin():
            done = expand_submission(
                db, context=context, submission_id=receipt.submission_id
            )
        if done:
            break
    assert done
    with Session(database_engine) as db:
        steps = db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == receipt.submission_id
            )
        ).all()
        ids = {step.kind: step.id for step in steps}
    created(wire, "CTA")
    assert (
        execution.process_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            step_id=ids["CTA"],
            revision=0,
        )
        == "SUCCEEDED"
    )
    sdk_start, mcp_start = len(wire["sdk_calls"]), len(wire["wire"].calls)
    execution.process_step(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        step_id=ids["MATERIAL"],
        revision=0,
    )
    execution.process_step(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        step_id=ids["CAMPAIGN"],
        revision=0,
    )
    with Session(database_engine) as db:
        material = db.get(ExecutionStep, ids["MATERIAL"])
        assert (material.status, material.error_code) == ("PENDING", "cover_pending")
        assert db.get(ExecutionStep, ids["CAMPAIGN"]).request_body is None
        assert all(
            db.get(ExecutionStep, ids[kind]).remote_id is None
            for kind in ("CAMPAIGN", "ADGROUP", "AD")
        )
    assert wire["sdk_calls"][sdk_start:] == []
    assert not [
        call
        for call in wire["wire"].calls[mcp_start:]
        if call["method"] == "tools/call"
    ]
    return ids


def _finish_cover(database_engine, redis_client, case, wire, remote, ids, monkeypatch):
    """实际 SOURCE 图片上传→IMAGE 共享→目标图片核实，禁止目标直接重传。"""
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.execution import process_step
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import run_cover

    context = case["context"]
    with Session(database_engine) as db:
        target_job = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                MaterialCoverJob.advertiser_id == "90071992547409932",
            )
        ).one()
        job_id = target_job.id

    def advance(identity, read=False):
        with Session(database_engine) as db:
            job = db.get(MaterialCoverJob, identity)
            dispatch_id, revision = job.dispatch_id, job.revision
        run_cover(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            job_id=identity,
            dispatch_id=dispatch_id,
            revision=revision,
            read=read,
        )
        with Session(database_engine) as db:
            return db.get(MaterialCoverJob, identity).model_copy()

    # 仅处理真实等待依赖，来源 job 必须由成功视频上传账本推导。
    advance(job_id)
    with Session(database_engine) as db:
        source = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == context.tenant_id,
                MaterialCoverJob.bc_id == case["route"].bc_id,
                MaterialCoverJob.purpose == "SOURCE",
            )
        ).one()
        source_id, source_vid, source_name = (
            source.id,
            source.video_id,
            source.remote_name,
        )
    image = {
        "image_id": "synthetic-source-image",
        "material_id": "900000",
        "signature": "c" * 32,
        "width": 160,
        "height": 240,
        "displayable": True,
        "file_name": source_name,
    }
    video = {
        "list": [
            {
                "video_id": source_vid,
                "signature": md5(remote.content).hexdigest(),
                "displayable": True,
                "width": 160,
                "height": 240,
                "video_cover_url": "https://cdn.example/target-cover",
            }
        ]
    }
    _reply(wire, "file_video_ad_info_get", video)
    _reply(wire, "file_image_ad_upload", image)
    offset = len(wire["sdk_calls"])
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data=video if len(wire["sdk_calls"]) == offset else image
    )
    try:
        advance(source_id)
    finally:
        wire["before"]["callback"] = None
    with Session(database_engine) as db:
        saved_source = db.get(MaterialCoverJob, source_id)
        assert saved_source.status == "READY"
        assert saved_source.image_mid == image["material_id"]
    # 来源等待已经落到持久依赖，不能用空dispatch_id直接执行目标。
    # 推进测试时钟到修复期限，走正式Beat修复函数生成原目标的下一投递。
    from app.modules.materials import covers

    with Session(database_engine) as db, db.begin():
        waiting = db.get(MaterialCoverJob, job_id)
        assert waiting.dispatch_id is None
        assert waiting.error_code == "cover_source_pending"
        due = waiting.repair_after + timedelta(seconds=1)
        with monkeypatch.context() as patch:
            patch.setattr(covers, "_now", lambda: due)
            assert covers.repair_cover_dispatches(db) >= 1
        assert waiting.dispatch_id is not None

    def page(rows):
        return {
            "list": rows,
            "page_info": {
                "page": 1,
                "page_size": 100,
                "total_page": 1,
                "total_number": len(rows),
            },
        }

    _reply(wire, "file_image_ad_search", page([]))
    _reply(wire, "creative_asset_share_get", {"failed_infos": {}})
    offset = len(wire["sdk_calls"])
    # 源上传已保存 MID，共享不重复读取源图片。
    replies = [page([]), {"failed_infos": {}}]
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data=replies[min(len(wire["sdk_calls"]) - offset, len(replies) - 1)]
    )
    try:
        for _ in range(5):
            job = advance(job_id)
            if job.status not in {"PENDING", "PREPARING"}:
                break
        assert job.status == "VERIFYING", job.error_code
    finally:
        wire["before"]["callback"] = None
    _reply(
        wire,
        "file_image_ad_search",
        page([{**image, "image_id": "synthetic-target-image"}]),
    )
    for _ in range(5):
        job = advance(job_id, read=True)
        if job.status != "VERIFYING":
            break
    assert job.status == "READY", job.error_code
    with Session(database_engine) as db, db.begin():
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == case["route"].bc_id,
                AccountMaterial.advertiser_id == "90071992547409932",
            )
        ).one()
        assert mapping.image_id == "synthetic-target-image"
        step = db.get(ExecutionStep, ids["MATERIAL"])
        step.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = step.dispatch_revision
    assert (
        process_step(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            step_id=ids["MATERIAL"],
            revision=revision,
        )
        == "SUCCEEDED"
    )


def _create_from_receipts(database_engine, redis_client, case, wire, ids):
    import json
    from decimal import Decimal

    from app.modules.builds.execution import process_step
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.route_models import BuildAttemptContext
    from app.modules.builds.routes import load_preview_route
    from tests.integrations.tiktok.build_wire import BuildWire
    from tests.modules.builds.test_channel_execution import created

    context, route = case["context"], case["route"]
    sdk_start, mcp_start = len(wire["sdk_calls"]), len(wire["wire"].calls)
    observed = {}
    for kind in ("CAMPAIGN", "ADGROUP", "AD"):
        created(wire, kind)
        assert (
            process_step(
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                step_id=ids[kind],
                revision=0,
            )
            == "SUCCEEDED"
        )
        # 合成上游保存真正进入HTTP边界的正文，后续GET读取该独立远端副本。
        # 不从数据库request_body或业务compiler生成读取事实。
        if route.channel == "OFFICIAL_API":
            observed[kind] = json.loads(
                wire["sdk_calls"][-1][2]["body"], parse_float=str
            )
        else:
            sent = [
                call for call in wire["wire"].calls if call["method"] == "tools/call"
            ][-1]
            observed[kind] = json.loads(
                json.dumps(sent["params"]["arguments"]), parse_float=str
            )
        observed[kind][BuildWire.id_keys[kind]] = "synthetic-" + kind.lower()
    assert Decimal(str(observed["CAMPAIGN"]["budget"])) == Decimal("100.25")
    assert Decimal(str(observed["ADGROUP"]["roas_bid"])) == Decimal("1.08")
    assert observed["ADGROUP"]["campaign_id"] == "synthetic-campaign"
    assert observed["AD"]["adgroup_id"] == "synthetic-adgroup"
    assert (
        observed["AD"]["ad_configuration"]["call_to_action_id"] == "synthetic-portfolio"
    )
    creative = observed["AD"]["creative_list"][0]["creative_info"]
    assert creative["video_info"]["video_id"] == "synthetic-target-vid"
    assert creative["image_info"] == [{"web_uri": "synthetic-target-image"}]
    assert all(row["operation_status"] == "ENABLE" for row in observed.values())
    with Session(database_engine) as db:
        original = [
            db.get(ExecutionStep, ids[kind])
            for kind in ("CTA", "CAMPAIGN", "ADGROUP", "AD")
        ]
        assert len({step.attempt_id for step in original}) == 4
        assert (
            load_preview_route(db, context=context, preview_id=original[0].preview_id)
            == route
        )
        for step in original:
            attempt = db.get(
                BuildAttemptContext, (context.tenant_id, step.id, step.attempt)
            )
            assert attempt.attempt_id == step.attempt_id
        assert all(
            step.status == "SUCCEEDED" and step.checked_at is None for step in original
        )
        assert not db.exec(
            select(ExecutionStep).where(
                ExecutionStep.submission_id == original[0].submission_id,
                ExecutionStep.kind == "READBACK",
            )
        ).all()
    assert all(call[0] == "POST" for call in wire["sdk_calls"][sdk_start:])
    mcp_creates = [
        call["params"]["name"]
        for call in wire["wire"].calls[mcp_start:]
        if call["method"] == "tools/call"
    ]
    assert all(name.endswith("_create") for name in mcp_creates)
    assert len(mcp_creates) == (3 if route.channel == "OFFICIAL_MCP" else 0)
    assert len(wire["sdk_calls"][sdk_start:]) == (
        3 if route.channel == "OFFICIAL_API" else 0
    )

    from app.modules.builds.submissions import get_submission

    with Session(database_engine) as db:
        result = get_submission(
            db, context=context, submission_id=original[0].submission_id
        )
        assert result.status == "COMPLETED"
        assert result.execution_route.connection_id == route.connection_id
        assert result.execution_route.channel == route.channel


def _finish_bc_seed(
    database_engine,
    redis_client,
    context,
    route,
    source_route,
    material_id,
    target_task_id,
    details,
    wire,
):
    """消费真实 BC seed；一次 URL 上传仅送到 B 主账户，未冒充目标 ready。"""
    from app.modules.materials.distribution import run_distribution
    from app.modules.materials.models import MaterialDistribution
    from app.modules.materials.seed_models import MaterialBCSeed

    with Session(database_engine) as db:
        waiter = db.get(MaterialDistribution, target_task_id)
        seed = db.get(MaterialBCSeed, waiter.seed_id)
        seed_id, primary = seed.distribution_id, seed.advertiser_id
        assert seed_id != target_task_id
        assert primary == "90071992547409931"
        owner = db.get(MaterialDistribution, seed_id)
        assert owner.source_route == source_route.model_dump(mode="json")
        assert owner.target_route == route.model_dump(mode="json")
        assert owner.material_id == material_id
    arguments = {
        "database_engine": database_engine,
        "redis_client": redis_client,
        "context": context,
    }
    sdk_start, mcp_start = len(wire["sdk_calls"]), len(wire["wire"].calls)
    # 未完成的 seed 只增加等待代数，不读取 A 或向目标上传。
    run_distribution(**arguments, distribution_id=target_task_id, kind="prepare")
    assert len(wire["sdk_calls"]) == sdk_start
    assert len(wire["wire"].calls) == mcp_start
    receipt = {
        "advertiser_id": primary,
        "video_id": "synthetic-primary-vid",
        "material_id": "synthetic-primary-mid",
    }
    upload_reply = [receipt] if route.channel == "OFFICIAL_API" else receipt
    _reply(wire, "file_video_ad_info_get", details)
    _reply(wire, "file_video_ad_upload", upload_reply)
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data=details if len(wire["sdk_calls"]) == sdk_start else upload_reply
    )
    try:
        run_distribution(**arguments, distribution_id=seed_id, kind="prepare")
    finally:
        wire["before"]["callback"] = None
    with Session(database_engine) as db:
        owner = db.get(MaterialDistribution, seed_id)
        operation = db.get(MaterialAssetOperation, owner.operation_id)
        assert owner.status == "ready", (
            owner.reason_code,
            operation.remote_response,
        )
        assert operation.remote_response["transport"] == "url_relay"
    primary_details = {
        "list": [
            {**details["list"][0], **receipt, "file_name": "bc-primary-source.mp4"}
        ]
    }
    _reply(wire, "file_video_ad_info_get", primary_details)
    run_distribution(**arguments, distribution_id=seed_id, kind="verify")
    # 重复 prepare 使用既有确定结果；不能再次发 URL 上传。
    run_distribution(**arguments, distribution_id=seed_id, kind="prepare")
    with Session(database_engine) as db:
        assert db.get(MaterialDistribution, seed_id).status == "ready"
        assert db.get(MaterialDistribution, target_task_id).status != "ready"
    if route.channel == "OFFICIAL_API":
        calls = wire["sdk_calls"][sdk_start:]
        assert [call[0] for call in calls] == ["GET", "POST"]
        uploads = [
            dict(call[2]["fields"])
            for call in calls
            if call[1].endswith("/file/video/ad/upload/")
        ]
    else:
        calls = [
            call["params"]
            for call in wire["wire"].calls[mcp_start:]
            if call["method"] == "tools/call"
        ]
        assert [call["name"] for call in calls] == [
            "file_video_ad_info_get",
            "file_video_ad_upload",
        ]
        uploads = [
            call["arguments"]
            for call in calls
            if call["name"] == "file_video_ad_upload"
        ]
    assert len(uploads) == 1
    assert uploads[0]["advertiser_id"] == primary
    assert uploads[0]["upload_type"] == "UPLOAD_BY_URL"
    assert uploads[0]["video_url"] == details["list"][0]["preview_url"]
    return primary_details


def _prepare_target(
    database_engine,
    redis_client,
    context,
    route,
    material_id,
    source_advertiser,
    remote,
    wire,
    monkeypatch,
    *,
    source_route,
):
    """沿同一路由真实分发到另一账户；来源、目标 VID 不得互相覆盖。"""
    from app.modules.materials.distribution import ensure_target_asset, run_distribution
    from app.modules.materials.models import MaterialDistribution
    from tests.modules.materials.test_readiness import target

    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"flow.vetted.example"})
    )
    target_advertiser = "90071992547409932"
    mcp_start = len(wire["wire"].calls)
    with Session(database_engine) as db, db.begin():
        target(
            db,
            {
                "context": context,
                "bc_id": route.bc_id,
                "connection_id": route.connection_id,
            },
            advertiser_id=target_advertiser,
        )
        prepared = ensure_target_asset(
            db,
            context=context,
            bc_id=route.bc_id,
            material_id=material_id,
            advertiser_id=target_advertiser,
            task_key=f"flow:{uuid4()}",
            route=route,
        )
        assert prepared.state == "queued", prepared.reason_code
    details = {
        "list": [
            {
                "advertiser_id": source_advertiser,
                "video_id": "synthetic-source-vid",
                "material_id": "synthetic-source-mid",
                "file_name": "source-file-01.mp4",
                "signature": md5(remote.content).hexdigest(),
                "size": len(remote.content),
                "displayable": True,
                "width": 160,
                "height": 240,
                "duration": 0.2,
                "format": "mp4",
                "preview_url": "https://flow.vetted.example/video?signature=synthetic-only",
            }
        ]
    }
    source_expected = (
        source_advertiser,
        "synthetic-source-vid",
        "synthetic-source-mid",
    )
    seed_expected = set()
    if source_route.bc_id != route.bc_id:
        details = _finish_bc_seed(
            database_engine,
            redis_client,
            context,
            route,
            source_route,
            material_id,
            prepared.task_id,
            details,
            wire,
        )
        source_advertiser = details["list"][0]["advertiser_id"]
        seed_expected.add(
            (source_advertiser, "synthetic-primary-vid", "synthetic-primary-mid")
        )
        mcp_start = len(wire["wire"].calls)
    created = {
        "advertiser_id": target_advertiser,
        "video_id": "synthetic-target-vid",
        "material_id": "synthetic-target-mid",
    }
    _reply(wire, "file_video_ad_info_get", details)
    _reply(wire, "creative_asset_share_get", {})
    # SDK 同一 worker 内读取源 MID/名称，再原生共享，不重新上传。
    offset = len(wire["sdk_calls"])
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data=details if len(wire["sdk_calls"]) == offset else {}
    )
    try:
        run_distribution(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=prepared.task_id,
            kind="prepare",
        )
    finally:
        wire["before"]["callback"] = None
    with Session(database_engine) as db:
        dist = db.get(MaterialDistribution, prepared.task_id)
        op = db.get(MaterialAssetOperation, dist.operation_id)
        assert dist.status == "verifying", (dist.reason_code, op.remote_response)
        assert dist.source_route == dist.target_route == route.model_dump(mode="json")
        assert op.remote_response["transport"] == "native_share"
        assert op.remote_response["share_acknowledged"] is True
        assert "signature=synthetic-only" not in repr(op.remote_response)
    # 已ACK共享先尝试源VID在目标账户的实际详情；这里目标VID不同，
    # 明确MISS后再按素材内容发现，不把源VID当作目标ID。
    _reply(wire, "file_video_ad_info_get", {"list": []})
    target_details = {
        "list": [{**details["list"][0], **created}],
        "page_info": {
            "page": 1,
            "page_size": 100,
            "total_page": 1,
            "total_number": 1,
        },
    }
    _reply(
        wire,
        "file_video_ad_search",
        target_details,
    )
    verify_offset = len(wire["sdk_calls"])
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data={"list": []} if len(wire["sdk_calls"]) == verify_offset else target_details
    )
    try:
        run_distribution(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            distribution_id=prepared.task_id,
            kind="verify",
        )
    finally:
        wire["before"]["callback"] = None
    with Session(database_engine) as db:
        dist = db.get(MaterialDistribution, prepared.task_id)
        assert dist.status == "ready", dist.reason_code
        mappings = db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material_id)
        ).all()
        assert {(row.advertiser_id, row.video_id, row.mid) for row in mappings} == {
            source_expected,
            (target_advertiser, "synthetic-target-vid", "synthetic-target-mid"),
        } | seed_expected
        assert {(row.bc_id, row.advertiser_id) for row in mappings} == {
            (source_route.bc_id, source_expected[0]),
            (route.bc_id, target_advertiser),
        } | ({(route.bc_id, source_advertiser)} if seed_expected else set())
        assert all(row.image_id is None for row in mappings)
    if route.channel == "OFFICIAL_MCP":
        calls = [
            call["params"]
            for call in wire["wire"].calls[mcp_start:]
            if call["method"] == "tools/call"
        ]
        assert [call["name"] for call in calls] == [
            "file_video_ad_info_get",
            "creative_asset_share_get",
            "file_video_ad_info_get",
            "file_video_ad_search",
        ]
        assert [call["arguments"]["advertiser_id"] for call in calls] == [
            source_advertiser,
            source_advertiser,
            target_advertiser,
            target_advertiser,
        ]
    else:
        calls = wire["sdk_calls"][offset:]
        assert [call[0] for call in calls] == ["GET", "POST", "GET", "GET"]
        assert calls[1][1].endswith("/creative/asset/share/")
        assert json.loads(calls[1][2]["body"])["shared_advertiser_ids"] == [
            target_advertiser
        ]
        assert all("/upload/" not in call[1] for call in calls)
