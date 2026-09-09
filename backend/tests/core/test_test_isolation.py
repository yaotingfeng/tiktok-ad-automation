from uuid import uuid4

import pytest
from sqlmodel import Session

from app.core.db import engine
from app.models import User
from tests.database import require_test_database, require_test_redis


@pytest.mark.parametrize(
    "url",
    [
        "sqlite://",
        "postgresql://localhost/app",
        "postgresql://localhost/production",
        "postgresql://localhost/contest",
    ],
)
def test_non_test_databases_refused(url):
    with pytest.raises(ValueError, match="dedicated PostgreSQL test database"):
        require_test_database(url)


@pytest.mark.parametrize("name", ["app_test", "test_app", "app_test_scenario"])
def test_explicit_test_database_names_accepted(name):
    require_test_database(f"postgresql+psycopg://localhost/{name}")


def test_test_redis_must_be_separate():
    for url in ("", "redis://localhost:6379/0", "redis://localhost:6379/1"):
        with pytest.raises(ValueError):
            require_test_redis(url, "redis://localhost:6379/1")
    require_test_redis("redis://localhost:6379/2", "redis://localhost:6379/1")


def test_fixture_commit_is_not_visible_outside_outer_transaction(session):
    user = User(username=f"{uuid4().hex}", hashed_password="unused")
    session.add(user)
    session.commit()
    assert session.get(User, user.id) is not None
    with Session(engine) as independent:
        assert independent.get(User, user.id) is None


def test_redis_uses_owned_run_prefix(redis_client, redis_key_prefix):
    key = f"{redis_key_prefix}:isolation"
    try:
        assert redis_client.set(key, "owned", nx=True, ex=60)
        assert redis_client.get(key) == "owned"
    finally:
        redis_client.delete(key)
