"""两个管理任务共享一个进程请求窗口，不各建一份线程池。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import sleep

import pytest

from app.core.config import settings
from app.modules.ad_management.dispatch_work import run_bounded


@pytest.mark.parametrize("capacity", [3, 50])
def test_two_tasks_share_request_concurrency(monkeypatch, capacity):
    monkeypatch.setattr(settings, "AD_MANAGEMENT_REQUEST_CONCURRENCY", capacity)
    lock = Lock()
    first_wave = Event()
    current = peak = completed = 0

    def send(_):
        nonlocal current, peak, completed
        with lock:
            current += 1
            peak = max(peak, current)
            if current == capacity:
                first_wave.set()
        assert first_wave.wait(5)
        sleep(0.02)
        with lock:
            current -= 1
            completed += 1
        return True

    with ThreadPoolExecutor(max_workers=2) as callers:
        results = list(
            callers.map(
                lambda _: run_bounded(send, tuple(range(capacity * 2))), range(2)
            )
        )
    assert results == [True, True]
    assert completed == capacity * 4
    assert peak == capacity


def test_deferred_work_is_not_lost(monkeypatch):
    monkeypatch.setattr(settings, "AD_MANAGEMENT_REQUEST_CONCURRENCY", 3)
    assert run_bounded(lambda value: value != "busy", ("ready", "busy")) is False


def test_failed_request_does_not_abandon_other_futures(monkeypatch):
    monkeypatch.setattr(settings, "AD_MANAGEMENT_REQUEST_CONCURRENCY", 3)
    completed = []

    def send(value):
        if value == 0:
            raise RuntimeError("request failed")
        sleep(0.01)
        completed.append(value)
        return True

    with pytest.raises(RuntimeError, match="request failed"):
        run_bounded(send, tuple(range(8)))
    assert sorted(completed) == list(range(1, 8))
