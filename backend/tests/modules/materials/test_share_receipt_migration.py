"""视频共享回执升级不补造历史响应，已有真实响应不可降级删除。"""

from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from app.modules.materials.batch_models import MaterialShareBatch
from tests.migration_database import historical_database, insert_historical_bc
from tests.modules.conftest import create_context


def test_share_receipt_migration_preserves_history_and_prevents_evidence_loss(
    monkeypatch,
):
    with historical_database(monkeypatch, "material_cover_sharing") as (
        engine,
        config,
    ):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            insert_historical_bc(db, tenant_id=context.tenant_id, bc_id="old-bc")
            route = {
                "tenant_id": str(context.tenant_id),
                "bc_id": "old-bc",
                "connection_id": str(uuid4()),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "adapter_contract_revision": "official-api-v1",
            }
            batch = MaterialShareBatch(
                tenant_id=context.tenant_id,
                bc_id="old-bc",
                actor_id=context.actor_id,
                source_advertiser_id="old-source",
                source_route=route,
                target_route=route,
                request_digest="a" * 64,
                claim_id=uuid4(),
                advertiser_ids=["old-target"],
            )
            db.add(batch)
            db.flush()
            receipt_id = uuid4()
            db.execute(
                text("""
                    INSERT INTO material_share_batch_receipt
                    (id,tenant_id,bc_id,batch_id,effect,created_at)
                    VALUES (:id,:tenant_id,'old-bc',:batch_id,'ACKNOWLEDGED',now())
                """),
                {
                    "id": receipt_id,
                    "tenant_id": context.tenant_id,
                    "batch_id": batch.id,
                },
            )
        with engine.connect() as db:
            before = db.execute(
                text("SELECT to_jsonb(r) FROM material_share_batch_receipt r")
            ).scalar_one()
        command.upgrade(config, "material_share_receipts")
        with engine.connect() as db:
            after = db.execute(
                text("SELECT to_jsonb(r) FROM material_share_batch_receipt r")
            ).scalar_one()
            assert after == {**before, "share_response": None}
        with engine.begin() as db:
            db.execute(
                text("""
                    INSERT INTO material_share_batch_receipt
                    (id,tenant_id,bc_id,batch_id,effect,created_at,share_response)
                    SELECT :id,tenant_id,bc_id,batch_id,effect,now(),
                        '{"failed_infos":{"old-target":["123"]}}'::jsonb
                    FROM material_share_batch_receipt WHERE id=:old_id
                """),
                {"id": uuid4(), "old_id": receipt_id},
            )
        with pytest.raises(
            DBAPIError, match="Cannot drop actual material share responses"
        ):
            command.downgrade(config, "material_cover_sharing")
