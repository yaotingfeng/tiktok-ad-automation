"""最终集成 head 的历史证明：只升独立新库，不补今日路由或改原请求。"""

from datetime import UTC, datetime, timedelta

from alembic import command
from sqlalchemy import text
from sqlmodel import Session

from app.modules.materials.ingest_models import OriginalUse, TemporaryMaterialObject
from app.modules.materials.models import MaterialAssetOperation, MaterialFile
from tests.migration_database import historical_database
from tests.modules.builds.test_route_migration import historical_rows, snapshot
from tests.modules.materials.test_cover_evidence_migration import (
    _history as cover_history,
)
from tests.modules.materials.test_cover_evidence_migration import (
    _snapshot as cover_snapshot,
)
from tests.modules.materials.test_material_route_history import (
    old_rows,
    original_snapshot,
)

FINAL_HEAD = "mcp_cover_evidence"


def _original_history(db, context, operation_id):
    """追加原件和已过期但仍 active 的用途，迁移不能替业务判断释放。"""
    operation = db.get(MaterialAssetOperation, operation_id)
    assert operation is not None
    file = db.get(MaterialFile, operation.material_id)
    assert file is not None
    file.current_object_generation = 1
    original = TemporaryMaterialObject(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        generation=1,
        object_key=file.object_key,
        expected_bytes=file.byte_size,
        actual_bytes=file.byte_size,
        status="verified",
        video_md5="a" * 32,
        sha256="b" * 64,
        digest_verified_at=datetime.now(UTC),
        digest_source="historical-verifier",
    )
    db.add_all([file, original])
    db.flush()
    db.add(
        OriginalUse(
            tenant_id=context.tenant_id,
            bc_id=file.bc_id,
            material_id=file.id,
            generation=1,
            actor_id=context.actor_id,
            purpose="upload_original",
            operation_id=operation_id,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db.flush()


def _original_snapshot(connection):
    return {
        table: connection.execute(
            text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY id")
        )
        .scalars()
        .all()
        for table in ("material_file", "temporary_material_object", "original_use")
    }


def test_pre_route_build_history_upgrades_to_final_head_without_new_authority(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_directory_bc_scope") as (engine, config):
        with Session(engine) as db, db.begin():
            # 同 BC 的两个独立租户；单连接或多连接均不能证明历史授权版本。
            historical_rows(db, connections=1)
            historical_rows(db, connections=2)
        with engine.connect() as connection:
            before = snapshot(connection)
        command.upgrade(config, FINAL_HEAD)
        with engine.connect() as connection:
            assert snapshot(connection) == before
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == [FINAL_HEAD]
            assert (
                connection.execute(
                    text("SELECT count(*) FROM build_route_context")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(text("SELECT error_code FROM build_submission"))
                .scalars()
                .all()
                == ["legacy_route_unverifiable"] * 2
            )
            assert (
                connection.execute(
                    text("SELECT execution_connection_id FROM build_draft")
                )
                .scalars()
                .all()
                == [None] * 2
            )


def test_p2_material_history_upgrades_to_final_head_without_releasing_originals(
    monkeypatch,
):
    with historical_database(monkeypatch, "mcp_material_routes") as (engine, config):
        with Session(engine) as db, db.begin():
            for count in (1, 2):
                context, operation_id = old_rows(db, connections=count)
                _original_history(db, context, operation_id)
            cover, receipt_id = cover_history(db)
        with engine.connect() as connection:
            before = original_snapshot(connection)
            originals = _original_snapshot(connection)
            cover_before = cover_snapshot(connection, cover["id"], receipt_id)
        command.upgrade(config, FINAL_HEAD)
        with engine.connect() as connection:
            assert original_snapshot(connection) == before
            assert _original_snapshot(connection) == originals
            assert cover_snapshot(connection, cover["id"], receipt_id) == cover_before
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == [FINAL_HEAD]
            # 历史 NULL 不回填，真实旧 ID/请求/用途均保留；升级不能假装重新准备。
            for table, columns in (
                ("material_asset_operation", ("frozen_route",)),
                ("material_distribution", ("source_route", "target_route")),
                ("upload_batch", ("frozen_route",)),
                ("ingest_session", ("frozen_route",)),
                ("material_cover_job", ("frozen_route", "video_md5")),
                ("material_cover_receipt", ("receipt_facts",)),
            ):
                for column in columns:
                    assert (
                        connection.execute(
                            text(
                                f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"
                            )
                        ).scalar_one()
                        == 0
                    )
            assert (
                connection.execute(text("SELECT status FROM original_use"))
                .scalars()
                .all()
                == ["active"] * 2
            )
