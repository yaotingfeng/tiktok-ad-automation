"""旧场景/草稿迁移保留事实；仅使用独立历史 PostgreSQL 数据库。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.models import AdvertiserAccount, TenantBC, TikTokConnection
from app.modules.builds.models import BuildDraft, DraftPreparation
from app.modules.builds.scene_job_models import (
    DraftScenePreparation,
    SceneJob,
    SceneJobPage,
)
from app.modules.providers.models import ProviderApplication, ProviderConnection
from app.modules.strategies.copy_pool import POOL_VERSION
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import create_strategy
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def test_historical_scene_migration_preserves_ids_and_facts_without_inventing_route(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_build_routes") as (engine, config):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            conn = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            provider = ProviderConnection(
                tenant_id=context.tenant_id,
                kind="wangyan",
                display_name="Synthetic",
                encrypted_credentials="synthetic",
            )
            db.add_all(
                [
                    conn,
                    provider,
                    TenantBC(tenant_id=context.tenant_id, bc_id="old-bc"),
                    AdvertiserAccount(
                        tenant_id=context.tenant_id, advertiser_id="old-advertiser"
                    ),
                ]
            )
            db.flush()
            db.add(
                ProviderApplication(
                    tenant_id=context.tenant_id,
                    connection_id=provider.id,
                    external_id="old-app",
                    name="Synthetic",
                )
            )
            strategy = create_strategy(
                db,
                context=context,
                name="Historical",
                config=StrategyConfig.model_validate(
                    {
                        "budget": "100",
                        "currency": "USD",
                        "target_roas": "1",
                        "group_size": 10,
                        "creative_count": 2,
                        "copy_pool_version": POOL_VERSION,
                    }
                ),
            )
            version = db.exec(
                select(StrategyVersion).where(StrategyVersion.strategy_id == strategy)
            ).one()
            draft = BuildDraft(
                tenant_id=context.tenant_id,
                bc_id="old-bc",
                status="READY",
                strategy_version_id=version.id,
                provider_connection_id=provider.id,
                application_id="old-app",
                link_config={},
                created_by=context.actor_id,
                request_id=uuid4(),
                request_digest="d" * 64,
            )
            db.add(draft)
            db.flush()
            prep = DraftPreparation(
                tenant_id=context.tenant_id,
                draft_id=draft.id,
                draft_revision=1,
                actor_id=context.actor_id,
                request_id=uuid4(),
                status="READY",
                phase="done",
            )
            db.add(prep)
            db.flush()
            job = SceneJob(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                bc_id="old-bc",
                advertiser_id="old-advertiser",
                connection_id=conn.id,
                credential_revision=7,
                provider_connection_id=provider.id,
                application_id="old-app",
                minis_id="old-minis",
                scope_basis="b" * 64,
                status="COMPLETE",
                resource="done",
                facts={"identity": {"matches": [{"identity_id": "old-remote-id"}]}},
                claim_token=uuid4(),
                claimed_until=datetime.now(UTC) + timedelta(minutes=1),
            )
            page = SceneJobPage(
                tenant_id=context.tenant_id,
                job_id=job.id,
                resource="identity",
                page=1,
                endpoint="/old/identity",
                request_id="old-request",
                source_revision="old-contract",
                scope_basis=job.scope_basis,
                facts={"remote_id": "old-remote-id"},
                observed_at=datetime.now(UTC),
            )
            state = DraftScenePreparation(
                tenant_id=context.tenant_id, draft_id=draft.id, preparation_id=prep.id
            )
            for model, row, omitted in (
                (SceneJob, job, {"frozen_route"}),
                (SceneJobPage, page, {"mcp_request_id", "remote_task_id"}),
                (DraftScenePreparation, state, {"frozen_route"}),
            ):
                table = Table(
                    model.__tablename__, MetaData(), autoload_with=db.connection()
                )
                db.execute(table.insert().values(**row.model_dump(exclude=omitted)))
            job_id, page_id, draft_id, prep_id = job.id, page.id, draft.id, prep.id
            route = FrozenTikTokRoute(
                tenant_id=context.tenant_id,
                bc_id="old-bc",
                connection_id=conn.id,
                channel="OFFICIAL_API",
                authorization_revision=0,
                adapter_contract_revision="official-api-v1",
            )
        command.upgrade(config, "mcp02")
        with Session(engine) as db:
            restored = db.get(SceneJob, job_id)
            assert (
                restored.id == job_id and restored.connection_id == route.connection_id
            )
            assert restored.facts == job.facts and restored.credential_revision == 7
            assert (
                restored.status == "STALE"
                and restored.error_code == "scene_route_missing"
            )
            assert (
                restored.frozen_route
                is restored.claim_token
                is restored.claimed_until
                is None
            )
            receipt = db.get(SceneJobPage, page_id)
            assert receipt.request_id == "old-request" and receipt.facts == page.facts
            assert receipt.mcp_request_id is receipt.remote_task_id is None
            assert db.get(DraftPreparation, prep_id).status == "BLOCKED"
            assert db.get(BuildDraft, draft_id).status == "BLOCKED"
            assert (
                db.get(DraftScenePreparation, (context.tenant_id, prep_id)).frozen_route
                is None
            )
        for model, key in (
            (SceneJob, job_id),
            (DraftScenePreparation, (context.tenant_id, prep_id)),
        ):
            with Session(engine) as db, pytest.raises(IntegrityError):
                db.get(model, key).frozen_route = route.model_dump(mode="json")
                db.commit()


def test_new_scene_route_is_immutable_and_invalid_json_cannot_be_inserted(
    database_engine, scene_case
):
    from tests.modules.builds.scene.support import ensure

    result = ensure(database_engine, scene_case)
    with Session(database_engine) as db:
        original = db.get(SceneJob, result.job_id).model_dump()
    with Session(database_engine) as db, pytest.raises(IntegrityError):
        db.get(SceneJob, result.job_id).frozen_route = {
            **original["frozen_route"],
            "authorization_revision": 99,
        }
        db.commit()
    for invalid in (
        {},
        {**original["frozen_route"], "tenant_id": str(uuid4())},
        {**original["frozen_route"], "authorization_revision": True},
        {**original["frozen_route"], "channel": "OTHER"},
    ):
        with Session(database_engine) as db, pytest.raises(IntegrityError):
            db.add(
                SceneJob(
                    **(
                        original
                        | {
                            "id": uuid4(),
                            "scope_basis": uuid4().hex,
                            "frozen_route": invalid,
                        }
                    )
                )
            )
            db.commit()
