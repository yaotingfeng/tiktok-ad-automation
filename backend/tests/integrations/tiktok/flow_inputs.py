"""跨阶段输入准备：真实R2状态机/版权方HTTP/草稿与预览，不替换业务结果。"""

import subprocess
from hashlib import md5, sha256
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.config import settings
from app.integrations.tiktok import material_upload_evidence
from app.modules.materials import ingest_service, ingest_transport
from app.modules.materials.channel_policy import MaterialUploadPolicy
from app.modules.materials.ingest_models import IngestSession
from app.modules.materials.ingest_schemas import (
    IngestChunkCreate,
    IngestFileInput,
    IngestIdentity,
    IngestSessionCreate,
)
from app.modules.materials.object_validation import validate_original
from tests.acceptance.test_r2_pipeline import PipelineStorage
from tests.modules.builds.scene.support import enqueue, run, scene_responses


class FlowStorage(PipelineStorage):
    """复用 R2 传输替身；锁检查必须使用当前独占库，不误读共享库。"""

    def __init__(self, content, database_engine):
        super().__init__(content)
        self.database_engine = database_engine

    def record(self, name, values):
        self.calls.append((name, values))
        key = values.get("Key") or values.get("Params", {}).get("Key")
        if key:
            material_id = key.split("/materials/")[1].split("/")[0]
            with Session(self.database_engine) as db, db.begin():
                db.execute(
                    text(
                        "SELECT id FROM material_file WHERE id = :id FOR UPDATE NOWAIT"
                    ),
                    {"id": material_id},
                ).one()


@pytest.fixture
def flow_storage(tmp_path, monkeypatch, gateway_case, database_engine):
    path = tmp_path / "Synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=160x240:d=0.2",
            "-an",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    remote = FlowStorage(path.read_bytes(), database_engine)
    for key, value in {
        "MATERIAL_INGEST_ENABLED": True,
        "OBJECT_STORAGE_PROVIDER": "r2",
        "S3_ENDPOINT_URL": "https://pipeline.r2.cloudflarestorage.com",
        "S3_BUCKET": "pipeline-private",
        "S3_ACCESS_KEY_ID": "synthetic-only",
        "S3_SECRET_ACCESS_KEY": "synthetic-only",
    }.items():
        monkeypatch.setattr(settings, key, value)
    route = gateway_case[1]
    # API 叶scope属于合成初始授权事实；不得在已冻结流程中伪装成普通token轮换。
    from app.core.credentials import decrypt_credentials, encrypt_credentials
    from app.modules.accounts.models import TikTokConnection

    context = gateway_case[0]
    with Session(database_engine) as db, db.begin():
        if route.channel == "OFFICIAL_API":
            connection = db.get(TikTokConnection, route.connection_id)
            credentials = decrypt_credentials(
                tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
            )
            credentials["scope"] = "[6]"
            connection.credential_ciphertext = encrypt_credentials(
                tenant_id=context.tenant_id, value=credentials
            )
    if route.channel == "OFFICIAL_MCP":
        monkeypatch.setattr(settings, "TIKTOK_APP_ID", "")
        monkeypatch.setattr(settings, "TIKTOK_APP_SECRET", "")
    # 仅给离线合同目录注入已标识的合成能力，不把它冒充真实平台保证。
    evidence = material_upload_evidence.MaterialUploadEvidence(
        channel=route.channel,
        adapter_contract_revision=route.adapter_contract_revision,
        policy=MaterialUploadPolicy(1024 * 1024),
        category="SYNTHETIC",
        sources=("P3.6 local HTTP fixture",),
        notes="Not live upload or permission verification.",
    )
    monkeypatch.setattr(material_upload_evidence, "UPLOAD_EVIDENCE", (evidence,))
    policies = dict(settings.TIKTOK_CALL_POLICIES)
    policies["base"] = {**policies["base"], "lease_ms": 970000}
    monkeypatch.setattr(settings, "TIKTOK_CALL_POLICIES", policies)
    return remote


def _identity(row):
    return IngestIdentity(
        generation=row.generation,
        upload_id=row.upload_id,
        operation_revision=row.operation_revision,
    )


def _store(database_engine, context, route, remote):
    """真实注册、multipart 完成与摘要验证；只替换对象存储客户端。"""
    from app.jobs.models import PendingDispatch
    from app.modules.materials.ingest_models import TemporaryMaterialObject
    from app.modules.materials.models import MaterialFile

    with Session(database_engine) as db, db.begin():
        parent = ingest_service.create_session(
            db,
            context=context,
            body=IngestSessionCreate(
                bc_id=route.bc_id,
                request_id=uuid4(),
                file_count=1,
                total_bytes=len(remote.content),
            ),
        )
        chunk = ingest_service.register_chunk(
            db,
            context=context,
            session_id=parent.session_id,
            body=IngestChunkCreate(
                request_id=uuid4(),
                files=[
                    IngestFileInput(
                        client_index=0,
                        file_name="Synthetic.mp4",
                        size=len(remote.content),
                        mime_type="video/mp4",
                    )
                ],
            ),
        )
        assert ingest_service.seal_session(
            db,
            context=context,
            session_id=parent.session_id,
        ).sealed
    row = chunk.items[0]
    args = {
        "database_engine": database_engine,
        "context": context,
        "session_id": parent.session_id,
        "material_id": row.material_id,
        "s3": remote,
    }
    row = ingest_transport.resume_file(**args, identity=_identity(row))
    remote.receive_bytes(row.model_dump())
    row = ingest_transport.complete_file(**args, identity=_identity(row))
    assert row.temporary_storage_status == "stored"
    with Session(database_engine) as db:
        dispatch = db.get(PendingDispatch, row.task_id)
        payload, dispatch_id = dict(dispatch.payload), dispatch.id
    validate_original(
        database_engine=database_engine,
        context=context,
        object_id=UUID(payload["object_id"]),
        generation=payload["generation"],
        revision=payload["revision"],
        dispatch_id=dispatch_id,
        s3=remote,
    )
    with Session(database_engine) as db:
        obj = db.get(TemporaryMaterialObject, UUID(payload["object_id"]))
        file = db.get(MaterialFile, row.material_id)
        assert obj.status == "verified", obj.error_code
        assert (file.video_md5, file.sha256) == (
            md5(remote.content).hexdigest(),
            sha256(remote.content).hexdigest(),
        )
        assert db.get(
            IngestSession, parent.session_id
        ).frozen_route == route.model_dump(mode="json")
    return parent.session_id, row.material_id, obj.id, obj.generation


def _prepare_preview(database_engine, redis_client, case, wire, monkeypatch):
    """草稿所有阶段均走真实生产函数，版权方仅允许合成域名的读取。"""
    from app.core.credentials import encrypt_credentials
    from app.modules.accounts import capabilities
    from app.modules.accounts.capability_models import CapabilityJob
    from app.modules.builds.drafts import continue_draft, create_draft, prepare_draft
    from app.modules.builds.models import BuildDraft, DraftPreparation
    from app.modules.builds.preview_models import BuildPreview
    from app.modules.builds.previews import continue_preview, generate_preview
    from app.modules.builds.scene_job_models import SceneJob
    from app.modules.providers.adapters import wangyan
    from app.modules.providers.link_steps import run_link_item
    from app.modules.providers.models import (
        LinkPreparationItem,
        ProviderApplication,
        ProviderConnection,
    )
    from app.modules.strategies.models import StrategyVersion
    from app.modules.strategies.service import create_strategy
    from tests.modules.accounts.capabilities.test_service import page
    from tests.modules.accounts.test_capability_boundaries import run_capability
    from tests.modules.strategies.test_versions import config

    context, route = case["context"], case["route"]
    monkeypatch.setattr(wangyan, "BASE", "https://synthetic-provider.example")
    monkeypatch.setattr(capabilities, "_require_bounded_worker", lambda: None)
    with Session(database_engine) as db, db.begin():
        provider = db.get(ProviderConnection, case["provider_id"])
        provider.encrypted_credentials = encrypt_credentials(
            tenant_id=context.tenant_id,
            value={"token": "synthetic-flow-provider"},
        )
        app = db.exec(
            select(ProviderApplication).where(
                ProviderApplication.connection_id == provider.id,
                ProviderApplication.external_id == case["application_id"],
            )
        ).one()
        app.channel_config = {**app.channel_config, "is_tt": True}
        strategy = create_strategy(
            db,
            context=context,
            name="Synthetic full flow",
            config=config(creative_count=1),
        )
        version = db.exec(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
        ).one()
        draft_id = create_draft(
            db,
            context=context,
            bc_id=route.bc_id,
            strategy_version_id=version.id,
            provider_connection_id=provider.id,
            application_id=case["application_id"],
            drama_lines=["Synthetic"],
            account_lines=["90071992547409932"],
            link_config={"episode": 1},
            execution_connection_id=route.connection_id,
        )
        prep_id = prepare_draft(
            db, context=context, draft_id=draft_id, request_id=uuid4()
        )
        provider_task = db.get(DraftPreparation, prep_id).provider_task_id
    provider_calls = []

    def provider_response(request):
        assert request.url.host == "synthetic-provider.example"
        assert request.method == "GET"
        assert request.headers["cookie"] == "x-ds-admin-token=synthetic-flow-provider"
        provider_calls.append(request.url.path)
        query = dict(request.url.params)
        if request.url.path == "/api/distribute_admin/drama/list":
            assert query["title"] == "Synthetic"
            data = (
                [{"id": "synthetic-drama", "title": "Synthetic", "lang": "en"}]
                if query["page"] == "1"
                else []
            )
            return httpx.Response(200, json={"code": 0, "data": data})
        assert request.url.path == "/api/distribute_admin/promote/link/list"
        assert query["app"] == case["application_id"]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "total": 1,
                "data": [
                    {
                        "id": 901,
                        "app": case["application_id"],
                        "drama_id": "synthetic-drama",
                        "drama_int_id": 73,
                        "chapter_index": 1,
                        "promote_platform": "tiktok",
                        "promote_name": "synthetic-existing-link",
                        "tt_minis_link": "https://www.tiktok.com/t/synthetic-flow",
                    }
                ],
            },
        )

    for _ in range(20):
        with Session(database_engine) as db, db.begin():
            continue_draft(db, context=context, task_id=prep_id)
            draft = db.get(BuildDraft, draft_id)
            if draft.status in {"READY", "BLOCKED"}:
                assert draft.status == "READY", db.get(
                    DraftPreparation, prep_id
                ).error_code
                break
            items = db.exec(
                select(LinkPreparationItem.id).where(
                    LinkPreparationItem.preparation_id == provider_task,
                    LinkPreparationItem.status.not_in(
                        ["ready", "failed", "blocked_auth"]
                    ),
                )
            ).all()
            cap_jobs = db.exec(
                select(CapabilityJob.id).where(
                    CapabilityJob.tenant_id == context.tenant_id,
                    CapabilityJob.status == "PENDING",
                )
            ).all()
            scenes = db.exec(
                select(SceneJob.id).where(
                    SceneJob.tenant_id == context.tenant_id,
                    SceneJob.status == "PENDING",
                )
            ).all()
        for item_id in items:
            with Session(database_engine) as db:
                run_link_item(
                    db,
                    context=context,
                    item_id=item_id,
                    transport=httpx.MockTransport(provider_response),
                )
        for job_id in cap_jobs:
            enqueue(
                wire,
                "account_roles",
                page([case["advertiser_id"], "90071992547409932"]),
            )
            run_capability(
                database_engine,
                redis_client,
                (context, route, case["advertiser_id"], job_id),
            )
        for job_id in scenes:
            for resource, data in scene_responses(case).items():
                enqueue(wire, resource, data)
                result = run(database_engine, redis_client, case, job_id)
            assert result.status == "COMPLETE", result.error_code
    else:
        pytest.fail("真实草稿准备在20个有界步骤内未结束")
    assert provider_calls and all("/create" not in path for path in provider_calls)
    with Session(database_engine) as db, db.begin():
        preview_id = generate_preview(
            db, context=context, draft_id=draft_id, expected_revision=1
        )
    for _ in range(10):
        with Session(database_engine) as db, db.begin():
            complete = continue_preview(db, context=context, preview_id=preview_id)
            preview = db.get(BuildPreview, preview_id)
            if complete:
                assert preview.status == "FROZEN", preview.error_code
                return preview_id
    pytest.fail("真实预览准备在10个有界步骤内未结束")
