"""Offline real-module capacity benchmark. Never connects to a business database.

Run from backend with an explicit *_test DATABASE_URL (or the private test .env)
AND TEST_REDIS_URL selecting a nonzero isolated Redis DB. Each invocation creates
and drops a separate owned PostgreSQL database. JSON output contains metrics only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import resource
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url


@dataclass(frozen=True)
class Parameters:
    accounts: int = 1000
    dramas: int = 2
    target_accounts: int = 3
    group_size: int = 10
    creative_count: int = 2
    seed: int = 20260908
    materials_per_drama: int = 30

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in asdict(self).values()):
            raise ValueError("Benchmark sizes and seed must be positive integers")
        if (
            self.target_accounts > self.accounts
            or self.group_size > 50
            or self.creative_count > 30
        ):
            raise ValueError("Invalid benchmark scope or strategy parameters")


def validate_database_url(value: str) -> URL:
    url = make_url(value)
    if (
        not url.drivername.startswith("postgresql")
        or not url.database
        or not re.fullmatch(r"[A-Za-z0-9_]+_test", url.database)
    ):
        raise ValueError(
            "Benchmark requires an explicitly named PostgreSQL *_test database"
        )
    return url.set(drivername="postgresql+psycopg")


def rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def measure_pages(
    fetch_page: Callable[[str | None], Any], *, identity: Callable[[Any], str]
) -> dict[str, Any]:
    cursor = previous = None
    count = maximum = response_max = 0
    durations: list[float] = []
    digest = hashlib.sha256()
    started_all = perf_counter()
    while True:
        started = perf_counter()
        page = fetch_page(cursor)
        durations.append(perf_counter() - started)
        items = page.items
        if len(items) > 100:
            raise ValueError("Capacity pages must respect the existing 100-row limit")
        for item in items:
            key = identity(item)
            if previous is not None and key <= previous:
                raise ValueError(
                    "Pages must be strictly ordered, with no duplicate rows"
                )
            previous = key
            digest.update(key.encode())
            digest.update(b"\n")
        count += len(items)
        maximum = max(maximum, len(items))
        payload = (
            page.model_dump(mode="json")
            if hasattr(page, "model_dump")
            else {"items": items, "next_cursor": page.next_cursor}
        )
        response_max = max(
            response_max,
            len(json.dumps(payload, separators=(",", ":"), default=str).encode()),
        )
        following = page.next_cursor
        if following is None:
            break
        if not items or following == cursor:
            raise ValueError("Pagination did not advance")
        cursor = following
    durations.sort()
    return {
        "rows": count,
        "pages": len(durations),
        "page_max": maximum,
        "p95_seconds": durations[max(0, math.ceil(0.95 * len(durations)) - 1)],
        "max_response_bytes": response_max,
        "seconds": perf_counter() - started_all,
        "ordered_key_digest": digest.hexdigest(),
    }


@contextmanager
def owned_database(base_url: str) -> Iterator[Engine]:
    """Create/drop only this invocation's random owned DB; never migrate base_url."""
    from alembic import command
    from alembic.config import Config

    from app.core.config import settings

    source = validate_database_url(base_url)
    identity = uuid4().hex
    name, marker = (
        f"p07_capacity_{identity[:16]}_test",
        f"p07-capacity-owner:{identity}",
    )
    admin = create_engine(source.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    benchmark = None
    old_url = settings.DATABASE_URL
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
            connection.exec_driver_sql(f"COMMENT ON DATABASE \"{name}\" IS '{marker}'")
        benchmark = create_engine(
            source.set(database=name), pool_size=5, max_overflow=5
        )
        with benchmark.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == name
        from pydantic import PostgresDsn

        settings.DATABASE_URL = PostgresDsn(
            source.set(database=name).render_as_string(hide_password=False)
        )
        command.upgrade(Config("alembic.ini"), "head")
        yield benchmark
    finally:
        settings.DATABASE_URL = old_url
        if benchmark is not None:
            benchmark.dispose()
        if created:
            with admin.connect() as connection:
                owner = connection.execute(
                    text(
                        "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                    ),
                    {"name": name},
                ).scalar_one()
                if owner != marker:
                    raise RuntimeError(
                        "Owned benchmark database marker changed; refusing cleanup"
                    )
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


class Recorder:
    def __init__(self, parameters: Parameters, output: Path | None = None):
        self.output = output
        self.started = perf_counter()
        self.sql_count = 0
        self.report: dict[str, Any] = {
            "schema": "p07-capacity-v1",
            "parameters": asdict(parameters),
            "external_mode": "offline SDK/provider transport doubles; no ad execution",
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "machine": platform.machine(),
            },
            "phases": {},
            "complete": False,
            "source_revision": subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip(),
            "source_dirty": subprocess.run(
                ["git", "diff", "--quiet"], check=False, capture_output=True
            ).returncode
            != 0,
        }

    def save(self) -> None:
        self.report["elapsed_seconds"] = perf_counter() - self.started
        self.report["peak_rss_bytes"] = rss_bytes()
        self.report["sql_statements"] = self.sql_count
        if self.output:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output.with_suffix(self.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self.report, indent=2, sort_keys=True) + "\n"
            )
            temporary.replace(self.output)

    def progress(self, phase: str, **metrics: Any) -> None:
        metrics["cumulative_sql_statements"] = self.sql_count
        self.report["phases"][phase] = metrics
        self.save()
        print(  # noqa: T201 -- bounded benchmark progress, never credentials or business rows.
            json.dumps(
                {
                    "phase": phase,
                    **{key: value for key, value in metrics.items() if key != "plan"},
                    "peak_rss_bytes": rss_bytes(),
                },
                sort_keys=True,
            ),
            flush=True,
        )


def run_benchmark(
    parameters: Parameters, *, output: Path | None = None, directory_only: bool = False
) -> dict[str, Any]:
    from app.core.config import settings
    from scripts.benchmark_support import run_scenario

    recorder = Recorder(parameters, output)
    with owned_database(str(settings.DATABASE_URL)) as database_engine:

        def count_sql(*_args: Any) -> None:
            recorder.sql_count += 1

        event.listen(database_engine, "before_cursor_execute", count_sql)
        run_scenario(
            database_engine, parameters, recorder, directory_only=directory_only
        )
        with database_engine.connect() as connection:
            recorder.report["database_bytes"] = connection.scalar(
                text("SELECT pg_database_size(current_database())")
            )
            recorder.report["environment"]["postgresql"] = connection.scalar(
                text("SHOW server_version")
            )
            recorder.report["environment"]["postgresql_settings"] = [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT name,setting,unit FROM pg_settings WHERE name IN "
                        "('shared_buffers','work_mem','max_connections','jit','max_parallel_workers_per_gather') ORDER BY name"
                    )
                ).mappings()
            ]
    recorder.report["complete"] = True
    recorder.save()
    return recorder.report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in [
        ("accounts", 1000),
        ("dramas", 2),
        ("target-accounts", 3),
        ("group-size", 10),
        ("creative-count", 2),
        ("seed", 20260908),
    ]:
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directory-only", action="store_true")
    options = vars(parser.parse_args())
    output, directory_only = options.pop("output"), options.pop("directory_only")
    run_benchmark(Parameters(**options), output=output, directory_only=directory_only)


if __name__ == "__main__":
    main()
