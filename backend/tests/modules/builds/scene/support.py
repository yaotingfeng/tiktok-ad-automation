"""场景测试的真实数据库事实、worker 调用与 HTTP 数据；不替换业务服务。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, delete

from app.modules.accounts.capabilities import _directory_basis
from app.modules.accounts.capability_models import (
    CapabilityAsset,
    CapabilityJob,
    CapabilityPage,
    CapabilityRequest,
)
from app.modules.builds.scene_job_models import SceneJob, SceneJobPage
from app.modules.providers.models import (
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
)


@pytest.fixture
def scene_case(database_engine, gateway_case, monkeypatch):
    from app.modules.builds import scene_jobs

    # 测试直接调用生产 worker；只免除 Celery 进程宿主检查，保留权限和实际准入。
    monkeypatch.setattr(scene_jobs, "_require_bounded_worker", lambda: None)
    context, route, advertiser = gateway_case
    with Session(database_engine) as db, db.begin():
        provider = ProviderConnection(
            tenant_id=context.tenant_id,
            kind="wangyan",
            display_name="Synthetic",
            encrypted_credentials="synthetic",
            status="active",
            verification_token=uuid4(),
        )
        db.add(provider)
        db.flush()
        app = ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=provider.id,
            external_id="synthetic-app",
            name="Synthetic",
            tiktok_minis_id="synthetic-minis",
            channel_config={"verification_token": str(provider.verification_token)},
        )
        db.add(app)
        db.flush()
        drama = ProviderDrama(
            tenant_id=context.tenant_id,
            connection_id=provider.id,
            application_id=app.external_id,
            external_drama_id="synthetic-drama",
            title="Synthetic",
            language="en",
        )
        db.add(drama)
        db.flush()
        link = PromotionLink(
            tenant_id=context.tenant_id,
            connection_id=provider.id,
            application_id=app.external_id,
            drama_id=drama.id,
            reuse_key="a" * 64,
            config={},
            version=1,
            remote_id="synthetic-link",
            url="https://example.test/link",
            protected_base="base",
            attribution={"source": "synthetic"},
            verified_at=datetime.now(UTC),
            status="ready",
        )
        db.add(link)
        proof = CapabilityJob(
            tenant_id=context.tenant_id,
            bc_id=route.bc_id,
            connection_id=route.connection_id,
            actor_id=context.actor_id,
            credential_revision=1,
            channel=route.channel,
            authorization_revision=route.authorization_revision,
            adapter_contract_revision=route.adapter_contract_revision,
            directory_basis=_directory_basis(
                db, context, route.bc_id, route.connection_id
            ),
            status="COMPLETE",
            phase="DONE",
            scope_known=True,
            scope_build=True,
            scope_upload=True,
            completed_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db.add(proof)
        db.flush()
        db.add(
            CapabilityPage(
                job_id=proof.id,
                page=1,
                tenant_id=context.tenant_id,
                bc_id=route.bc_id,
                row_count=1,
            )
        )
        db.flush()
        db.add(
            CapabilityAsset(
                job_id=proof.id,
                advertiser_id=advertiser,
                tenant_id=context.tenant_id,
                bc_id=route.bc_id,
                page=1,
                role="OPERATOR",
            )
        )
        result = {
            "context": context,
            "route": route,
            "advertiser_id": advertiser,
            "link_id": link.id,
            "provider_id": provider.id,
            "application_id": app.external_id,
        }
    try:
        yield result
    finally:
        with Session(database_engine) as db, db.begin():
            for model in (
                SceneJobPage,
                SceneJob,
                CapabilityRequest,
                CapabilityAsset,
                CapabilityPage,
                CapabilityJob,
                PromotionLink,
                ProviderDrama,
                ProviderApplication,
                ProviderConnection,
            ):
                db.exec(delete(model).where(model.tenant_id == context.tenant_id))


def ensure(database_engine, case):
    from app.modules.builds.scene_jobs import ensure_scene_preparation

    with Session(database_engine) as db, db.begin():
        return ensure_scene_preparation(
            db,
            context=case["context"],
            bc_id=case["route"].bc_id,
            advertiser_id=case["advertiser_id"],
            link_id=case["link_id"],
            route=case["route"],
        )


def run(database_engine, redis_client, case, job_id):
    from app.modules.builds.scene_jobs import process_scene_job

    with Session(database_engine) as db, db.begin():
        job = db.get(SceneJob, job_id)
        job.due_at = datetime.now(UTC) - timedelta(seconds=1)
        revision = job.revision
    process_scene_job(
        database_engine=database_engine,
        redis_client=redis_client,
        tenant_id=case["context"].tenant_id,
        actor_id=case["context"].actor_id,
        payload={"job_id": str(job_id), "revision": revision},
    )
    with Session(database_engine) as db:
        return db.get(SceneJob, job_id)


def page(rows, *, key="list", number=1, total=None):
    total = len(rows) if total is None else total
    return {
        key: rows,
        "page_info": {
            "page": number,
            "page_size": 50,
            "total_number": total,
            "total_page": (total + 49) // 50,
        },
    }


def scene_responses(case):
    return {
        "identity": page(
            [
                {
                    "identity_id": "synthetic-identity",
                    "identity_type": "BC_AUTH_TT",
                    "identity_authorized_bc_id": case["route"].bc_id,
                    "available_status": "AVAILABLE",
                    "can_push_video": True,
                    "is_gpppa": False,
                }
            ],
            key="identity_list",
        ),
        "minis": page(
            [
                {
                    "minis_id": "synthetic-minis",
                    "minis_status": "ACTIVE",
                    "minis_type": "MINI_SERIES",
                    "region_codes": ["US"],
                }
            ]
        ),
        "cta": {
            "recommend_assets": [
                {"asset_ids": ["cta-watch"], "asset_content": "Watch now"}
            ]
        },
        "vbo": {"vo_min_roas": "QUALIFIED"},
        "regions": {
            "region_list": ["US"],
            "region_info": [
                {
                    "region_code": "US",
                    "location_id": "6252001",
                    "level": "COUNTRY",
                    "area_type": "ADMIN",
                }
            ],
        },
    }


def enqueue(wire, resource, data):
    from app.integrations.tiktok.mcp.protocol import load_tool_contracts

    operations = {
        "account_roles": "accounts.list_bc_assets",
        "identity": "scene.list_identities",
        "minis": "scene.list_minis",
        "cta": "scene.recommend_ctas",
        "vbo": "scene.check_vbo",
        "regions": "scene.list_regions",
    }
    tool = next(
        c.tool_name
        for c in load_tool_contracts()
        if c.operation == operations[resource]
    )
    wire["sdk_data"]["data"] = data
    wire["wire"].results[tool].clear()
    wire["wire"].results[tool].append(
        {
            "content": [],
            "structuredContent": {
                "code": 0,
                "data": data,
                "request_id": "synthetic-scene-request",
                "mcp_request_id": "synthetic-mcp-request",
                "remote_task_id": "synthetic-remote-task",
            },
        }
    )
