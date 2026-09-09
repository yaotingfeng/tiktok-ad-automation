# ruff: noqa: F811
import pytest
from sqlmodel import Session

from app.modules.accounts.models import AdvertiserAccount, BCAccountAccess
from tests.acceptance.scenario import (
    AcceptanceScenario,
    Wire,
    offline_runtime,
    seed_scope,
)
from tests.modules.strategies.test_concurrency import (
    isolated_strategy_database,  # noqa: F401
)


@pytest.fixture
def acceptance_scenario(isolated_strategy_database, request):
    database_engine, _, _ = isolated_strategy_database
    options = getattr(request, "param", "jiashu")
    options = {"provider_kind": options} if isinstance(options, str) else options
    wire = Wire()
    with offline_runtime(wire, database_engine) as runtime:
        scope = seed_scope(database_engine, wire, label="t1", **options)
        other = seed_scope(database_engine, wire, label="t2", material_count=1)
        with Session(database_engine) as session, session.begin():
            session.add(
                AdvertiserAccount(
                    tenant_id=other.context.tenant_id,
                    advertiser_id=scope.accounts[0],
                    name="Ownership conflict remains visible",
                    currency="USD",
                    timezone="UTC",
                    remote_status="ENABLE",
                    ownership_conflict=True,
                )
            )
            session.flush()
            session.add(
                BCAccountAccess(
                    tenant_id=other.context.tenant_id,
                    bc_id=other.bc_id,
                    advertiser_id=scope.accounts[0],
                    connection_id=other.connection_id,
                    in_bc=True,
                    authorized=True,
                    active=True,
                    permission_state="UNKNOWN",
                )
            )
        yield AcceptanceScenario(database_engine, runtime, scope, other)
