"""独立历史库升级：旧VID/封面保留，主图类型和资格由PG强约束。"""

from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.materials.models import AccountMaterial
from tests.migration_database import historical_database
from tests.modules.conftest import create_context
from tests.modules.materials.test_tenant_materials import mapping, material


def test_image_account_upgrade_backfill_constraints_and_no_loss_downgrade(monkeypatch):
    with historical_database(monkeypatch, "0046_image_materials") as (engine, config):
        with Session(engine) as db, db.begin():
            owner = create_context(db)
            video = material(db, owner, "before.mp4", historical=True)
            old = mapping(db, owner, video, historical=True)
            old_id = old.id
        command.upgrade(config, "0048_image_reissue_routes")
        with Session(engine) as db, db.begin():
            old = db.get(AccountMaterial, old_id)
            assert old.media_type == "VIDEO" and old.video_id == "video-account-a"
            assert old.is_carousel_usable is None and old.frozen_route is None
            picture = material(db, owner, "after.png")
            picture.media_type, picture.mime_type = "IMAGE", "image/png"
            db.flush()
            fields = {
                "tenant_id": owner.tenant_id,
                "bc_id": picture.bc_id,
                "material_id": picture.id,
                "advertiser_id": old.advertiser_id,
                "connection_id": old.connection_id,
                "media_type": "IMAGE",
                "video_id": None,
                "image_id": "real-image",
                "status": "available",
                "verified_at": datetime.now(UTC),
            }
            for extra in (
                {"is_carousel_usable": None},
                {"is_carousel_usable": False},
                {"is_carousel_usable": True, "video_id": "fake-video"},
                {"is_carousel_usable": True, "material_id": video.id},
            ):
                with pytest.raises(IntegrityError), db.begin_nested():
                    db.add(AccountMaterial(**{**fields, **extra}))
                    db.flush()
            db.add(AccountMaterial(**fields, is_carousel_usable=True))
        # 拒绝退回会丢失IMAGE身份的版本，绝不清空真实映射以便降级。
        with pytest.raises(RuntimeError, match="Image mappings"):
            command.downgrade(config, "0046_image_materials")
        with Session(engine) as db:
            assert (
                db.execute(
                    text(
                        "SELECT count(*) FROM account_material WHERE media_type='IMAGE'"
                    )
                ).scalar_one()
                == 1
            )
