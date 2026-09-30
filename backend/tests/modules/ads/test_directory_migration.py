import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.modules.accounts.models import AdvertiserAccount
from app.modules.ads.models import AdObject
from tests.migration_database import historical_database
from tests.modules.conftest import create_context


def test_additive_upgrade_constraints_and_safe_downgrade(monkeypatch):
    with historical_database(monkeypatch, "material_push") as (engine, config):
        with Session(engine) as session, session.begin():
            context = create_context(session)
            account = AdvertiserAccount(tenant_id=context.tenant_id, advertiser_id="a")
            session.add(account)
        command.upgrade(config, "ads_reporting_data")
        assert {
            "ad_object",
            "campaign_name_projection",
            "ad_material_reference",
            "ad_directory_run",
            "ad_directory_page",
            "report_fact",
            "report_coverage",
            "report_observation",
            "report_sync_run",
            "report_staged_page",
            "sync_schedule",
            "account_balance_observation",
        } <= set(inspect(engine).get_table_names())
        with Session(engine) as session, session.begin():
            from datetime import UTC, datetime

            values = {
                "tenant_id": context.tenant_id,
                "advertiser_id": "a",
                "kind": "ad",
                "remote_id": "same",
                "ad_type": "REGULAR",
                "name": "x",
                "observed_at": datetime.now(UTC),
                "published_version": 1,
            }
            session.add(AdObject(**values))
            session.flush()
            with pytest.raises(IntegrityError), session.begin_nested():
                session.execute(AdObject.__table__.insert().values(**values))
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(AdObject(**(values | {"advertiser_id": "foreign"})))
                session.flush()
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(
                    AdObject(
                        **(
                            values
                            | {
                                "remote_id": "orphan",
                                "parent_remote_id": "parent",
                                "parent_kind": None,
                            }
                        )
                    )
                )
                session.flush()
        with pytest.raises(RuntimeError, match="投放数据"):
            command.downgrade(config, "material_push")
        with engine.begin() as db:
            db.execute(text("DELETE FROM ad_object"))
        command.downgrade(config, "material_push")
        assert "ad_object" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
