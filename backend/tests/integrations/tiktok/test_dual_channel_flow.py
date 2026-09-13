"""跨阶段真实服务链；所有平台授权和远端响应均为 SYNTHETIC 离线证据。

从生产场景任务和原件入库开始，不播种可用素材或已冻结的广告预览。
使用同一事实链完成目标分发、封面、预览、创建与独立回读。
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
def test_scene_to_enabled_ads_and_readback_uses_one_channel(
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
    assert job.status == "COMPLETE", job.error_code
    sdk_start = len(gateway_wire["sdk_calls"])
    mcp_start = len(gateway_wire["wire"].calls)
    parent_id, material_id, object_id, generation = _store(
        database_engine, context, route, remote
    )
    created = {
        "video_id": "synthetic-source-vid",
        "material_id": "synthetic-source-mid",
        "advertiser_id": advertiser,
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
        assert op.frozen_route == route.model_dump(mode="json")
        op_id = op.id
        mapping = db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material_id)
        ).one()
        assert (mapping.advertiser_id, mapping.video_id, mapping.mid) == (
            advertiser,
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
        assert all(call["arguments"]["advertiser_id"] == advertiser for call in calls)
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
        advertiser,
        remote,
        gateway_wire,
        monkeypatch,
    )
    preview_id = _prepare_preview(
        database_engine, redis_client, case, gateway_wire, monkeypatch
    )
    ids = _submit_waiting_for_cover(
        database_engine, redis_client, case, preview_id, gateway_wire, monkeypatch
    )
    _finish_cover(database_engine, redis_client, case, gateway_wire, remote, ids)
    _create_and_readback(
        database_engine, redis_client, case, gateway_wire, ids, monkeypatch
    )


def _submit_waiting_for_cover(
    database_engine, redis_client, case, preview_id, wire, monkeypatch
):
    """允许可准备组合提交；即使CTA完成，缺封面也不能创建Campaign。"""
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


def _finish_cover(database_engine, redis_client, case, wire, remote, ids):
    from datetime import UTC, datetime, timedelta

    from app.modules.builds.execution import process_step
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.materials.cover_models import MaterialCoverJob
    from app.modules.materials.covers import run_cover

    context = case["context"]
    with Session(database_engine) as db, db.begin():
        job = db.exec(
            select(MaterialCoverJob).where(
                MaterialCoverJob.tenant_id == context.tenant_id
            )
        ).one()
        job_id, dispatch_id, revision = job.id, job.dispatch_id, job.revision
    video = {
        "list": [
            {
                "video_id": "synthetic-target-vid",
                "signature": md5(remote.content).hexdigest(),
                "displayable": True,
                "width": 160,
                "height": 240,
                "video_cover_url": "https://cdn.example/target-cover",
            }
        ]
    }
    receipt = {"image_id": "synthetic-target-image", "signature": "c" * 32}
    _reply(wire, "file_video_ad_info_get", video)
    _reply(wire, "file_image_ad_upload", receipt)
    offset = len(wire["sdk_calls"])
    wire["before"]["callback"] = lambda: wire["sdk_data"].update(
        data=video if len(wire["sdk_calls"]) == offset else receipt
    )
    try:
        run_cover(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            job_id=job_id,
            dispatch_id=dispatch_id,
            revision=revision,
            read=False,
        )
    finally:
        wire["before"]["callback"] = None
    with Session(database_engine) as db:
        job = db.get(MaterialCoverJob, job_id)
        assert job.known_image_id == "synthetic-target-image", job.error_code
        dispatch_id, revision, name = job.dispatch_id, job.revision, job.remote_name
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.advertiser_id == "90071992547409932",
            )
        ).one()
        assert mapping.image_id is None
    _reply(
        wire,
        "file_image_ad_info_get",
        {
            "list": [
                {
                    **receipt,
                    "file_name": name,
                    "displayable": True,
                    "width": 160,
                    "height": 240,
                }
            ]
        },
    )
    run_cover(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        job_id=job_id,
        dispatch_id=dispatch_id,
        revision=revision,
        read=True,
    )
    with Session(database_engine) as db, db.begin():
        mapping = db.exec(
            select(AccountMaterial).where(
                AccountMaterial.tenant_id == context.tenant_id,
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


def _create_and_readback(database_engine, redis_client, case, wire, ids, monkeypatch):
    import json
    from decimal import Decimal

    from app.integrations.tiktok.mcp.protocol import load_tool_contracts
    from app.modules.builds import reconciliation
    from app.modules.builds.execution import process_step
    from app.modules.builds.execution_models import ExecutionStep
    from app.modules.builds.route_models import BuildAttemptContext
    from app.modules.builds.routes import load_preview_route
    from tests.integrations.tiktok.build_wire import BuildWire
    from tests.modules.builds.test_channel_execution import created

    context, route = case["context"], case["route"]
    monkeypatch.setattr(reconciliation, "require_bounded_worker", lambda: None)
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
        reads = {
            db.get(ExecutionStep, step.parent_step_id).kind: step.id
            for step in db.exec(
                select(ExecutionStep).where(
                    ExecutionStep.submission_id == original[0].submission_id,
                    ExecutionStep.kind == "READBACK",
                )
            ).all()
        }
    sdk_start, mcp_start = len(wire["sdk_calls"]), len(wire["wire"].calls)
    for kind, row in observed.items():
        tool = next(
            contract.tool_name
            for contract in load_tool_contracts()
            if contract.operation == BuildWire.operations[kind]
        )
        _reply(
            wire,
            tool,
            {
                "list": [row],
                "page_info": {
                    "page": 1,
                    "page_size": 100,
                    "total_page": 1,
                    "total_number": 1,
                },
            },
        )
        result = reconciliation.process_reconciliation(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            step_id=reads[kind],
            revision=0,
        )
        with Session(database_engine) as db:
            step = db.get(ExecutionStep, reads[kind])
            source = db.get(ExecutionStep, ids[kind])
            assert step.status == "SUCCEEDED", (kind, result, step.error_code)
            assert not source.mismatch and source.operation_status == "ENABLE"
    assert all(call[0] == "GET" for call in wire["sdk_calls"][sdk_start:])
    mcp_reads = [
        call["params"]["name"]
        for call in wire["wire"].calls[mcp_start:]
        if call["method"] == "tools/call"
    ]
    assert all(name.endswith("_get") for name in mcp_reads)
    assert len(mcp_reads) == (3 if route.channel == "OFFICIAL_MCP" else 0)
    assert len(wire["sdk_calls"][sdk_start:]) == (
        3 if route.channel == "OFFICIAL_API" else 0
    )

    # 当前CTA详情缺实际advertiser事实时只能保持不完整，不能借请求账户伪造MATCH。
    from datetime import UTC, datetime, timedelta

    from app.integrations.tiktok.contracts.builds import BuildReadQuery
    from app.integrations.tiktok.gateway import open_tiktok_gateway

    with Session(database_engine) as db:
        cta = db.get(ExecutionStep, ids["CTA"])
        intent = reconciliation.source_intent(db, cta, route)
    _reply(
        wire,
        "creative_portfolio_get",
        {
            "creative_portfolio_id": "synthetic-portfolio",
            "creative_portfolio_type": "CTA",
            "portfolio_content": [
                {"asset_ids": ["cta-watch"], "asset_content": "Watch now"}
            ],
        },
    )
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=route,
        task_deadline=datetime.now(UTC) + timedelta(seconds=30),
    ) as gateway:
        page = gateway.builds.read_page(
            query=BuildReadQuery(intent=intent, remote_id="synthetic-portfolio")
        )
    assert page.rows[0].remote_id == "synthetic-portfolio"
    assert (
        page.rows[0].intent is None and "advertiser_id" in page.rows[0].missing_fields
    )
    from app.modules.builds.submissions import get_submission

    with Session(database_engine) as db:
        result = get_submission(
            db, context=context, submission_id=original[0].submission_id
        )
        assert result.status == "COMPLETED"
        assert result.execution_route.connection_id == route.connection_id
        assert result.execution_route.channel == route.channel


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
    _reply(wire, "file_video_ad_search", {"list": [{**details["list"][0], **created}], "page_info": {"page": 1, "page_size": 100, "total_page": 1, "total_number": 1}})
    run_distribution(database_engine=database_engine, redis_client=redis_client, context=context, distribution_id=prepared.task_id, kind="verify")
    _reply(
        wire, "file_video_ad_info_get", {"list": [{**details["list"][0], **created}]}
    )
    run_distribution(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        distribution_id=prepared.task_id,
        kind="verify",
    )
    with Session(database_engine) as db:
        dist = db.get(MaterialDistribution, prepared.task_id)
        assert dist.status == "ready", dist.reason_code
        mappings = db.exec(
            select(AccountMaterial).where(AccountMaterial.material_id == material_id)
        ).all()
        assert {(row.advertiser_id, row.video_id, row.mid) for row in mappings} == {
            (source_advertiser, "synthetic-source-vid", "synthetic-source-mid"),
            (target_advertiser, "synthetic-target-vid", "synthetic-target-mid"),
        }
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
            "file_video_ad_search",
            "file_video_ad_info_get",
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
        assert json.loads(calls[1][2]["body"])["shared_advertiser_ids"] == [target_advertiser]
        assert all("/upload/" not in call[1] for call in calls)
