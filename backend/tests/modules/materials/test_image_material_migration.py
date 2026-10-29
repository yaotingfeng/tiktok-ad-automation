"""图片迁移为旧视频保留待探测状态与既有稳定索引。"""

from sqlalchemy import inspect, text

from tests.modules.materials.test_tenant_materials import material


def test_migration_media_defaults_preserve_unprobed_video(session, context):
    row = material(session, context, "unprobed.mp4")
    assert row.duration is None
    session.flush()
    result = session.execute(
        text("SELECT media_type, duration FROM material_file WHERE id=:id"),
        {"id": row.id},
    ).one()
    assert result == ("VIDEO", None)
    columns = {
        column["name"]
        for column in inspect(session.get_bind()).get_columns("ingest_session_file")
    }
    assert "media_type" in columns
    assert "client_index" in columns
    assert "sequence" not in columns


def test_upgrade_backfills_old_video_and_preserves_pending_duration(session):
    from importlib import import_module

    import pytest
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    # 事务内临时表隔离迁移演练，不回退共享测试库，也不改历史迁移。
    session.execute(
        text(
            "CREATE TEMP TABLE material_file (id integer PRIMARY KEY, duration double precision, storage_state text) ON COMMIT DROP"
        )
    )
    session.execute(
        text(
            "CREATE TEMP TABLE ingest_session_file (id integer PRIMARY KEY, client_index integer NOT NULL) ON COMMIT DROP"
        )
    )
    session.execute(text("INSERT INTO material_file VALUES (1,NULL,'receiving')"))
    session.execute(text("INSERT INTO ingest_session_file VALUES (1,7)"))
    migration = import_module("app.alembic.versions.0046_image_materials")
    with Operations.context(MigrationContext.configure(session.connection())):
        migration.upgrade()
    assert session.execute(
        text("SELECT media_type,duration,storage_state FROM material_file")
    ).one() == ("VIDEO", None, "receiving")
    assert session.execute(
        text("SELECT media_type,client_index FROM ingest_session_file")
    ).one() == ("VIDEO", 7)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text("INSERT INTO material_file VALUES (2,1.5,'stored','IMAGE')")
        )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(text("INSERT INTO ingest_session_file VALUES (2,8,'AUDIO')"))
