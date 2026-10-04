import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.auth import _exchange_token


def blocked_worker(_channel, _app_id, _secret, _auth_code):
    # A real spawned local process, no network and no real credentials.
    time.sleep(60)


def success_worker(channel, _app_id, _secret, _auth_code):
    channel.send(("ok", {"access_token": "fake-process-token", "scope": "[2,6]"}))
    channel.close()


def test_spawned_exchange_deadline_stops_child_before_return():
    before = {child.pid for child in multiprocessing.active_children()}
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _exchange_token,
            app_id="fake",
            secret="fake",
            auth_code="fake",
            deadline_seconds=0.3,
            _worker=blocked_worker,
        )
        with pytest.raises(DomainError) as error:
            future.result(timeout=8)
    assert error.value.code == "oauth_result_unknown"
    assert time.monotonic() - start < 6
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_spawned_exchange_succeeds_from_request_thread():
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(
            _exchange_token,
            app_id="fake",
            secret="fake",
            auth_code="fake",
            deadline_seconds=10,
            _worker=success_worker,
        ).result(timeout=12) == {"access_token": "fake-process-token", "scope": "[2,6]"}


def test_oauth_worker_module_is_importable_in_spawn_child():
    """The real OAuth worker must survive multiprocessing spawn import order."""
    import os
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "PYTHONPATH": str(backend)}
    result = subprocess.run(
        [sys.executable, "-c", "import app.integrations.tiktok.auth"],
        cwd=backend,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
