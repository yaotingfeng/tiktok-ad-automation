"""Fresh historical schemas; never downgrade a shared/current application DB."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app.core.config import settings
from tests.database import require_test_database


@contextmanager
def historical_database(monkeypatch, revision):
    require_test_database(str(settings.DATABASE_URL))
    source = make_url(str(settings.DATABASE_URL))
    name = f"migration_{uuid4().hex}_test"
    target = source.set(database=name)
    admin_url = source.set(drivername="postgresql", database="postgres")
    with psycopg.connect(
        admin_url.render_as_string(hide_password=False), autocommit=True
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    engine = create_engine(target)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                settings, "DATABASE_URL", target.render_as_string(hide_password=False)
            )
            backend = Path(__file__).resolve().parents[1]
            config = Config(str(backend / "alembic.ini"))
            config.set_main_option("script_location", str(backend / "app/alembic"))
            command.upgrade(config, revision)
            yield engine, config
    finally:
        engine.dispose()
        with psycopg.connect(
            admin_url.render_as_string(hide_password=False), autocommit=True
        ) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
