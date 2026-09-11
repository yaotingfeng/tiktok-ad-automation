"""真实历史库验证六列路由迁移；不对共享测试库或生产库降级。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.accounts.models import (
    AdvertiserAccount,
    BCAccountAccess,
    TenantBC,
    TikTokConnection,
)
from app.modules.materials.cover_models import MaterialCoverJob
from app.modules.materials.ingest_models import IngestSession
from app.modules.materials.models import (
    AccountMaterial,
    MaterialAssetOperation,
    MaterialDistribution,
    MaterialFile,
    UploadBatch,
)
from tests.migration_database import historical_database
from tests.modules.conftest import create_context

ROUTES = {
    "ingest_session": ("frozen_route",),
    "material_asset_operation": ("frozen_route",),
    "material_cover_job": ("frozen_route",),
    "material_distribution": ("target_route", "source_route"),
    "upload_batch": ("frozen_route",),
}


def test_history_is_preserved_and_new_route_evidence_cannot_be_rewritten_or_dropped(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_directory_semantics") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            conn = TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE")
            scope = {"tenant_id": context.tenant_id, "bc_id": "historical-material-bc"}
            db.add_all(
                [
                    conn,
                    TenantBC(**scope),
                    AdvertiserAccount(
                        tenant_id=context.tenant_id, advertiser_id="actual-source"
                    ),
                ]
            )
            db.flush()
            db.add(
                BCConnectionBinding(**scope, connection_id=conn.id, kind="OFFICIAL_API")
            )
            db.add(
                BCAccountAccess(
                    **scope, advertiser_id="actual-source", connection_id=conn.id
                )
            )
            material = MaterialFile(
                **scope,
                file_name="historical.mp4",
                object_key="synthetic/old",
                byte_size=42,
            )
            db.add(material)
            db.flush()
            asset = AccountMaterial(
                **scope,
                material_id=material.id,
                advertiser_id="actual-source",
                connection_id=conn.id,
                video_id="actual-video",
                mid="actual-mid",
                image_id="actual-image",
                status="available",
                verified_at=datetime.now(UTC),
            )
            db.add(asset)
            db.flush()
            identity = {
                **scope,
                "material_id": material.id,
                "advertiser_id": "actual-source",
            }
            operation = MaterialAssetOperation(
                **identity,
                path="upload_original",
                status="result_unknown",
                request_digest="d" * 64,
                remote_response={
                    "video_id": "actual-video",
                    "connection_id": str(conn.id),
                },
            )
            rows = [
                IngestSession(
                    **scope,
                    actor_id=context.actor_id,
                    request_id=uuid4(),
                    request_digest="i" * 64,
                    expected_files=1,
                    expected_bytes=42,
                    accepted_count=1,
                    accepted_bytes=42,
                ),
                UploadBatch(
                    **scope,
                    actor_id=context.actor_id,
                    request_id=uuid4(),
                    request_digest="u" * 64,
                ),
                operation,
                MaterialDistribution(
                    **identity,
                    actor_id=context.actor_id,
                    source_asset_id=asset.id,
                    operation_id=operation.id,
                    path="upload_original",
                    status="result_unknown",
                ),
                MaterialCoverJob(
                    **identity,
                    actor_id=context.actor_id,
                    asset_id=asset.id,
                    connection_id=conn.id,
                    video_id=asset.video_id,
                    remote_name="original-cover",
                    status="UNKNOWN",
                    request_armed_at=datetime.now(UTC),
                    known_image_id="actual-image",
                ),
            ]
            originals = {}
            for row in rows:
                table = Table(
                    row.__tablename__, MetaData(), autoload_with=db.connection()
                )
                # 用真实旧 schema 列播种；后续封面摘要等新 ORM 列不属于迁移前证据。
                assert not set(ROUTES[row.__tablename__]).intersection(table.c.keys())
                values = {
                    key: value
                    for key, value in row.model_dump().items()
                    if key in table.c
                }
                db.execute(table.insert().values(**values))
                originals[row.__tablename__] = values
            route = {
                **{k: str(v) for k, v in scope.items()},
                "connection_id": str(conn.id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "adapter_contract_revision": "historical-independent-contract",
            }
        with engine.connect() as db:
            before = {
                table: db.execute(
                    text(f"SELECT to_jsonb(r)::text FROM {table} r")
                ).all()
                for table in ROUTES
            }
        command.upgrade(config, "mcp_material_routes")
        with engine.connect() as db:
            for table, columns in ROUTES.items():
                keys = ",".join(f"'{column}'" for column in columns)
                assert (
                    db.execute(
                        text(f"SELECT (to_jsonb(r)-ARRAY[{keys}])::text FROM {table} r")
                    ).all()
                    == before[table]
                )
                assert (
                    db.execute(
                        text(
                            f"SELECT count(*) FROM {table} WHERE "
                            + " OR ".join(f"{column} IS NOT NULL" for column in columns)
                        )
                    ).scalar_one()
                    == 0
                )
        for table, columns in ROUTES.items():
            reflected = Table(table, MetaData(), autoload_with=engine)
            for column in columns:
                # NULL 历史不得在恢复时按今日连接补写。
                with engine.begin() as db, pytest.raises(IntegrityError):
                    db.execute(reflected.update().values({column: route}))
                values = {**originals[table], "id": uuid4(), column: route}
                if table in {"ingest_session", "upload_batch"}:
                    values["request_id"] = uuid4()
                if table == "material_asset_operation":
                    values["status"] = "failed"  # 不占用已有 UNKNOWN 的唯一身份。
                if table == "material_distribution":
                    values["status"] = "blocked"
                if table == "material_cover_job":
                    values["video_id"] = "second-known-video"
                # JSON 类型、额外字段和跨连接归属均须由数据库拒绝。
                for invalid in (
                    {},
                    {**route, "authorization_revision": True},
                    {**route, "extra": 1},
                    {**route, "connection_id": str(uuid4())},
                    {**route, "channel": "OFFICIAL_MCP"},
                ):
                    with engine.begin() as db, pytest.raises(IntegrityError):
                        db.execute(
                            reflected.insert().values({**values, column: invalid})
                        )
                with engine.begin() as db:
                    db.execute(reflected.insert().values(values))
                with engine.begin() as db, pytest.raises(IntegrityError):
                    db.execute(
                        reflected.update()
                        .where(reflected.c.id == values["id"])
                        .values({column: None})
                    )
                with pytest.raises(RuntimeError, match="frozen material evidence"):
                    command.downgrade(config, "mcp_directory_semantics")
                with engine.begin() as db:
                    assert (
                        db.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one()
                        == "mcp_material_routes"
                    )
                    db.execute(reflected.delete().where(reflected.c.id == values["id"]))
        # 仅保留 NULL 历史时才允许降级；旧列和回执仍完整。
        command.downgrade(config, "mcp_directory_semantics")
        with engine.connect() as db:
            assert {
                table: db.execute(
                    text(f"SELECT to_jsonb(r)::text FROM {table} r")
                ).all()
                for table in ROUTES
            } == before
