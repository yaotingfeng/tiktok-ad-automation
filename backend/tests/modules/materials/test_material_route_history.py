"""旧素材身份和请求逐值保留，今日连接数量不构成历史授权证据。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlmodel import Session

from app.core.errors import DomainError
from app.modules.accounts.models import AdvertiserAccount, TikTokConnection
from app.modules.materials.models import MaterialFile
from app.modules.materials.routes import load_material_route
from tests.migration_database import historical_database, insert_historical_bc
from tests.modules.conftest import create_context


def old_rows(db, connections):
    context = create_context(db)
    # 不使用当前 BC ORM 读取历史 schema 尚不存在的主素材账户列。
    insert_historical_bc(db, tenant_id=context.tenant_id, bc_id="bc-a")
    file = MaterialFile(
        tenant_id=context.tenant_id,
        bc_id="bc-a",
        file_name="original-name.mp4",
        object_key=f"historical/{uuid4()}",
        byte_size=4_000_000_000,
        storage_state="stored",
    )
    db.add(file)
    account_table = Table(
        "advertiser_account", MetaData(), autoload_with=db.connection()
    )
    account = AdvertiserAccount(
        tenant_id=context.tenant_id, advertiser_id="actual-source"
    )
    db.execute(
        account_table.insert().values(
            **{
                key: value
                for key, value in account.model_dump().items()
                if key in account_table.c
            }
        )
    )
    for _ in range(connections):
        db.add(TikTokConnection(tenant_id=context.tenant_id, status="ACTIVE"))
    db.flush()
    operation_id = uuid4()
    metadata = MetaData()
    operation = Table(
        "material_asset_operation", metadata, autoload_with=db.connection()
    )
    db.execute(
        operation.insert().values(
            id=operation_id,
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            material_id=file.id,
            advertiser_id="actual-source",
            path="upload_original",
            status="result_unknown",
            request_digest="a" * 64,
            remote_response={
                "video_id": "actual-remote-vid",
                "mid": "actual-mid",
                "send_armed": True,
                "remote_name": "original-name.mp4",
                "revision": 7,
            },
        )
    )
    distribution = Table(
        "material_distribution", metadata, autoload_with=db.connection()
    )
    db.execute(
        distribution.insert().values(
            id=uuid4(),
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            material_id=file.id,
            advertiser_id="actual-source",
            actor_id=context.actor_id,
            operation_id=operation_id,
            path="existing_target",
            status="result_unknown",
        )
    )
    batch = Table("upload_batch", metadata, autoload_with=db.connection())
    from datetime import UTC, datetime

    db.execute(
        batch.insert().values(
            id=uuid4(),
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            actor_id=context.actor_id,
            request_id=uuid4(),
            request_digest="b" * 64,
            status="stored",
            created_at=datetime.now(UTC),
        )
    )
    ingest = Table("ingest_session", metadata, autoload_with=db.connection())
    from app.modules.materials.ingest_models import IngestSession

    row = IngestSession(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        actor_id=context.actor_id,
        request_id=uuid4(),
        request_digest="c" * 64,
        expected_files=1,
        expected_bytes=123,
    )
    db.execute(ingest.insert().values(row.model_dump(exclude={"frozen_route"})))
    return context, operation_id


def original_snapshot(db):
    result = {}
    for table in (
        "material_asset_operation",
        "material_distribution",
        "upload_batch",
        "ingest_session",
        "account_material",
    ):
        result[table] = (
            db.execute(
                text(
                    f"SELECT (to_jsonb(t) - ARRAY['frozen_route','source_route','target_route'])::text FROM {table} t ORDER BY id"
                )
            )
            .scalars()
            .all()
        )
    return result


@pytest.mark.parametrize("connections", [0, 1, 2])
def test_history_keeps_original_payload_ids_and_null_route_for_same_bc_two_tenants(
    monkeypatch, connections
):
    with historical_database(monkeypatch, "mcp_directory_semantics") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            first, identity = old_rows(db, connections)
            second, second_id = old_rows(db, connections)
        with engine.connect() as db:
            before = original_snapshot(db)
        command.upgrade(config, "mcp_material_routes")
        with engine.connect() as db:
            assert original_snapshot(db) == before
            for table, columns in (
                ("material_asset_operation", ("frozen_route",)),
                ("material_distribution", ("source_route", "target_route")),
                ("upload_batch", ("frozen_route",)),
                ("ingest_session", ("frozen_route",)),
            ):
                for column in columns:
                    assert (
                        db.execute(
                            text(
                                f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
                            )
                        ).scalar_one()
                        == 0
                    )
        with Session(engine) as db:
            for context, op_id in ((first, identity), (second, second_id)):
                # 读取迁移目标真实存在的列，不让当前 ORM 引入后续迁移字段。
                operation = db.execute(
                    text(
                        "SELECT advertiser_id, remote_response, frozen_route, bc_id "
                        "FROM material_asset_operation WHERE id = :id"
                    ),
                    {"id": op_id},
                ).one()
                assert operation.advertiser_id == "actual-source"
                assert operation.remote_response["video_id"] == "actual-remote-vid"
                with pytest.raises(DomainError) as error:
                    load_material_route(
                        operation.frozen_route, context=context, bc_id=operation.bc_id
                    )
                assert error.value.code == "material_route_unverified"
