"""Safety checks before any test migration or database connection."""

import re
from urllib.parse import urlsplit


def require_test_database(url: str) -> None:
    parsed = urlsplit(url)
    name = parsed.path.lstrip("/")
    if not parsed.scheme.startswith("postgresql") or not re.search(
        r"(^|_)test($|_)", name
    ):
        raise ValueError(
            "Tests require DATABASE_URL naming a dedicated PostgreSQL test database"
        )


def require_test_redis(url: str, application_url: str) -> None:
    parsed = urlsplit(url)
    application = urlsplit(application_url)
    if (
        parsed.scheme not in {"redis", "rediss"}
        or not parsed.path.lstrip("/").isdigit()
        or int(parsed.path.lstrip("/")) == 0
    ):
        raise ValueError(
            "TEST_REDIS_URL must select a dedicated nonzero Redis database"
        )
    if (parsed.hostname, parsed.port or 6379, parsed.path) == (
        application.hostname,
        application.port or 6379,
        application.path,
    ):
        raise ValueError(
            "TEST_REDIS_URL must differ from the application Redis database"
        )
