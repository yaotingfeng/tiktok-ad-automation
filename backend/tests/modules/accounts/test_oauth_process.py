import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.errors import DomainError
from app.integrations.tiktok.auth import _exchange_token


def blocked_worker(_channel, _app_id, _secret, _auth_code):
    # A real spawned local process, no network and no real credentials.
    time.sleep(60)


def success_worker(channel, _app_id, _secret, _auth_code):
    channel.send(("ok", "fake-process-token"))
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
        assert (
            pool.submit(
                _exchange_token,
                app_id="fake",
                secret="fake",
                auth_code="fake",
                deadline_seconds=10,
                _worker=success_worker,
            ).result(timeout=12)
            == "fake-process-token"
        )
