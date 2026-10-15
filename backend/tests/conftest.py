import os
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from redis import Redis
from sqlmodel import Session

from app.core.config import settings
from tests.database import require_test_database, require_test_redis

# Refuse before importing an engine or running migrations; never print the DSN.
try:
    require_test_database(str(settings.DATABASE_URL))
except ValueError as error:
    raise pytest.UsageError(str(error)) from None

from app.api.deps import get_db  # noqa: E402
from app.core.context import TenantContext  # noqa: E402
from app.core.db import engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from tests.utils.user import authentication_token_from_username  # noqa: E402
from tests.utils.utils import get_superuser_token_headers  # noqa: E402

# 快速层：验收目录整体及清单中的慢测试函数标记为 slow，`-m "not slow"` 即可排除。
# 清单由 deploy/ci/slow_tests.py 按实测耗时生成，按函数 ID（不含参数）匹配。
SLOW_TESTS = {
    line
    for line in (Path(__file__).with_name("slow_tests.txt")).read_text().splitlines()
    if line and not line.startswith("#")
}


def is_mcp_test(nodeid: str) -> bool:
    """官方 MCP 通道专属用例：文件名含 mcp，或参数化通道为 OFFICIAL_MCP。"""
    path, _, rest = nodeid.partition("::")
    params = rest.partition("[")[2].rstrip("]").split("-")
    return "mcp" in Path(path).name.lower() or "OFFICIAL_MCP" in params


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        nodeid = item.nodeid
        if (
            nodeid.startswith("tests/acceptance/")
            or nodeid.split("[", 1)[0] in SLOW_TESTS
        ):
            item.add_marker(pytest.mark.slow)
        # 官方 MCP 通道退役中，发布门禁不再运行；需要时 `pytest -m mcp` 手动运行。
        if is_mcp_test(nodeid):
            item.add_marker(pytest.mark.mcp)


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "app/alembic"))
    command.upgrade(config, "head")


@pytest.fixture
def session() -> Generator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            with Session(
                bind=connection, join_transaction_mode="create_savepoint"
            ) as value:
                init_db(value)
                yield value
        finally:
            transaction.rollback()


@pytest.fixture
def db(session: Session) -> Session:
    """Compatibility alias for the template tests, with per-test rollback."""
    return session


@pytest.fixture
def context() -> TenantContext:
    # Tenant modules seed real membership for this context in their own fixtures.
    return TenantContext(tenant_id=uuid4(), actor_id=uuid4(), role="operator")


@pytest.fixture
def client(session: Session) -> Generator[TestClient]:
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: session
    try:
        with TestClient(app) as value:
            yield value
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def superuser_token_headers(client: TestClient) -> dict[str, str]:
    return get_superuser_token_headers(client)


@pytest.fixture
def normal_user_token_headers(client: TestClient, db: Session) -> dict[str, str]:
    return authentication_token_from_username(
        client=client, username=settings.TEST_USERNAME, db=db
    )


@pytest.fixture(scope="session")
def test_run_id() -> str:
    return uuid4().hex


@pytest.fixture
def redis_key_prefix(test_run_id: str) -> str:
    return f"test:{test_run_id}:{uuid4().hex}"


@pytest.fixture
def redis_client() -> Generator[Redis]:
    url = os.environ.get("TEST_REDIS_URL", "")
    require_test_redis(url, settings.REDIS_URL)
    client = Redis.from_url(url, decode_responses=True)
    try:
        client.ping()
        yield client
    finally:
        # Each test owns and cleans only its own prefixed keys. Never FLUSHDB.
        client.close()
