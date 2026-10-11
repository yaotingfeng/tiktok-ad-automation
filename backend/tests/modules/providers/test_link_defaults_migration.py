"""0021 只清理尚未开始准备的草稿中的废弃或旧强制默认取链参数。"""

from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from sqlalchemy import MetaData, Table, text
from sqlmodel import Session

from app.modules.providers.models import ProviderConnection
from app.modules.strategies.models import Strategy, StrategyVersion
from tests.migration_database import historical_database, insert_historical_bc
from tests.modules.builds.test_drafts import config
from tests.modules.conftest import create_context

REVISION = "0022_provider_link_defaults"


def _seed_parents(db, context):
    """旧 schema 的 provider_application 没有 link_defaults 列，应用行用反射表播种。"""
    insert_historical_bc(db, tenant_id=context.tenant_id, bc_id="bc-draft")
    connection = ProviderConnection(
        tenant_id=context.tenant_id,
        kind="jiashu",
        display_name="test",
        encrypted_credentials="unused",
        status="active",
    )
    db.add(connection)
    db.flush()
    applications = Table(
        "provider_application", MetaData(), autoload_with=db.connection()
    )
    db.execute(
        applications.insert().values(
            id=uuid4(),
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            external_id="app-draft",
            name="test",
            channel_config={},
        )
    )
    strategy = Strategy(tenant_id=context.tenant_id, name="test", latest_version=1)
    db.add(strategy)
    db.flush()
    values = config()
    version = StrategyVersion(
        tenant_id=context.tenant_id,
        strategy_id=strategy.id,
        number=1,
        copy_pool_version_id=values.copy_pool_version,
        config={
            "budget": str(values.budget),
            "currency": values.currency,
            "target_roas": str(values.target_roas),
            "group_size": 10,
            "creative_count": 2,
            "copy_pool_version": str(values.copy_pool_version),
        },
        budget=values.budget,
        target_roas=values.target_roas,
        created_by=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        request_kind="create",
    )
    db.add(version)
    db.flush()
    return connection.id, version.id


def _draft(db, context, parents, status, link_config):
    connection_id, version_id = parents
    table = Table("build_draft", MetaData(), autoload_with=db.connection())
    draft_id = uuid4()
    values = {
        "id": draft_id,
        "tenant_id": context.tenant_id,
        "bc_id": "bc-draft",
        "revision": 1,
        "status": status,
        "strategy_version_id": version_id,
        "provider_connection_id": connection_id,
        "application_id": "app-draft",
        "link_config": link_config,
        "delivery_config": {},
        "created_by": context.actor_id,
        "request_id": uuid4(),
        "request_digest": "a" * 64,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    db.execute(
        table.insert().values(**{k: v for k, v in values.items() if k in table.c})
    )
    return draft_id


def test_0022_cleans_only_unsubmitted_drafts(monkeypatch):
    with historical_database(monkeypatch, "0021_report_run_retry_count") as (
        engine,
        config_,
    ):
        with Session(engine) as db, db.begin():
            context = create_context(db)
            parents = _seed_parents(db, context)
            a = _draft(
                db,
                context,
                parents,
                "DRAFT",
                {
                    "episode": 1,
                    "promote_name": "x",
                    "card_point_episode": 1,
                    "free_episode_count": "1",
                },
            )
            b = _draft(
                db,
                context,
                parents,
                "DRAFT",
                {
                    "episode": 3,
                    "payment_template_id": "",
                    "alias": "y",
                    "episodic_drama_id": "9",
                },
            )
            c = _draft(
                db,
                context,
                parents,
                "READY",
                {"episode": 1, "promote_name": "x", "miniapp_id": "1144", "name": "x"},
            )
            d = _draft(
                db,
                context,
                parents,
                "DRAFT",
                {"miniapp_id": "1144", "name": "x", "episode": 3},
            )
            e = _draft(db, context, parents, "DRAFT", {"episode_seq": 1, "episode": 2})
        command.upgrade(config_, REVISION)
        with engine.connect() as db:
            rows = dict(
                db.execute(text("SELECT id, link_config FROM build_draft")).all()
            )
            applications = (
                db.execute(text("SELECT link_defaults FROM provider_application"))
                .scalars()
                .all()
            )
        assert rows[a] == {}
        assert rows[b] == {"episode": 3}
        assert rows[c] == {
            "episode": 1,
            "promote_name": "x",
            "miniapp_id": "1144",
            "name": "x",
        }
        assert rows[d] == {"episode": 3}
        assert rows[e] == {"episode": 2}
        assert applications == [{}]
