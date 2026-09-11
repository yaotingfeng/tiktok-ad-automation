"""封面升级保留历史，只允许新记录保存原摘要和实际回执事实。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session

from app.modules.materials.cover_models import MaterialCoverJob, MaterialCoverReceipt
from tests.migration_database import historical_database
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import mapping, material


def _history(db):
    context = create_context(db)
    file = material(db, context, "original-cover.mp4")
    asset = mapping(db, context, file)
    row = MaterialCoverJob(
        tenant_id=context.tenant_id,
        bc_id=file.bc_id,
        material_id=file.id,
        asset_id=asset.id,
        advertiser_id=asset.advertiser_id,
        connection_id=asset.connection_id,
        actor_id=context.actor_id,
        video_id=asset.video_id,
        remote_name="original-image-name",
        status="UNKNOWN",
        request_armed_at=datetime.now(UTC),
        known_image_id="original-image-id",
    )
    receipt = MaterialCoverReceipt(
        tenant_id=context.tenant_id, job_id=row.id, image_id="original-image-id"
    )
    tables = MetaData()
    job_table = Table("material_cover_job", tables, autoload_with=db.connection())
    receipt_table = Table(
        "material_cover_receipt", tables, autoload_with=db.connection()
    )
    # 反射 JSONB 不保留 none_as_null 配置；省略旧路由列才能生成 SQL NULL。
    values = row.model_dump(exclude={"video_md5", "frozen_route"})
    SASession.execute(db, job_table.insert().values(values))
    SASession.execute(
        db, receipt_table.insert().values(receipt.model_dump(exclude={"receipt_facts"}))
    )
    return values, receipt.id


def _snapshot(db, job_id, receipt_id):
    return (
        db.execute(
            text(
                "SELECT to_jsonb(j)-'video_md5' FROM material_cover_job j WHERE id=:id"
            ),
            {"id": job_id},
        ).scalar_one(),
        db.execute(
            text(
                "SELECT to_jsonb(r)-'receipt_facts' FROM material_cover_receipt r WHERE id=:id"
            ),
            {"id": receipt_id},
        ).scalar_one(),
    )


def test_cover_evidence_preserves_history_and_rejects_rewrites_and_loss(monkeypatch):
    with historical_database(monkeypatch, "mcp_draft_connection") as (engine, config):
        with Session(engine) as db, db.begin():
            old, receipt_id = _history(db)
        with engine.connect() as db:
            before = _snapshot(db, old["id"], receipt_id)
        command.upgrade(config, "mcp_cover_evidence")
        with engine.connect() as db:
            assert _snapshot(db, old["id"], receipt_id) == before
            assert (
                db.execute(
                    text("SELECT video_md5 FROM material_cover_job WHERE id=:id"),
                    {"id": old["id"]},
                ).scalar_one()
                is None
            )
            assert (
                db.execute(
                    text(
                        "SELECT receipt_facts FROM material_cover_receipt WHERE id=:id"
                    ),
                    {"id": receipt_id},
                ).scalar_one()
                is None
            )
        for statement, identity, value in (
            (
                "UPDATE material_cover_job SET video_md5=:value WHERE id=:id",
                old["id"],
                "a" * 32,
            ),
            (
                "UPDATE material_cover_receipt SET receipt_facts=CAST(:value AS jsonb) WHERE id=:id",
                receipt_id,
                '{"signature":null}',
            ),
        ):
            with (
                engine.begin() as db,
                pytest.raises(DBAPIError, match="immutable cover"),
            ):
                db.execute(text(statement), {"id": identity, "value": value})
        tables = MetaData()
        jobs = Table("material_cover_job", tables, autoload_with=engine)
        receipts = Table("material_cover_receipt", tables, autoload_with=engine)
        current = {**old, "id": uuid4(), "video_id": "new-video", "video_md5": "a" * 32}
        new_receipt_id = uuid4()
        with (
            engine.begin() as db,
            pytest.raises(DBAPIError, match="ck_material_cover_video_md5"),
        ):
            db.execute(jobs.insert().values({**current, "video_md5": "invalid"}))
        with engine.begin() as db:
            db.execute(jobs.insert().values(current))
            db.execute(
                receipts.insert().values(
                    id=new_receipt_id,
                    tenant_id=old["tenant_id"],
                    job_id=current["id"],
                    image_id="new-image",
                    receipt_facts={"signature": None},
                    observed_at=datetime.now(UTC),
                )
            )
        for facts in (
            {},
            {"signature": "bad"},
            {"signature": True},
            {"signature": None, "extra": "x"},
        ):
            with engine.begin() as db, pytest.raises(DBAPIError):
                db.execute(
                    receipts.insert().values(
                        id=uuid4(),
                        tenant_id=old["tenant_id"],
                        job_id=current["id"],
                        image_id=str(uuid4()),
                        receipt_facts=facts,
                        observed_at=datetime.now(UTC),
                    )
                )
        with (
            engine.begin() as db,
            pytest.raises(DBAPIError, match="immutable cover video digest"),
        ):
            db.execute(
                jobs.update()
                .where(jobs.c.id == current["id"])
                .values(video_md5="b" * 32)
            )
        with (
            engine.begin() as db,
            pytest.raises(DBAPIError, match="immutable cover receipt"),
        ):
            db.execute(
                receipts.update()
                .where(receipts.c.id == new_receipt_id)
                .values(receipt_facts={"signature": "a" * 32})
            )
        with pytest.raises(DBAPIError, match="Cannot drop frozen cover evidence"):
            command.downgrade(config, "mcp_draft_connection")
        with engine.begin() as db:
            assert _snapshot(db, old["id"], receipt_id) == before
            db.execute(receipts.delete().where(receipts.c.id == new_receipt_id))
        # 两个分支单独成立时都拒降，不能只保护新job而丢弃旧请求的迟到回执。
        with pytest.raises(DBAPIError, match="Cannot drop frozen cover evidence"):
            command.downgrade(config, "mcp_draft_connection")
        with engine.begin() as db:
            db.execute(jobs.delete().where(jobs.c.id == current["id"]))
            db.execute(
                receipts.insert().values(
                    id=new_receipt_id,
                    tenant_id=old["tenant_id"],
                    job_id=old["id"],
                    image_id="late-image",
                    receipt_facts={"signature": "a" * 32},
                    observed_at=datetime.now(UTC),
                )
            )
        with pytest.raises(DBAPIError, match="Cannot drop frozen cover evidence"):
            command.downgrade(config, "mcp_draft_connection")
        with engine.begin() as db:
            db.execute(receipts.delete().where(receipts.c.id == new_receipt_id))
        command.downgrade(config, "mcp_draft_connection")
        with engine.connect() as db:
            assert _snapshot(db, old["id"], receipt_id) == before
