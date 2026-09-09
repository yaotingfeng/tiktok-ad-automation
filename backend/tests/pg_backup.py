"""Explicit, version-checked PostgreSQL backup tools with private diagnostics."""

import os
import re
import shutil
import subprocess
from pathlib import Path


def tool_major(binary: str) -> int:
    name = Path(binary).name
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except OSError, subprocess.TimeoutExpired:
        raise RuntimeError(f"Cannot read {name} version") from None
    match = re.search(r"\(PostgreSQL\) (\d+)(?:\.|\s|$)", result.stdout)
    if result.returncode or not match:
        raise RuntimeError(f"Cannot read {name} PostgreSQL major version")
    return int(match.group(1))


def backup_tools(server_major: int) -> tuple[str, str]:
    directory = os.environ.get("PG_BIN_DIR")
    binaries = []
    for name in ("pg_dump", "pg_restore"):
        if directory:
            binary = Path(directory) / name
            if (
                not binary.is_absolute()
                or not binary.is_file()
                or not os.access(binary, os.X_OK)
            ):
                raise RuntimeError(
                    f"{name} unavailable in PG_BIN_DIR; install matching PostgreSQL clients"
                )
            binaries.append(str(binary))
        else:
            found = shutil.which(name)
            if not found:
                raise RuntimeError(
                    f"{name} unavailable; set PG_BIN_DIR to matching PostgreSQL clients"
                )
            binaries.append(found)
    dump_major, restore_major = (tool_major(binary) for binary in binaries)
    if dump_major < server_major:
        raise RuntimeError(
            f"pg_dump major {dump_major} is older than server major {server_major}; set PG_BIN_DIR to matching PostgreSQL clients"
        )
    if restore_major < dump_major:
        raise RuntimeError(
            f"pg_restore major {restore_major} is older than pg_dump major {dump_major}; set PG_BIN_DIR to matching PostgreSQL clients"
        )
    return binaries[0], binaries[1]


def run_backup_tool(binary: str, arguments: list[str], env: dict[str, str]) -> None:
    name = Path(binary).name
    try:
        result = subprocess.run(
            [binary, *arguments], env=env, capture_output=True, timeout=120
        )
    except OSError, subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{name} unavailable or timed out; backup/restore validation was not completed"
        ) from None
    if result.returncode:
        # stderr/argv can contain private identifiers or connection parameters.
        raise RuntimeError(
            f"{name} failed (exit {result.returncode}); backup/restore validation was not completed"
        )


if __name__ == "__main__":
    import psycopg
    from sqlalchemy.engine import make_url

    from app.core.config import settings
    from tests.database import require_test_database

    require_test_database(str(settings.DATABASE_URL))
    url = make_url(str(settings.DATABASE_URL)).set(drivername="postgresql")
    try:
        with psycopg.connect(
            url.render_as_string(hide_password=False), connect_timeout=10
        ) as connection:
            server_major = connection.info.server_version // 10000
    except psycopg.Error:
        raise SystemExit("PostgreSQL backup preflight connection failed") from None
    dump, restore = backup_tools(server_major)
    print(  # noqa: T201 - CLI emits only verified numeric versions
        f"Backup preflight: server major {server_major}, pg_dump major {tool_major(dump)}, pg_restore major {tool_major(restore)}"
    )
