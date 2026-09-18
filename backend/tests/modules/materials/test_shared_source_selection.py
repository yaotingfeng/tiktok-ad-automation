"""同 BC 与跨 BC 转存后的副本采用同一稳定共享来源。"""

from sqlmodel import Session

from app.core.db import engine
from app.modules.accounts.models import TenantBC
from app.modules.accounts.routing import freeze_route
from app.modules.materials.distribution_sources import distribution_sources
from app.modules.materials.models import MaterialFile
from tests.modules.materials.test_readiness import asset, target
from tests.modules.materials.test_source_uploads import source_env as source_env
from tests.modules.materials.test_source_uploads import wire as wire


def test_newer_secondary_copy_does_not_split_primary_share_batch(source_env, wire):
    with Session(engine) as db, db.begin():
        primary = target(db, source_env, advertiser_id="primary")
        secondary = target(db, source_env, advertiser_id="secondary")
        destination = target(db, source_env)
        db.get(
            TenantBC, (source_env["context"].tenant_id, source_env["bc_id"])
        ).material_advertiser_id = primary
        expected = asset(db, source_env, primary, seconds_old=600)
        asset(db, source_env, secondary)
        route = freeze_route(
            db, context=source_env["context"], bc_id=source_env["bc_id"]
        )
        selected = distribution_sources(
            db,
            context=source_env["context"],
            materials=[db.get(MaterialFile, source_env["material_id"])],
            advertiser_id=destination,
            route=route,
        )
        assert selected[source_env["material_id"]].id == expected.id
    assert wire[0] == []
