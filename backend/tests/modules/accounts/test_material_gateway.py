"""冻结素材来源通过真实 gateway/PG/Redis，只替换第三方 HTTP。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import load_tool_contracts
from app.jobs.models import PendingDispatch
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.remote_sources import read_remote_source
from tests.modules.accounts.test_gateway_factory import (
    business_calls,
)
from tests.modules.accounts.test_gateway_factory import (
    database_engine as database_engine,
)
from tests.modules.accounts.test_gateway_factory import (
    gateway_case as gateway_case,
)
from tests.modules.accounts.test_gateway_factory import (
    gateway_wire as gateway_wire,
)


@pytest.fixture
def source_material(gateway_case, database_engine, monkeypatch):
    context, route, advertiser = gateway_case
    monkeypatch.setattr(
        settings, "MATERIAL_REMOTE_MEDIA_HOSTS", frozenset({"media.example.com"})
    )
    with Session(database_engine) as db, db.begin():
        material = MaterialFile(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            file_name="fixed.mp4",
            object_key=f"synthetic/{uuid4()}",
            byte_size=120,
            video_md5="a" * 32,
            storage_state="unavailable",
        )
        db.add(material)
        db.flush()
        source = AccountMaterial(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            material_id=material.id,
            advertiser_id=advertiser,
            connection_id=route.connection_id,
            video_id="source-video",
            status="available",
            verified_at=datetime.now(UTC),
        )
        db.add(source)
        db.flush()
        result = material.id, source.id
    try:
        yield result
    finally:
        with Session(database_engine) as db, db.begin():
            db.exec(delete(AccountMaterial).where(AccountMaterial.id == result[1]))
            db.exec(delete(MaterialFile).where(MaterialFile.id == result[0]))


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize("problem", [None, "digest", "size", "durable_change"])
def test_frozen_source_compares_actual_remote_identity_to_durable_material(
    database_engine, redis_client, gateway_case, gateway_wire, source_material, problem
):
    context, route, advertiser = gateway_case
    material_id, source_id = source_material
    data = {
        "list": [
            {
                "advertiser_id": advertiser,
                "video_id": "source-video",
                "signature": ("b" if problem == "digest" else "a") * 32,
                "size": 121 if problem == "size" else 120,
                "width": 1080,
                "height": 1920,
                "duration": 4.5,
                "format": "mp4",
                "displayable": True,
                "preview_url": "https://media.example.com/video?signature=private",
            }
        ]
    }
    gateway_wire["sdk_data"]["data"] = data
    tool = next(
        c.tool_name
        for c in load_tool_contracts()
        if c.operation == "materials.get_videos"
    )
    gateway_wire["wire"].results[tool].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": data,
                "request_id": "material-request",
            },
        }
    )
    if problem == "durable_change":

        def mutate():
            gateway_wire["before"]["callback"] = None
            with Session(database_engine) as db, db.begin():
                db.get(MaterialFile, material_id).video_md5 = "c" * 32

        gateway_wire["before"]["callback"] = mutate
    kwargs = {
        "database_engine": database_engine,
        "redis_client": redis_client,
        "context": context,
        "bc_id": route.bc_id,
        "material_id": material_id,
        "source_asset_id": source_id,
        "deadline": datetime.now(UTC) + timedelta(seconds=180),
        "hard_limit": 200,
        "extend_lease": True,
    }
    if problem:
        with pytest.raises(DomainError) as error:
            read_remote_source(**kwargs)
        assert error.value.code == "material_preview_unverified"
    else:
        preview = read_remote_source(**kwargs)
        assert preview.md5 == "a" * 32 and preview.size == 120
        assert preview.evidence.request_id
    assert len(business_calls(gateway_wire, route.channel)) == 1
    # 本例令牌新鲜，所以连凭据刷新也不排队；短令牌的单独用例允许刷新投递。
    with Session(database_engine) as db:
        assert (
            db.exec(
                select(func.count())
                .select_from(PendingDispatch)
                .where(PendingDispatch.tenant_id == context.tenant_id)
            ).one()
            == 0
        )
        assert "signature=private" not in repr(db.get(MaterialFile, material_id))


def after_material_http(monkeypatch, channel, mutate):
    """只在实际业务 HTTP 响应已到达后改变独立数据库会话。"""
    if channel == "OFFICIAL_API":
        import urllib3

        original = urllib3.PoolManager.request

        def response(pool, method, url, **kwargs):
            result = original(pool, method, url, **kwargs)
            if "/file/video/ad/info/" in url:
                mutate()
            return result

        monkeypatch.setattr(urllib3.PoolManager, "request", response)
    else:
        import json

        from app.integrations.tiktok.mcp import transport

        original = transport._new_http_transport

        class ResponseBoundary(original):
            async def handle_async_request(self, request):
                message = (
                    json.loads(request.content) if request.method == "POST" else {}
                )
                result = await super().handle_async_request(request)
                if message.get("method") == "tools/call":
                    mutate()
                return result

        monkeypatch.setattr(transport, "_new_http_transport", ResponseBoundary)


@pytest.mark.parametrize(
    "gateway_case", ["OFFICIAL_API", "OFFICIAL_MCP"], indirect=True
)
@pytest.mark.parametrize(
    "change", ["authorization", "contract", "read_fact", "binding", "credential"]
)
def test_preview_revalidates_frozen_authority_after_actual_http(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
    source_material,
    monkeypatch,
    change,
):
    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        BCDefaultRoute,
        ConnectionAuthorization,
    )
    from app.modules.accounts.models import TikTokConnection
    from app.modules.materials.catalog import remote_preview

    context, route, advertiser = gateway_case
    material_id, _ = source_material
    data = {
        "list": [
            {
                "advertiser_id": advertiser,
                "video_id": "source-video",
                "signature": "a" * 32,
                "size": 120,
                "width": 1080,
                "height": 1920,
                "duration": 4.5,
                "format": "mp4",
                "displayable": True,
                "preview_url": "https://media.example.com/video?signature=private",
            }
        ]
    }
    gateway_wire["sdk_data"]["data"] = data
    tool = next(
        c.tool_name
        for c in load_tool_contracts()
        if c.operation == "materials.get_videos"
    )
    gateway_wire["wire"].results[tool].append(
        {"content": [], "structuredContent": {"code": 0, "data": data}}
    )

    def mutate():
        with Session(database_engine) as db, db.begin():
            connection = db.get(TikTokConnection, route.connection_id)
            if change == "authorization":
                connection.authorization_revision += 1
            elif change == "contract":
                connection.adapter_contract_revision = "synthetic-next-contract"
            elif change == "credential":
                from app.core.credentials import (
                    decrypt_credentials,
                    encrypt_credentials,
                )

                credentials = decrypt_credentials(
                    tenant_id=context.tenant_id,
                    ciphertext=connection.credential_ciphertext,
                )
                credentials["access_token"] = "synthetic-rotated-token"
                connection.credential_ciphertext = encrypt_credentials(
                    tenant_id=context.tenant_id, value=credentials
                )
                connection.credential_revision += 1
            elif change == "read_fact":
                auth = db.exec(
                    select(ConnectionAuthorization).where(
                        ConnectionAuthorization.connection_id == route.connection_id
                    )
                ).one()
                auth.permission_summary = {
                    **auth.permission_summary,
                    "read_authorized": False,
                }
            else:
                db.exec(
                    delete(BCDefaultRoute).where(
                        BCDefaultRoute.connection_id == route.connection_id
                    )
                )
                db.exec(
                    delete(BCConnectionBinding).where(
                        BCConnectionBinding.connection_id == route.connection_id
                    )
                )

    after_material_http(monkeypatch, route.channel, mutate)
    kwargs = {
        "database_engine": database_engine,
        "redis_client": redis_client,
        "context": context,
        "bc_id": route.bc_id,
        "material_id": material_id,
    }
    if change == "credential":
        preview = remote_preview(**kwargs)
        assert preview.video_id == "source-video"
    else:
        with pytest.raises(DomainError) as error:
            remote_preview(**kwargs)
        assert (
            error.value.code
            == {
                "authorization": "route_authorization_changed",
                "contract": "route_contract_changed",
                "read_fact": "account_access_denied",
                "binding": "connection_bc_mismatch",
            }[change]
        )
    assert len(business_calls(gateway_wire, route.channel)) == 1


def test_initial_preview_source_selection_has_the_same_absolute_deadline(
    database_engine,
    redis_client,
    gateway_case,
    gateway_wire,
    source_material,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import text

    from app.modules.materials import source_uploads
    from app.modules.materials.catalog import remote_preview

    context, route, _ = gateway_case
    monkeypatch.setattr(source_uploads, "READ_HARD_LIMIT", 8)
    # 真 PostgreSQL 表锁阻塞第一次来源 SELECT；3 秒调用预算必须早于测试看门狗结束。
    with ThreadPoolExecutor(max_workers=1) as executor:
        with Session(database_engine) as blocker:
            blocker.execute(
                text("LOCK TABLE account_material IN ACCESS EXCLUSIVE MODE")
            )
            future = executor.submit(
                remote_preview,
                database_engine=database_engine,
                redis_client=redis_client,
                context=context,
                bc_id=route.bc_id,
                material_id=source_material[0],
            )
            try:
                with pytest.raises(DomainError) as error:
                    future.result(timeout=3.7)
                assert error.value.code in {
                    "tiktok_local_resources_unavailable",
                    "tiktok_call_deadline_exceeded",
                }
            finally:
                blocker.rollback()
    assert business_calls(gateway_wire, route.channel) == []


def test_short_mcp_token_queues_only_credential_refresh_not_material_work(
    database_engine, redis_client, gateway_case, gateway_wire, source_material
):
    from app.core.credentials import decrypt_credentials, encrypt_credentials
    from app.modules.accounts.models import TikTokConnection
    from app.modules.materials.catalog import remote_preview

    context, route, _ = gateway_case
    with Session(database_engine) as db, db.begin():
        connection = db.get(TikTokConnection, route.connection_id)
        credentials = decrypt_credentials(
            tenant_id=context.tenant_id, ciphertext=connection.credential_ciphertext
        )
        credentials["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=10)
        ).isoformat()
        connection.credential_ciphertext = encrypt_credentials(
            tenant_id=context.tenant_id, value=credentials
        )
    with pytest.raises(DomainError) as error:
        remote_preview(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            bc_id=route.bc_id,
            material_id=source_material[0],
        )
    assert error.value.code == "mcp_refresh_pending"
    assert gateway_wire["wire"].calls == []
    with Session(database_engine) as db:
        tasks = db.exec(
            select(PendingDispatch.task_name).where(
                PendingDispatch.tenant_id == context.tenant_id
            )
        ).all()
        assert tasks == ["accounts.refresh_mcp"]
