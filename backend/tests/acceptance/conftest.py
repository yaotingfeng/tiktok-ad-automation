# ruff: noqa: F811
import pytest

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
        yield AcceptanceScenario(database_engine, runtime, scope, other)
