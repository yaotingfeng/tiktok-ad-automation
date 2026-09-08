from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from app.core.config import settings
from app.core.errors import DomainError
from app.modules.strategies.models import Strategy, StrategyVersion
from app.modules.strategies.service import append_version, create_strategy
from tests.database import require_test_database
from tests.modules.conftest import create_context
from tests.modules.strategies.test_versions import config


@pytest.fixture
def isolated_strategy_database(monkeypatch):
    """Immutable history is cleaned by dropping only this newly-created test DB."""
    url = make_url(str(settings.DATABASE_URL))
    require_test_database(str(settings.DATABASE_URL))
    name = f"strategy_{uuid4().hex}_test"
    temporary = url.set(database=name)
    admin_url = url.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    test_engine = create_engine(temporary)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                settings,
                "DATABASE_URL",
                temporary.render_as_string(hide_password=False),
            )
            backend = Path(__file__).resolve().parents[3]
            alembic = Config(str(backend / "alembic.ini"))
            alembic.set_main_option("script_location", str(backend / "app/alembic"))
            command.upgrade(alembic, "head")
        with Session(test_engine) as session:
            context = create_context(session)
            strategy = create_strategy(
                session, context=context, name="S", config=config()
            )
            session.commit()
        yield test_engine, context, strategy
    finally:
        test_engine.dispose()
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.parametrize(
    "mode", ["parallel_versions", "expected_version", "duplicate_request"]
)
def test_concurrent_strategy_writes_have_one_ordered_history(
    isolated_strategy_database, mode
):
    engine, context, identity = isolated_strategy_database
    barrier = Barrier(2)
    request = uuid4()

    def work(index):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            try:
                version = append_version(
                    session,
                    context=context,
                    strategy_id=identity,
                    config=config(
                        budget="200"
                        if mode == "duplicate_request"
                        else str(200 + index)
                    ),
                    expected_version=1 if mode == "expected_version" else None,
                    request_id=request if mode == "duplicate_request" else uuid4(),
                )
                session.commit()
                return version
            except DomainError as error:
                assert error.code == "version_conflict"
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(work, range(2)))
    with Session(engine) as session:
        versions = session.exec(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == identity)
            .order_by(StrategyVersion.number)
        ).all()
        assert [version.number for version in versions] == (
            [1, 2, 3] if mode == "parallel_versions" else [1, 2]
        )
        assert session.get(Strategy, identity).latest_version == len(versions)
        if mode == "duplicate_request":
            assert results[0] == results[1]
        if mode == "expected_version":
            assert results.count(None) == 1
