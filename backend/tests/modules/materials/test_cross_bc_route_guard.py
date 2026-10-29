"""独立来源授权必须按来源 BC 校验，不能借用目标 BC 绑定。"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.accounts.connection_models import BCConnectionBinding
from app.modules.materials.models import MaterialDistribution
from tests.modules.materials.test_tenant_materials import mapping, material


def seed_routes(session, context, *, historical=False):
    source = material(session, context, "source.mp4", historical=historical)
    destination = material(
        session, context, "target.mp4", bc="bc-b", historical=historical
    )
    source_asset = mapping(session, context, source, historical=historical)
    target_asset = mapping(
        session, context, destination, account="account-b", historical=historical
    )
    routes = []
    for asset in (source_asset, target_asset):
        session.add(
            BCConnectionBinding(
                tenant_id=context.tenant_id,
                bc_id=asset.bc_id,
                connection_id=asset.connection_id,
                kind="OFFICIAL_API",
            )
        )
        routes.append(
            {
                "tenant_id": str(context.tenant_id),
                "bc_id": asset.bc_id,
                "connection_id": str(asset.connection_id),
                "channel": "OFFICIAL_API",
                "authorization_revision": 0,
                "binding_revision": 0,
                "adapter_contract_revision": "test-independent-authorizations",
            }
        )
    session.flush()
    return {
        "tenant_id": context.tenant_id,
        "bc_id": target_asset.bc_id,
        "material_id": source.id,
        "advertiser_id": target_asset.advertiser_id,
        "actor_id": context.actor_id,
        "source_asset_id": source_asset.id,
        "source_material_id": source.id,
        "source_bc_id": source_asset.bc_id,
        "source_route": routes[0],
        "target_route": routes[1],
        "path": "share_source",
    }


@pytest.fixture
def route_pair(session, context):
    return seed_routes(session, context)


def test_cross_bc_distribution_accepts_independent_source_authorization(
    session, route_pair
):
    row = MaterialDistribution(**route_pair)
    session.add(row)
    session.flush()
    session.expire(row)
    assert row.source_route["bc_id"] == "bc-a"
    assert row.target_route["bc_id"] == "bc-b"
    assert row.source_route["connection_id"] != row.target_route["connection_id"]


@pytest.mark.parametrize(
    "invalid", ["target_connection", "unknown_connection", "foreign_tenant"]
)
def test_cross_bc_source_rejects_wrong_scope(session, route_pair, invalid):
    route = dict(route_pair["source_route"])
    if invalid == "target_connection":
        route["connection_id"] = route_pair["target_route"]["connection_id"]
    elif invalid == "unknown_connection":
        route["connection_id"] = str(uuid4())
    else:
        route["tenant_id"] = str(uuid4())
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(MaterialDistribution(**{**route_pair, "source_route": route}))
        session.flush()


def test_cross_bc_frozen_source_cannot_be_rewritten(session, route_pair):
    row = MaterialDistribution(**route_pair)
    session.add(row)
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        row.source_route = {**row.source_route, "authorization_revision": 1}
        session.add(row)
        session.flush()


def test_upgrade_preserves_history_and_refuses_unsafe_downgrade(monkeypatch):
    from alembic import command
    from sqlalchemy import MetaData, Table, text
    from sqlmodel import Session

    from tests.migration_database import historical_database
    from tests.modules.conftest import create_context

    with historical_database(monkeypatch, "preview_skipped_materials") as (
        engine,
        config,
    ):
        with Session(engine) as session, session.begin():
            values = seed_routes(session, create_context(session), historical=True)
            table = Table(
                "material_distribution", MetaData(), autoload_with=session.connection()
            )
            historical = MaterialDistribution(
                **{
                    **values,
                    "source_route": None,
                    "target_route": None,
                    "status": "blocked",
                }
            )
            # 旧版本尚无替代链字段，仅播种该迁移之前的真实列。
            session.execute(
                table.insert().values(
                    **{
                        key: value
                        for key, value in historical.model_dump().items()
                        if key in table.c and value is not None
                    }
                )
            )
            identity = historical.id
            before = session.execute(
                text("SELECT to_jsonb(d) FROM material_distribution d WHERE id=:id"),
                {"id": identity},
            ).scalar_one()
        command.upgrade(config, "material_route_scope")
        with Session(engine) as session, session.begin():
            assert (
                session.execute(
                    text(
                        "SELECT to_jsonb(d) FROM material_distribution d WHERE id=:id"
                    ),
                    {"id": identity},
                ).scalar_one()
                == before
            )
        # 没有冻结跨 BC 记录时允许回退，再升级仍能接受独立来源授权。
        command.downgrade(config, "preview_skipped_materials")
        command.upgrade(config, "material_route_scope")
        with Session(engine) as session, session.begin():
            row = MaterialDistribution(**values)
            session.execute(
                table.insert().values(
                    **{
                        key: value
                        for key, value in row.model_dump().items()
                        if key in table.c and value is not None
                    }
                )
            )
        with pytest.raises(RuntimeError, match="cross-BC evidence"):
            command.downgrade(config, "preview_skipped_materials")
        with engine.connect() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "material_route_scope"
            )
